"""Survey-email send service (Resend).

Sends the annual "confirm your info" survey to every eligible alum in a
graduation year. Each recipient gets a personalized email with a UNIQUE,
HMAC-signed link to `<SURVEY_APP_BASE_URL>/survey/<token>`; the token carries the
alum's id so the landing page can (later) load their record and save edits.

Design notes:
- The Resend API key lives ONLY in backend config (`RESEND_API_KEY`); it is never
  exposed to the frontend.
- `dry_run=True` (the endpoint default) builds and counts everything but sends
  nothing — safe to run before the domain/key are live.
- Outbound HTTP mirrors `supabase_admin.py`: a short-timeout `httpx.AsyncClient`
  that raises `ServiceError` on misconfig / transport / non-2xx, never leaking
  upstream bodies.
- The token copy here is the Career Directors' default wording. A staff-editable
  message lives in the frontend prototype only; wiring that through the backend
  is a later step.
"""

from __future__ import annotations

import asyncio
import base64
import datetime
import hashlib
import hmac
import logging
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from html import escape

import httpx
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.dropdowns import holds_designation
from app.core.errors import ServiceError
from app.models.alumni import Alumni
from app.models.audit import AuditLog
from app.models.contact import AlumniContactInfo
from app.models.employment import CurrentEmployment
from app.models.engagement import AlumniProgramEngagement
from app.models.survey_response import SurveyResponse
from app.repositories.alumni import build_alumni_query
from app.schemas.survey import (
    GraduationYearCount,
    SurveyRespondInfo,
    SurveySendResult,
    SurveySendSample,
    SurveyUsage,
)

log = logging.getLogger(__name__)

_RESEND_BATCH_URL = "https://api.resend.com/emails/batch"
# Each real send writes an audit row whose text carries `sent=N` (the recipients
# actually sent). We sum those N to report true daily/monthly usage.
_SENT_COUNT_RE = re.compile(r"\bsent=(\d+)")

# The survey is ANNUAL: once an alum replies, they're not re-surveyed for a year.
# A response submitted on/after this cutoff counts as "already surveyed this
# cycle" — used both to exclude them from a send and to count real replies.
_RESURVEY_INTERVAL_DAYS = 365


def _resurvey_cutoff() -> datetime.datetime:
    return datetime.datetime.now(datetime.UTC) - datetime.timedelta(
        days=_RESURVEY_INTERVAL_DAYS
    )


def _tally_sent(rows, start_today: datetime.datetime) -> tuple[int, int]:
    """Sum ``sent=N`` across ``(new_value, created_at)`` audit rows. ``rows`` are
    already scoped to this calendar month, so their sum is the month total; rows
    at/after ``start_today`` also count toward the day total. A row with no
    ``sent=N`` contributes 0."""
    sent_today = 0
    sent_this_month = 0
    for new_value, created_at in rows:
        match = _SENT_COUNT_RE.search(new_value or "")
        n = int(match.group(1)) if match else 0
        sent_this_month += n
        if created_at >= start_today:
            sent_today += n
    return sent_today, sent_this_month


async def get_send_usage(session: AsyncSession) -> SurveyUsage:
    """Real send usage for the console tallies: emails actually sent today and
    this calendar month, summed from the ``send_survey`` audit rows (each records
    ``sent=N``). Day/month boundaries are UTC, matching the app's other date
    filtering. Dry runs (``send_survey_dry_run``) are excluded by the action
    filter."""
    settings = get_settings()
    anchor = settings.survey_usage_baseline_at
    now = datetime.datetime.now(datetime.UTC)
    start_today = datetime.datetime.combine(
        now.date(), datetime.time.min, tzinfo=datetime.UTC
    )
    start_month = start_today.replace(day=1)
    stmt = (
        select(AuditLog.new_value, AuditLog.created_at)
        .where(AuditLog.action_type == "send_survey")
        .where(AuditLog.created_at >= start_month)
    )
    if anchor is not None:
        # Baseline set: the baseline covers everything up to the anchor, so only
        # count sends recorded strictly AFTER it (avoids double-counting).
        stmt = stmt.where(AuditLog.created_at > anchor)
    rows = (await session.execute(stmt)).all()
    sent_today, sent_this_month = _tally_sent(rows, start_today)
    if anchor is not None:
        # Add the baseline only while we're still in the anchor's day / month.
        if now.date() == anchor.date():
            sent_today += settings.survey_usage_baseline_today
        if (now.year, now.month) == (anchor.year, anchor.month):
            sent_this_month += settings.survey_usage_baseline_month
    return SurveyUsage(sent_today=sent_today, sent_this_month=sent_this_month)
_TIMEOUT_SECONDS = 20.0
# Resend caps a batch call at 100 messages.
_BATCH_MAX = 100

_SUBJECT = "Your BYU Finance alumni information — a quick update"

_INTRO = (
    "Our BYU Finance alumni are one of the greatest strengths of our program. "
    "We are working to strengthen our alumni community by staying connected with "
    "you throughout your career. To do that, we're reaching out to ensure we have "
    "your most current information.\n\n"
    "Please take a moment to review the information below and update or replace "
    "any information in our alumni survey that is wrong or missing."
)
_CLOSING = (
    "If everything above is correct, please confirm that your information is up "
    "to date at the bottom of the survey. If anything has changed, please update "
    "the applicable questions in the survey. We have also included a few optional "
    "questions that will help us better connect with and support our alumni "
    "community.\n\n"
    "Thank you for being an important part of the BYU Finance family. We look "
    "forward to staying connected with you in the years ahead!\n\n"
    "Warmest regards,\nTanya Harmon & Amy Densley\nBYU Finance Career Directors"
)

_BTN_STYLE = (
    "display:inline-block;background:#1e2a4a;color:#ffffff;text-decoration:none;"
    "font-size:15px;font-weight:600;padding:11px 20px;border-radius:8px;"
)


# ------------------------------------------------------------------ tokens ---


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _token_secret() -> str:
    secret = get_settings().survey_token_secret
    if not secret:
        raise ServiceError("Survey token secret is not configured (SURVEY_TOKEN_SECRET).")
    return secret


def make_survey_token(alumni_id: int, graduation_year: int) -> str:
    """A stateless, tamper-evident token: `<b64(payload)>.<b64(hmac)>`."""
    payload = f"{alumni_id}.{graduation_year}".encode()
    sig = hmac.new(_token_secret().encode("utf-8"), payload, hashlib.sha256).digest()
    return f"{_b64(payload)}.{_b64(sig)}"


def verify_survey_token(token: str) -> int | None:
    """Return the alumni_id if the token is valid + untampered, else None."""
    try:
        payload_b64, sig_b64 = token.split(".", 1)
        payload = _unb64(payload_b64)
        sig = _unb64(sig_b64)
    except Exception:  # noqa: BLE001 - any decode failure means an invalid token
        return None
    expected = hmac.new(_token_secret().encode("utf-8"), payload, hashlib.sha256).digest()
    if not hmac.compare_digest(sig, expected):
        return None
    try:
        alumni_id_str, _ = payload.decode("utf-8").split(".", 1)
        return int(alumni_id_str)
    except (ValueError, UnicodeDecodeError):
        return None


# ---------------------------------------------------------------- template ---


@dataclass(frozen=True)
class Recipient:
    alumni_id: int
    first_name: str
    email: str
    # The full "here's what we have on file" list (label, value) shown in the
    # email — the Career Directors' field list, empties as "—".
    on_file: tuple[tuple[str, str], ...]


def _on_file_rows(r: Recipient) -> list[tuple[str, str]]:
    return list(r.on_file)


def _dash(value: object) -> str:
    """Display value, or an em dash when we have nothing on file."""
    text = "" if value is None else str(value).strip()
    return text or "—"


def _build_on_file(alum, contact, job) -> tuple[tuple[str, str], ...]:
    """The Career Directors' full field list, in order, for the email preview."""
    spouse = " ".join(
        p for p in (alum.spouse_first_name, alum.spouse_last_name) if p
    ).strip()
    g = lambda o, n: getattr(o, n, None)  # noqa: E731
    return (
        ("Current employment status", _dash(alum.employment_status)),
        ("Company", _dash(g(job, "current_employer"))),
        ("Title", _dash(g(job, "current_title"))),
        ("Industry", _dash(g(job, "current_industry"))),
        ("Secondary industry", _dash(g(job, "current_industry_secondary"))),
        ("Employment city", _dash(g(job, "current_city"))),
        ("Employment state", _dash(g(job, "current_state"))),
        ("Employment country", _dash(g(job, "current_country"))),
        ("Residence city", _dash(g(contact, "city"))),
        ("Residence state", _dash(g(contact, "state"))),
        ("Residence country", _dash(g(contact, "country"))),
        ("Spouse name", _dash(spouse)),
        ("Permanent email", _dash(g(contact, "personal_email"))),
        ("Work email", _dash(g(contact, "work_email"))),
        ("LinkedIn profile", _dash(alum.linkedin_url)),
        ("Graduate school program", _dash(alum.graduate_degree)),
        ("Graduate school name", _dash(alum.graduate_school)),
        ("Projected graduation year", _dash(alum.graduate_graduation_year)),
        ("Finance designations", _dash(alum.other_designations)),
    )


def render_survey_email(r: Recipient, link: str) -> tuple[str, str, str]:
    """Return (subject, html, text) for one recipient."""
    rows = _on_file_rows(r)

    # Plain-text part.
    text_lines = [f"Hello {r.first_name},", "", _INTRO, ""]
    if rows:
        text_lines.append("Here's what we have on file:")
        text_lines += [f"  {label}: {value}" for label, value in rows]
        text_lines.append("")
    text_lines += [f"Confirm or update your info: {link}", "", _CLOSING]
    text = "\n".join(text_lines)

    # HTML part (inline styles for email clients).
    info_html = ""
    if rows:
        cells = "".join(
            f'<tr><td style="padding:4px 16px 4px 0;color:#6b7280;font-size:14px;">'
            f"{escape(label)}</td>"
            f'<td style="padding:4px 0;color:#111827;font-size:14px;font-weight:600;">'
            f"{escape(value)}</td></tr>"
            for label, value in rows
        )
        info_html = (
            '<div style="margin:16px 0;padding:16px;border:1px solid #e5e7eb;'
            'border-radius:8px;background:#f9fafb;">'
            '<div style="font-size:13px;font-weight:600;color:#111827;'
            'margin-bottom:8px;">Here\'s what we have on file</div>'
            f'<table style="border-collapse:collapse;">{cells}</table></div>'
        )

    intro_html = escape(_INTRO).replace("\n\n", "</p><p style=\"margin:0 0 12px;\">")
    closing_html = escape(_CLOSING).replace("\n", "<br>")
    html = f"""\
<div style="margin:0;padding:0;background:#f3f4f6;">
  <div style="max-width:600px;margin:0 auto;background:#ffffff;">
    <div style="background:#1e2a4a;padding:16px 24px;">
      <span style="color:#ffffff;font-size:16px;font-weight:600;">BYU Finance Alumni Update</span>
    </div>
    <div style="padding:24px;font-family:Arial,sans-serif;color:#374151;line-height:1.55;">
      <p style="margin:0 0 12px;font-size:15px;">Hello {escape(r.first_name)},</p>
      <p style="margin:0 0 12px;font-size:15px;">{intro_html}</p>
      {info_html}
      <div style="margin:20px 0;">
        <a href="{escape(link)}" style="{_BTN_STYLE}">Confirm or update my info</a>
      </div>
      <p style="margin:0;font-size:14px;color:#4b5563;">{closing_html}</p>
    </div>
    <div style="padding:16px 24px;border-top:1px solid #e5e7eb;font-family:Arial,sans-serif;
         font-size:12px;color:#9ca3af;">BYU Marriott School of Business</div>
  </div>
</div>"""
    return _SUBJECT, html, text


# ----------------------------------------------------- respondent (public) ---


def _held(value: str | None) -> str | None:
    """'Yes' when a designation column says the alum HOLDS it, else None (so `put`
    drops the key and the survey renders an unticked box).

    Uses the shared :func:`holds_designation` predicate, so an alum whose column
    was imported as "No" (or any other negative) is NOT pre-ticked — presence
    alone is not the question."""
    return "Yes" if holds_designation(value) else None


async def get_respondent(
    session: AsyncSession, token: str
) -> SurveyRespondInfo | None:
    """Resolve a survey token to the alum's current on-file info for the public
    confirm page. Returns None if the token is invalid/tampered or the alum is
    gone/archived. The token is the credential (no login), so only the fields the
    survey confirms are returned — keyed by the frontend's SURVEY_FIELDS keys."""
    alumni_id = verify_survey_token(token)
    if alumni_id is None:
        return None
    alum = (
        await session.execute(select(Alumni).where(Alumni.alumni_id == alumni_id))
    ).scalar_one_or_none()
    if alum is None or alum.archived:
        return None

    contact = (
        await session.execute(
            select(AlumniContactInfo).where(AlumniContactInfo.alumni_id == alumni_id)
        )
    ).scalar_one_or_none()
    job = (
        await session.execute(
            select(CurrentEmployment).where(CurrentEmployment.alumni_id == alumni_id)
        )
    ).scalar_one_or_none()
    # Only for the CFA/CFP tickboxes (#529) — without it the confirm page would
    # show an alum who already holds the CFA an empty box, and they'd have to
    # re-tick something we already know.
    eng = (
        await session.execute(
            select(AlumniProgramEngagement).where(
                AlumniProgramEngagement.alumni_id == alumni_id
            )
        )
    ).scalar_one_or_none()

    fields: dict[str, str] = {}

    def put(key: str, value: object) -> None:
        if value is not None and str(value).strip() != "":
            fields[key] = str(value)

    # Employment (current_employment)
    put("employment.current_employer", getattr(job, "current_employer", None))
    put("employment.current_title", getattr(job, "current_title", None))
    put("employment.current_industry", getattr(job, "current_industry", None))
    put(
        "employment.current_industry_secondary",
        getattr(job, "current_industry_secondary", None),
    )
    put("employment.current_city", getattr(job, "current_city", None))
    put("employment.current_state", getattr(job, "current_state", None))
    put("employment.current_country", getattr(job, "current_country", None))
    put("employment.current_zip", getattr(job, "current_zip", None))
    put("employment.seniority_level", getattr(job, "seniority_level", None))
    # Contact (alumni_contact_info)
    put("contact.personal_email", getattr(contact, "personal_email", None))
    put("contact.work_email", getattr(contact, "work_email", None))
    put("contact.phone", getattr(contact, "phone", None))
    put("contact.city", getattr(contact, "city", None))
    put("contact.state", getattr(contact, "state", None))
    put("contact.country", getattr(contact, "country", None))
    # Profile (alumni)
    put("profile.employment_status", alum.employment_status)
    put("profile.linkedin_url", alum.linkedin_url)
    put("profile.graduate_degree", alum.graduate_degree)
    put("profile.graduate_school", alum.graduate_school)
    put("profile.graduate_graduation_year", alum.graduate_graduation_year)
    put("profile.spouse_first_name", alum.spouse_first_name)
    put("profile.spouse_last_name", alum.spouse_last_name)
    put("profile.other_designations", alum.other_designations)
    # Designations (alumni_program_engagement). The columns hold a marker string
    # ('CFA'/'CFP') when held and NULL when not, but the survey asks a
    # held/not-held question — so send the tickbox's own vocabulary and omit the
    # key entirely when it isn't held, which renders as an unticked box. "Not
    # held" is decided by `holds_designation`, not by presence: an imported "No"
    # is a stored value but must NOT pre-tick the box.
    put("program.cfa_designation", _held(getattr(eng, "cfa_designation", None)))
    put("program.cfp_designation", _held(getattr(eng, "cfp_designation", None)))
    put("profile.gender", alum.gender)
    put("profile.marital_status", alum.marital_status)
    # A date column — emit as an ISO "YYYY-MM-DD" string for the survey date input.
    put("profile.birth_date", alum.birth_date)
    put("profile.citizenship", alum.citizenship)
    put("profile.home_country", alum.home_country)

    first = (alum.preferred_first_name or alum.first_name or "there").strip()
    full = " ".join(p for p in (alum.first_name, alum.last_name) if p).strip() or first
    return SurveyRespondInfo(first_name=first, full_name=full, fields=fields)


# --------------------------------------------------------- graduation years --


async def list_graduation_years(session: AsyncSession) -> list[GraduationYearCount]:
    """Every graduation year present among eligible alumni (is_alumni, not
    archived), with a count — newest first. Drives the console's year picker so
    it reflects the real DB (including the 1900 test cohort)."""
    stmt = (
        select(Alumni.graduation_year, func.count().label("n"))
        .where(
            Alumni.is_alumni.is_(True),
            Alumni.archived.is_(False),
            Alumni.graduation_year.is_not(None),
        )
        .group_by(Alumni.graduation_year)
        .order_by(Alumni.graduation_year.desc())
    )
    rows = (await session.execute(stmt)).all()

    # How many DISTINCT alumni have replied WITHIN THE LAST YEAR for each grad
    # year (any status — a reply is a reply). Matches the re-survey exclusion in
    # _load_recipients, so "responded" == "already surveyed this cycle".
    responded_stmt = (
        select(
            SurveyResponse.graduation_year,
            func.count(func.distinct(SurveyResponse.alumni_id)).label("responded"),
        )
        .where(
            SurveyResponse.graduation_year.is_not(None),
            SurveyResponse.submitted_at >= _resurvey_cutoff(),
        )
        .group_by(SurveyResponse.graduation_year)
    )
    responded_by_year = {
        year: responded for year, responded in (await session.execute(responded_stmt)).all()
    }

    return [
        GraduationYearCount(
            graduation_year=year,
            total_alumni=count,
            responded=responded_by_year.get(year, 0),
        )
        for year, count in rows
    ]


# ------------------------------------------------------------- send service --


# Reserved / placeholder domains that must never be emailed — Resend rejects the
# whole batch if any `to` uses one, and they're never real inboxes anyway.
_UNSENDABLE_DOMAINS = frozenset(
    {"example.com", "example.org", "example.net", "test", "localhost", "invalid"}
)


def _is_sendable_email(email: str | None) -> bool:
    """A minimal deliverability gate: a real-looking address, not a reserved /
    placeholder domain (e.g. the REPLACE_WITH_…@example.com test stand-ins)."""
    if not email or "@" not in email:
        return False
    local, _, domain = email.partition("@")
    domain = domain.strip().lower()
    if not local.strip() or "." not in domain:
        return False
    return domain not in _UNSENDABLE_DOMAINS


def _chunks(items: list, size: int):
    for i in range(0, len(items), size):
        yield items[i : i + size]


async def _load_recipients(session: AsyncSession, graduation_year: int) -> list[Recipient]:
    """Eligible alumni for a grad year (is_alumni, not archived) who have a
    personal email AND have NOT replied within the last year — with the on-file
    fields the email previews. Side tables are bulk-loaded once (no N+1),
    mirroring the CSV export. Excluding recent responders is what stops an alum
    who already confirmed their info from being surveyed again this cycle."""
    has_personal_email = (
        select(AlumniContactInfo.contact_info_id)
        .where(
            AlumniContactInfo.alumni_id == Alumni.alumni_id,
            AlumniContactInfo.personal_email.is_not(None),
        )
        .exists()
    )
    # Already replied within the last year -> skip (the survey is annual).
    replied_recently = (
        select(SurveyResponse.survey_response_id)
        .where(
            SurveyResponse.alumni_id == Alumni.alumni_id,
            SurveyResponse.submitted_at >= _resurvey_cutoff(),
        )
        .exists()
    )
    stmt = (
        build_alumni_query(graduation_year=graduation_year)
        .where(has_personal_email)
        .where(~replied_recently)
    )
    alumni = (await session.execute(stmt)).scalars().all()
    ids = [a.alumni_id for a in alumni]
    if not ids:
        return []

    contacts = {
        c.alumni_id: c
        for c in (
            await session.execute(
                select(AlumniContactInfo).where(AlumniContactInfo.alumni_id.in_(ids))
            )
        )
        .scalars()
        .all()
    }
    employ = {
        e.alumni_id: e
        for e in (
            await session.execute(
                select(CurrentEmployment).where(CurrentEmployment.alumni_id.in_(ids))
            )
        )
        .scalars()
        .all()
    }

    recipients: list[Recipient] = []
    for a in alumni:
        contact = contacts.get(a.alumni_id)
        email = contact.personal_email if contact else None
        if not _is_sendable_email(email):
            continue
        job = employ.get(a.alumni_id)
        recipients.append(
            Recipient(
                alumni_id=a.alumni_id,
                first_name=(a.preferred_first_name or a.first_name or "there"),
                email=email,
                on_file=_build_on_file(a, contact, job),
            )
        )
    return recipients


class ResendRateLimited(Exception):
    """Resend returned 429 — we've hit its request/volume limit. ``retry_after``
    is the seconds Resend told us to wait (from its ``retry-after`` /
    ``ratelimit-reset`` header) before sending more. The LIMIT COMES FROM RESEND:
    we never hardcode a daily/monthly cap — we send until Resend says stop."""

    def __init__(self, retry_after: int) -> None:
        self.retry_after = retry_after
        super().__init__(f"Resend rate limit hit; retry after {retry_after}s")


def _int_header(response: httpx.Response, name: str) -> int | None:
    raw = response.headers.get(name)
    if raw is None:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


# Small pause between batches to stay under Resend's request rate limit
# (default ~10 req/s per team); the authoritative pace still comes from the
# ratelimit-* headers below.
_INTER_BATCH_DELAY_SECONDS = 0.15
_MAX_PACE_SLEEP_SECONDS = 5.0


async def _send_batch(emails: list[dict]) -> tuple[int | None, int | None]:
    """Send one ≤100-email batch. Returns ``(ratelimit_remaining, ratelimit_reset)``
    from Resend's response headers so the caller can pace itself. Raises
    :class:`ResendRateLimited` on 429 (honoring ``retry-after``) and
    :class:`ServiceError` on other failures. All pacing/stopping is driven by
    Resend's own headers — nothing here is a configured limit."""
    settings = get_settings()
    key = settings.resend_api_key
    if not key:
        raise ServiceError("Resend API key is not configured (RESEND_API_KEY).")
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT_SECONDS) as client:
            response = await client.post(
                _RESEND_BATCH_URL,
                headers={
                    "Authorization": f"Bearer {key}",
                    "Content-Type": "application/json",
                },
                json=emails,
            )
    except httpx.HTTPError as exc:
        raise ServiceError("Could not reach the email service.") from exc
    if response.status_code == 429:
        retry_after = (
            _int_header(response, "retry-after")
            or _int_header(response, "ratelimit-reset")
            or 1
        )
        log.warning("Resend rate limit (429); retry after %ss", retry_after)
        raise ResendRateLimited(retry_after)
    if not response.is_success:
        # Surface Resend's own reason (e.g. domain-not-verified, free-tier
        # recipient restriction) so the operator can fix it — these messages
        # are actionable and non-sensitive.
        detail = ""
        try:
            body = response.json()
            if isinstance(body, dict):
                detail = str(body.get("message") or body.get("error") or "").strip()
        except Exception:  # noqa: BLE001 - non-JSON error body
            detail = ""
        log.error(
            "Resend batch send failed: status=%s detail=%s",
            response.status_code,
            detail,
        )
        raise ServiceError(
            f"Resend rejected the send (HTTP {response.status_code})"
            + (f": {detail}" if detail else ".")
        )
    return (
        _int_header(response, "ratelimit-remaining"),
        _int_header(response, "ratelimit-reset"),
    )


def _survey_link(base_url: str, alumni_id: int, graduation_year: int) -> str:
    """The recipient's unique, signed confirm link."""
    token = make_survey_token(alumni_id, graduation_year)
    return f"{base_url.rstrip('/')}/survey/{token}"


def _build_survey_email(
    r: Recipient, *, graduation_year: int, base_url: str, from_field: str
) -> dict:
    """One Resend batch entry for a recipient (unique link + rendered content)."""
    link = _survey_link(base_url, r.alumni_id, graduation_year)
    subject, html, text = render_survey_email(r, link)
    return {
        "from": from_field,
        "to": [r.email],
        "subject": subject,
        "html": html,
        "text": text,
    }


async def send_recipients(
    recipients: list[Recipient],
    *,
    graduation_year: int,
    base_url: str,
    from_field: str,
    on_batch_sent: Callable[[list[Recipient]], Awaitable[None]] | None = None,
) -> tuple[int, int | None]:
    """Send the survey to a list of recipients, Resend-governed.

    Sends in ``_BATCH_MAX`` chunks via :func:`_send_batch`, pacing from Resend's
    ratelimit-* headers. Returns ``(sent, retry_after)`` — the count actually
    delivered and, if Resend rate-limited us (429), the seconds to wait before the
    rest can go (``None`` when not throttled). On a 429 it STOPS: the un-sent
    remainder is left for a later run. ``on_batch_sent`` is awaited with each
    successfully-sent chunk BEFORE pacing, so callers can durably record delivery
    (the scheduler logs each recipient) so a crash/throttle never re-emails them.

    The limit is Resend's, discovered from its response — never a configured cap.
    Shared by :func:`send_campaign` (manual send) and the send scheduler.
    """
    sent = 0
    retry_after: int | None = None
    try:
        for chunk in _chunks(recipients, _BATCH_MAX):
            emails = [
                _build_survey_email(
                    r,
                    graduation_year=graduation_year,
                    base_url=base_url,
                    from_field=from_field,
                )
                for r in chunk
            ]
            remaining, reset = await _send_batch(emails)
            sent += len(chunk)
            if on_batch_sent is not None:
                await on_batch_sent(chunk)
            # Pace from Resend's own headers: if the window is exhausted wait for
            # its reset, otherwise a small gap to stay under the req/s cap.
            if remaining is not None and remaining <= 0 and reset:
                await asyncio.sleep(min(reset, _MAX_PACE_SLEEP_SECONDS))
            else:
                await asyncio.sleep(_INTER_BATCH_DELAY_SECONDS)
    except ResendRateLimited as exc:
        # Hit Resend's limit — stop; the rest is left for a later run.
        retry_after = exc.retry_after
    return sent, retry_after


async def send_campaign(
    session: AsyncSession,
    *,
    graduation_year: int,
    actor_user_id: int | None,
    dry_run: bool = True,
    limit: int | None = None,
) -> SurveySendResult:
    """Build and (unless dry_run) send the survey to a graduation year.

    Attempts EVERY eligible recipient (or the first `limit` if given) and lets
    RESEND'S rate limit decide how many actually go: on a 429 it stops, reports
    `retry_after_seconds`, and leaves the rest as `remaining` for a later run.
    No daily/monthly cap is configured here — the limit comes from Resend.
    Writes an audit row and commits.
    """
    settings = get_settings()
    base_url = settings.survey_app_base_url
    from_email = settings.survey_from_email
    if not base_url:
        raise ServiceError("SURVEY_APP_BASE_URL is not configured.")
    if not from_email:
        raise ServiceError("SURVEY_FROM_EMAIL is not configured.")

    recipients = await _load_recipients(session, graduation_year)
    # The cap comes from RESEND, not our config: attempt everyone unless the
    # caller passes an explicit `limit`. Resend's 429 stops us at the plan's real
    # daily/monthly limit, and the un-sent remainder is reported for a later run
    # (the scheduler resumes it once `retry_after` has elapsed).
    to_send = recipients[: max(limit, 0)] if limit is not None else recipients
    from_field = f"{settings.survey_from_name} <{from_email}>"
    # Every eligible recipient builds exactly one email, so prepared == len(to_send).
    prepared = len(to_send)

    sent = 0
    retry_after: int | None = None
    if not dry_run and to_send:
        sent, retry_after = await send_recipients(
            to_send,
            graduation_year=graduation_year,
            base_url=base_url,
            from_field=from_field,
        )

    remaining = len(recipients) - len(to_send) if dry_run else len(recipients) - sent
    session.add(
        AuditLog(
            user_id=actor_user_id,
            action_type="send_survey" if not dry_run else "send_survey_dry_run",
            entity_type="survey_campaign",
            entity_id=graduation_year,
            new_value=(
                f"grad_year={graduation_year} recipients={len(recipients)} "
                f"prepared={prepared} sent={sent} dry_run={dry_run}"
                + (f" throttled_retry_after={retry_after}" if retry_after else "")
            ),
        )
    )
    await session.commit()

    return SurveySendResult(
        graduation_year=graduation_year,
        total_recipients=len(recipients),
        prepared=prepared,
        sent=sent,
        remaining=remaining,
        dry_run=dry_run,
        retry_after_seconds=retry_after,
        sample=[
            SurveySendSample(
                email=r.email,
                link=_survey_link(base_url, r.alumni_id, graduation_year),
            )
            for r in to_send[:3]
        ],
    )
