"""Survey-email send service (Resend).

Sends the annual "confirm your info" survey to every eligible alum in a
graduation year. Each recipient gets a personalized email with a UNIQUE,
HMAC-signed link to `<SURVEY_APP_BASE_URL>/survey/<token>`; the token carries the
alum's id so the landing page can (later) load their record and save edits.

Design notes:
- There is ONE way to send: `send_survey_stage`. It owns choosing the stage,
  working out who is owed it, claiming them in `survey_send_log`, calling Resend,
  writing the audit row and committing — for BOTH the console's manual send and
  the daily cron. `_send_batch` is private; nothing outside this module can email
  an alum without recording it. The manual send previously called the raw sender
  without the send-log callback, which is what let a whole cohort be emailed with
  no record and then emailed again by the cron two days later (2026-08-02).
- Delivery is CLAIMED BEFORE IT IS SENT (insert + commit, then Resend). Emailing
  is irreversible and a log row is not, so this fails toward "possibly missed"
  rather than "sent twice".
- `survey_send_log` is the usage ledger (`get_send_usage`), not the audit trail —
  an engineer actor's audit row is rerouted to `engineer_action_log` and would
  read as zero usage.
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
from dataclasses import dataclass
from html import escape

import httpx
from sqlalchemy import delete, func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.dropdowns import SUPPRESSED_CONTACT_STATUS_LABELS, holds_designation
from app.core.errors import ServiceError
from app.models.alumni import Alumni
from app.models.audit import AuditLog
from app.models.contact import AlumniContactInfo
from app.models.employment import CurrentEmployment
from app.models.engagement import AlumniProgramEngagement
from app.models.survey_response import SurveyResponse
from app.models.survey_schedule import SurveySchedule, SurveySendLog
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

# The survey is ANNUAL: once an alum replies, they're not re-surveyed for a year.
# A response submitted on/after this cutoff counts as "already surveyed this
# cycle" — used both to exclude them from a send and to count real replies.
_RESURVEY_INTERVAL_DAYS = 365

# Which `survey_responses.status` values count as "they replied this cycle".
#
# `rejected` is DELIBERATELY absent. A rejected response is one staff THREW AWAY
# (spam, junk, someone else's data) — nothing was written to the record, so the
# alum has effectively not replied and must stay surveyable. Counting it both
# silenced them for 365 days and reported them to the console as "replied", i.e.
# complete. This tuple is the single definition, shared by the send exclusion
# (:func:`_load_recipients`) and the console's responded tally
# (:func:`list_graduation_years`) — they must never drift.
RESPONDED_STATUSES: tuple[str, ...] = ("pending", "applied")

# --------------------------------------------------------------- send stages --
#
# A campaign sends in three stages: the initial email, then a 1-week and a 2-week
# reminder to whoever has not replied. `survey_send_log` is UNIQUE on
# (graduation_year, alumni_id, stage, cycle_seq), so the stage is what makes
# "have we already emailed this person?" answerable WITHIN a campaign, and
# `cycle_seq` is what keeps that question about THIS campaign rather than all
# time (#357) — and it is why a manual send must
# pick a REAL stage rather than a synthetic "manual" one: a `stage = -1` would
# satisfy the unique constraint alongside a stage-0 cron row and let the same
# alum be emailed twice, which is exactly the incident this all exists to stop.
#
# These live here, not in `survey_schedule`, because they describe the send log —
# which both the scheduled and the manual sender now write. `survey_schedule`
# re-exports them so its own callers/tests are unaffected.
STAGE_INITIAL = 0
STAGE_REMINDER_1 = 1
STAGE_REMINDER_2 = 2
_STAGE_WINDOW_DAYS = 7  # each stage covers a 7-day window from start_date


def stage_for(elapsed_days: int) -> int | None:
    """Which stage's WINDOW ``elapsed_days`` after the start falls in.

    0 for the first week, 1 for the second, 2 for the third; ``None`` once the
    2-week-reminder window has passed.

    ``None`` means "the calendar has run out of windows" — it does NOT mean the
    campaign is finished. Completion is decided from what has actually been
    delivered (see :func:`select_stage_targets` and
    ``survey_schedule.run_due_schedules``); this function only ever CAPS which
    stage may go out. Treating its ``None`` as "done" is what silently completed
    cohorts that had never been emailed at all."""
    if elapsed_days < 0:
        return None  # not started yet
    stage = elapsed_days // _STAGE_WINDOW_DAYS
    return stage if stage <= STAGE_REMINDER_2 else None


def ceiling_stage_for(elapsed_days: int) -> int:
    """The HIGHEST stage that may be sent ``elapsed_days`` after the start.

    Same windows as :func:`stage_for`, but total: before the start only the
    initial is permitted, and once every window has gone by all three stages are
    permitted so stragglers from any stage can still be finished."""
    if elapsed_days < 0:
        return STAGE_INITIAL
    window = stage_for(elapsed_days)
    return STAGE_REMINDER_2 if window is None else window


def _resurvey_cutoff() -> datetime.datetime:
    return datetime.datetime.now(datetime.UTC) - datetime.timedelta(
        days=_RESURVEY_INTERVAL_DAYS
    )


FIRST_CYCLE = 1
"""The cycle a year is on before anyone has started a second campaign for it —
and the cycle a send belongs to when the year has no schedule row at all (a
manual console send for an unscheduled year). Matches the migration's backfill,
so pre-#357 log rows and a fresh manual send agree."""


async def current_cycle_seq(session: AsyncSession, graduation_year: int) -> int:
    """The campaign cycle a send for this year belongs to right now (#357).

    Read from the year's ``survey_schedule`` row, which is the only thing that
    ever advances it. A year with no schedule has never had a cycle started, so
    its manual sends belong to cycle 1 — the same cycle the migration backfilled
    onto existing rows, so a manual send before and after this change lands in
    the same bucket."""
    seq = (
        await session.execute(
            select(SurveySchedule.cycle_seq).where(
                SurveySchedule.graduation_year == graduation_year
            )
        )
    ).scalar_one_or_none()
    return seq if seq is not None else FIRST_CYCLE


async def logged_alumni_ids(
    session: AsyncSession, graduation_year: int, stage: int, cycle_seq: int
) -> set[int]:
    """alumni_ids already emailed for (year, stage, cycle) — the double-send guard.

    ``cycle_seq`` is NOT optional and must not be defaulted: an unscoped read
    here is exactly the #357 bug (the guard becomes an all-time question, so a
    second campaign for the year finds everyone already logged and emails
    nobody)."""
    stmt = select(SurveySendLog.alumni_id).where(
        SurveySendLog.graduation_year == graduation_year,
        SurveySendLog.stage == stage,
        SurveySendLog.cycle_seq == cycle_seq,
    )
    return set((await session.execute(stmt)).scalars().all())


async def get_send_usage(session: AsyncSession) -> SurveyUsage:
    """Real send usage for the console tallies: emails actually sent today and
    this calendar month, counted from ``survey_send_log``.

    The ledger is the send log, NOT the audit trail. Audit rows are still written
    for every send, but they cannot be counted on: an ENGINEER actor's
    ``AuditLog`` is rerouted into ``engineer_action_log`` by the audit hook
    (#199), so an engineer's manual send left the meter reading zero and the
    scheduler handed out a budget that was already spent. The send log has no
    such hole — one row per email, inserted and committed in the same
    transaction as the delivery claim, by both senders.

    Day/month boundaries are UTC, matching the app's other date filtering. Dry
    runs never claim, so they never appear here."""
    settings = get_settings()
    anchor = settings.survey_usage_baseline_at
    now = datetime.datetime.now(datetime.UTC)
    start_today = datetime.datetime.combine(
        now.date(), datetime.time.min, tzinfo=datetime.UTC
    )
    start_month = start_today.replace(day=1)
    stmt = select(
        func.count().label("month"),
        func.count()
        .filter(SurveySendLog.sent_at >= start_today)
        .label("today"),
    ).where(SurveySendLog.sent_at >= start_month)
    if anchor is not None:
        # Baseline set: the baseline covers everything up to the anchor, so only
        # count sends recorded strictly AFTER it (avoids double-counting).
        stmt = stmt.where(SurveySendLog.sent_at > anchor)
    row = (await session.execute(stmt)).first()
    sent_this_month, sent_today = (row[0] or 0, row[1] or 0) if row else (0, 0)
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


# How long a minted survey link stays usable (#360).
#
# SEVEN DAYS, because that is exactly the reminder cadence: stage 0 goes out on
# day 0, stage 1 on day 7, stage 2 on day 14 (``_STAGE_WINDOW_DAYS``). A token is
# minted at the moment its email is built (``_build_survey_email`` ->
# ``_survey_link``), i.e. at send time, so "7 days from issue" means each link
# stays valid right up to the moment the next reminder issues a fresh one that
# supersedes it. That alignment IS the design (Jake, 2026-08-03) — do not raise
# this number without moving the cadence with it.
#
# Before this, the token was a bare HMAC over ``alumni_id.graduation_year`` with
# no time component at all: a forwarded email, a mailing-list archive or a shared
# browser history handed the holder a PERMANENT read of that alum's employment,
# both emails, phone, spouse, birth date and citizenship — plus the matching
# write — and the only revocation was rotating SURVEY_TOKEN_SECRET, which kills
# every outstanding link at once.
SURVEY_TOKEN_TTL_SECONDS = 7 * 24 * 60 * 60

# Tokens minted BEFORE this change carry no issued-at (payload is two fields, not
# three). They cannot be dated, so they are given one fixed, shared deadline
# rather than an unbounded life: the same 7 days, measured from the day the fix
# ships. After it passes, every legacy link is dead for good.
#
# A hardcoded instant is deliberate. Anchoring the grace to process start would
# be worse than useless on a serverless deploy — every cold start would restart
# the 7 days, so legacy tokens would live forever. If the deploy slips past this
# date the grace is simply already over, which is the safe direction to fail.
_LEGACY_TOKEN_VALID_UNTIL = datetime.datetime(2026, 8, 11, tzinfo=datetime.UTC)


# The ONE message every token failure gets — malformed, forged, or simply past
# its 7 days. Deliberately does NOT say which: telling a prober "expired" rather
# than "invalid" confirms the token was once a real alum's credential.
#
# It still has to be useful to the person it is actually shown to, so it names
# the lifetime, says a replacement is coming, and gives a way out that does not
# depend on waiting. Lives here, next to the rule it describes, and is used by
# every public entry point (`api/routes/survey.py`, `survey_responses.py`) so the
# three routes can never drift into telling an alum three different stories.
LINK_DEAD_MESSAGE = (
    "This survey link is no longer usable. Survey links expire seven days after "
    "they are sent. If our survey is still running you will receive a fresh link "
    "in your next reminder email — please use the most recent email we sent you. "
    "If you would rather not wait, contact the BYU Finance department and we will "
    "send you a new one."
)


def _now() -> datetime.datetime:
    return datetime.datetime.now(datetime.UTC)


def make_survey_token(
    alumni_id: int,
    graduation_year: int,
    *,
    issued_at: datetime.datetime | None = None,
) -> str:
    """A stateless, tamper-evident, EXPIRING token: `<b64(payload)>.<b64(hmac)>`.

    The payload is ``alumni_id.graduation_year.issued_at`` (issued_at = whole
    UTC seconds). The expiry rides INSIDE the signed payload rather than beside
    it, because there is no token row anywhere — the link is the whole credential
    — so anything not covered by the HMAC would be editable by the recipient.
    Signing the issue time makes "when was this minted?" as unforgeable as "whose
    record is this?".

    ``issued_at`` is injectable for tests only; callers mint at send time.
    """
    issued = int((issued_at or _now()).timestamp())
    payload = f"{alumni_id}.{graduation_year}.{issued}".encode()
    sig = hmac.new(_token_secret().encode("utf-8"), payload, hashlib.sha256).digest()
    return f"{_b64(payload)}.{_b64(sig)}"


def verify_survey_token(token: str, *, now: datetime.datetime | None = None) -> int | None:
    """Return the alumni_id if the token is valid, untampered AND unexpired.

    ``None`` covers every failure — malformed, wrong signature, expired — and the
    callers turn all of them into the SAME 404 message. That is deliberate:
    distinguishing "expired" from "never existed" would confirm to a prober that
    a given token was once a real alum's credential.

    Editing the issued-at is not a way in: it is inside the signed payload, so a
    changed timestamp fails ``compare_digest`` and looks identical to garbage.
    """
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
        parts = payload.decode("utf-8").split(".")
    except UnicodeDecodeError:
        return None

    moment = now or _now()
    if len(parts) == 2:
        # Legacy (pre-#360) token: signed, but undatable. Honour it only until the
        # shared cutoff, so a link already sitting in an alum's inbox still works
        # for its final week instead of dying the second this deploys.
        if moment >= _LEGACY_TOKEN_VALID_UNTIL:
            log.info("Survey token rejected: legacy token past the cutover grace")
            return None
    elif len(parts) == 3:
        try:
            issued = datetime.datetime.fromtimestamp(int(parts[2]), datetime.UTC)
        except (ValueError, OverflowError, OSError):
            return None
        age = (moment - issued).total_seconds()
        if age > SURVEY_TOKEN_TTL_SECONDS:
            # Age only — never the token, the alumni_id or the email address.
            log.info("Survey token rejected: expired (age %.1f days)", age / 86400)
            return None
    else:
        return None

    try:
        return int(parts[0])
    except ValueError:
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
    put("program.cpa_designation", _held(getattr(eng, "cpa_designation", None)))
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
    # year. Uses the SAME status filter as the re-survey exclusion in
    # _load_recipients (:data:`RESPONDED_STATUSES`), so "responded" really does
    # mean "already surveyed this cycle". A REJECTED response is neither — staff
    # threw it away, so the alum is still surveyable and must not be reported to
    # the console as complete.
    responded_stmt = (
        select(
            SurveyResponse.graduation_year,
            func.count(func.distinct(SurveyResponse.alumni_id)).label("responded"),
        )
        .where(
            SurveyResponse.graduation_year.is_not(None),
            SurveyResponse.submitted_at >= _resurvey_cutoff(),
            SurveyResponse.status.in_(RESPONDED_STATUSES),
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


def eligible_alumni_query(graduation_year: int):
    """The SINGLE definition of "who may be emailed the survey for this year".

    Everything is a SQL-level predicate (correlated EXISTS / NOT EXISTS) so it
    runs in Postgres over the whole 8,000+ row table — nothing is filtered in
    Python. Exposed (not private) so a future "who would receive this?" preview
    can render the identical population rather than re-deriving it.

    On top of ``build_alumni_query``'s defaults (is_alumni, not archived):

    * **Not deceased** — ``alumni.deceased`` is the flag; the column is NOT NULL
      so ``deceased=False`` is total.
    * **Not suppressed** — no ``Deceased`` / ``Do Not Contact`` status label
      (:data:`SUPPRESSED_CONTACT_STATUS_LABELS`). The deceased flag and the
      Deceased LABEL are separate columns that are set independently, so both
      must be checked: on dev, alumni carry both and only the ``@example.com``
      placeholder filter was accidentally stopping them. On prod, with real
      addresses, a "confirm your information" email listing a dead person's full
      record goes to a live inbox — in practice, their spouse's.
      ``Lost Contact`` / ``Retired`` / ``Inactive`` stay ELIGIBLE by design
      (Jake, 2026-08-03): "lost contact" means we want to reconnect, and this
      survey is the tool for it.
    * **Has a personal email** — there is nothing to send to otherwise.
    * **Has not replied this cycle** — a `pending` or `applied` response within
      365 days. A `rejected` one does NOT count (see
      :data:`RESPONDED_STATUSES`).

    Ordered by ``alumni_id`` so that ANY truncation (a ``limit``, or the daily
    send budget) takes a stable, reproducible prefix. ``build_alumni_query`` has
    no ORDER BY of its own, so without this an interrupted send resumed on an
    arbitrary subset and "run Send again to continue" was not a true statement.
    """
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
            SurveyResponse.status.in_(RESPONDED_STATUSES),
        )
        .exists()
    )
    return (
        build_alumni_query(
            graduation_year=graduation_year,
            deceased=False,
            suppress_labels=SUPPRESSED_CONTACT_STATUS_LABELS,
        )
        .where(has_personal_email)
        .where(~replied_recently)
        .order_by(Alumni.alumni_id)
    )


def dedupe_by_email(
    recipients: list[Recipient],
) -> tuple[list[Recipient], list[Recipient]]:
    """Split ``recipients`` into (one per address, the collisions dropped).

    Two alumni rows can carry the same personal email — spouses sharing a
    household address, an address reassigned to a new owner, a data-entry slip,
    or a genuine duplicate record. Without this, EACH of them is emailed
    separately, and every message contains ~19 fields of that alum's record
    (both emails, spouse names, residence, employer, title, LinkedIn) plus a
    live signed token that lets the holder EDIT that record. One inbox would
    receive several people's profiles with write access to all of them.

    The keeper is chosen deterministically — the input is ordered by
    ``alumni_id``, so it is the lowest id — which makes a re-run pick the same
    person rather than rotating whose record leaks. The dropped ones are
    returned, not silently discarded, so the caller can surface the collision.
    """
    seen: dict[str, Recipient] = {}
    kept: list[Recipient] = []
    dropped: list[Recipient] = []
    for r in recipients:
        key = (r.email or "").strip().lower()
        if key in seen:
            dropped.append(r)
            continue
        seen[key] = r
        kept.append(r)
    return kept, dropped


async def _load_recipients(session: AsyncSession, graduation_year: int) -> list[Recipient]:
    """Eligible alumni for a grad year (see :func:`eligible_alumni_query`), with
    the on-file fields the email previews. Side tables are bulk-loaded once (no
    N+1), mirroring the CSV export."""
    alumni = (await session.execute(eligible_alumni_query(graduation_year))).scalars().all()
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
    we never hardcode a daily/monthly cap — we send until Resend says stop.

    A 429 is a DEFINITIVE rejection: Resend refused the call, so not one email in
    that batch was queued. The claim for it is therefore released (see
    :func:`_release_claim`) and those recipients go out on the next run."""

    def __init__(self, retry_after: int) -> None:
        self.retry_after = retry_after
        super().__init__(f"Resend rate limit hit; retry after {retry_after}s")


class ResendDeliveryUnknown(ServiceError):
    """The batch left us but we never learned its fate — a transport error or a
    timeout, which can fire AFTER Resend accepted and queued the emails.

    This is the one failure we cannot undo, and it is why the sender claims
    before it sends: the claim stays, so those recipients are treated as
    possibly-delivered and are never emailed a second time. A non-2xx RESPONSE is
    different — Resend answered, and answered no — so that raises a plain
    :class:`ServiceError` and the claim is released."""


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
        # AMBIGUOUS: the request may have reached Resend and been queued before
        # the connection died. Distinct exception type so the caller keeps the
        # claim rather than releasing it (see :class:`ResendDeliveryUnknown`).
        raise ResendDeliveryUnknown("Could not reach the email service.") from exc
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
    """The recipient's unique, signed confirm link.

    Minted here, at send time, so the token's issued-at IS its sent-at and the
    7-day life (:data:`SURVEY_TOKEN_TTL_SECONDS`) runs out exactly as the next
    reminder issues its replacement."""
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


# ------------------------------------------------ stage + target selection ---


async def select_stage_targets(
    session: AsyncSession,
    *,
    graduation_year: int,
    recipients: list[Recipient],
    max_stage: int,
    cycle_seq: int,
) -> tuple[int | None, list[Recipient]]:
    """Pick the stage this send should cover, and who still needs it.

    Returns the LOWEST stage ``s <= max_stage`` that still has recipients with no
    ``survey_send_log`` row, together with those recipients — or ``(None, [])``
    when every permitted stage has been fully delivered.

    ``max_stage`` is a CEILING derived from the calendar
    (:func:`ceiling_stage_for`), never a target: a stage is sent because someone
    is owed it, not because today is its window.

    Two bugs this shape fixes:

    * The initial used to be the only stage with a "finish it regardless of the
      window" rule. A reminder window that could not drain — cap-throttled, or a
      cron run missed inside its 7 days — abandoned its stragglers permanently,
      because the next run only ever looked at the CURRENT window. Scanning
      upward from 0 means an unfinished stage is always picked up.
    * ``(None, [])`` is the only honest basis for completing a campaign. Deciding
      it from ``elapsed >= 21`` alone completed cohorts that had received zero
      emails.

    Reminders reach only initial-recipients without needing a separate check: we
    only reach the loop when stage 0 has no targets left, which means every
    remaining recipient is already logged for stage 0.

    Every log read here is scoped to ``cycle_seq`` (#357), so "already emailed"
    means "already emailed IN THIS CAMPAIGN". Unscoped, a year's second campaign
    saw last year's rows, found no targets at any stage, and completed having
    emailed nobody.
    """
    logged_initial = await logged_alumni_ids(
        session, graduation_year, STAGE_INITIAL, cycle_seq
    )
    initial_targets = [r for r in recipients if r.alumni_id not in logged_initial]
    if initial_targets:
        return STAGE_INITIAL, initial_targets
    for stage in range(STAGE_REMINDER_1, max_stage + 1):
        already = await logged_alumni_ids(session, graduation_year, stage, cycle_seq)
        targets = [r for r in recipients if r.alumni_id not in already]
        if targets:
            return stage, targets
    return None, []


async def campaign_max_stage(session: AsyncSession, graduation_year: int) -> int:
    """The ceiling stage a MANUAL send for this year may record.

    A manual send has no stage of its own — there is one email template — but
    ``survey_send_log`` is UNIQUE on (year, alumni, stage), so it must record a
    REAL one. It is resolved exactly the way the cron resolves it: from the
    year's ``survey_schedule`` row if there is one, else stage 0 only. Inventing
    a synthetic ``stage = -1`` would let the unique constraint hold while the
    same alum received both a manual email and a stage-0 cron email — the very
    incident this is fixing.
    """
    start_date = (
        await session.execute(
            select(SurveySchedule.start_date).where(
                SurveySchedule.graduation_year == graduation_year
            )
        )
    ).scalar_one_or_none()
    if start_date is None:
        return STAGE_INITIAL
    elapsed = (datetime.datetime.now(datetime.UTC).date() - start_date).days
    return ceiling_stage_for(elapsed)


# ----------------------------------------------------- claim-then-send core ---


async def _claim_batch(
    session: AsyncSession,
    *,
    graduation_year: int,
    stage: int,
    cycle_seq: int,
    batch: list[Recipient],
) -> list[Recipient]:
    """Reserve ``batch`` in ``survey_send_log`` and COMMIT, before sending.

    ``ON CONFLICT DO NOTHING ... RETURNING alumni_id`` makes the reservation
    atomic: only rows this statement actually inserted come back, so anyone
    already logged (an earlier run, a concurrent one) is simply not ours to send
    to and drops out of the batch.

    Claiming BEFORE the Resend call is deliberate. Emailing is irreversible and
    the log row is not, so the two orderings fail in opposite directions:
    send-then-log loses the record on any failure in between and RE-EMAILS those
    people on the next run (this is what produced the unscheduled Sunday send);
    claim-then-send can at worst skip someone. For an irreversible side effect,
    "possibly missed" is the correct way to fail — a missed alum can be found and
    re-sent deliberately, a duplicate cannot be recalled.
    """
    stmt = (
        pg_insert(SurveySendLog)
        .values(
            [
                {
                    "graduation_year": graduation_year,
                    "alumni_id": r.alumni_id,
                    "stage": stage,
                    "cycle_seq": cycle_seq,
                }
                for r in batch
            ]
        )
        .on_conflict_do_nothing(
            constraint="uq_survey_send_log_year_alumni_stage"
        )
        .returning(SurveySendLog.alumni_id)
    )
    claimed = set((await session.execute(stmt)).scalars().all())
    await session.commit()
    return [r for r in batch if r.alumni_id in claimed]


async def _release_claim(
    session: AsyncSession,
    *,
    graduation_year: int,
    stage: int,
    cycle_seq: int,
    batch: list[Recipient],
) -> None:
    """Undo a claim Resend DEFINITIVELY refused (a 429, or a non-2xx answer).

    Only rows :func:`_claim_batch` itself inserted are ever passed here — an
    ``ON CONFLICT DO NOTHING`` never returns a pre-existing row — so this can
    never delete the record of a genuinely delivered email.

    ``cycle_seq`` is part of that guarantee, not decoration (#357). Without it
    this DELETE matches (year, alumni, stage) across EVERY cycle, so releasing a
    throttled claim in cycle 2 would silently delete the cycle-1 row recording an
    email that really was delivered last year — destroying send history and
    making that alum re-sendable in the earlier cycle's accounting.
    """
    if not batch:
        return
    await session.execute(
        delete(SurveySendLog).where(
            SurveySendLog.graduation_year == graduation_year,
            SurveySendLog.stage == stage,
            SurveySendLog.cycle_seq == cycle_seq,
            SurveySendLog.alumni_id.in_([r.alumni_id for r in batch]),
        )
    )
    await session.commit()


async def _send_and_log(
    session: AsyncSession,
    recipients: list[Recipient],
    *,
    graduation_year: int,
    stage: int,
    cycle_seq: int,
    base_url: str,
    from_field: str,
) -> tuple[int, int | None, ServiceError | None]:
    """Claim, send and durably record ``recipients`` in ``_BATCH_MAX`` chunks.

    Returns ``(sent, retry_after, error)``. Every chunk is claimed and committed
    before its Resend call, so what has been delivered is already durable when
    anything goes wrong — a crash, a throttle or a transport failure can no
    longer lose the send log and cause a re-send.

    Stops at the first failure, and unwinds according to what the failure tells
    us: a 429 or a non-2xx answer means Resend queued nothing, so the claim is
    released and those recipients go out next run; a transport failure means we
    do not know, so the claim STAYS and they are treated as delivered.

    This is the only place either sender reaches Resend — ``_send_batch`` is
    private and nothing outside this module can send without logging again.
    """
    sent = 0
    retry_after: int | None = None
    error: ServiceError | None = None
    for chunk in _chunks(recipients, _BATCH_MAX):
        claimed = await _claim_batch(
            session,
            graduation_year=graduation_year,
            stage=stage,
            cycle_seq=cycle_seq,
            batch=chunk,
        )
        if not claimed:
            continue  # someone else already owns this (year, alumni, stage, cycle)
        emails = [
            _build_survey_email(
                r,
                graduation_year=graduation_year,
                base_url=base_url,
                from_field=from_field,
            )
            for r in claimed
        ]
        try:
            remaining, reset = await _send_batch(emails)
        except ResendRateLimited as exc:
            await _release_claim(
                session,
                graduation_year=graduation_year,
                stage=stage,
                cycle_seq=cycle_seq,
                batch=claimed,
            )
            retry_after = exc.retry_after
            break
        except ResendDeliveryUnknown as exc:
            # Ambiguous — keep the claim. See :class:`ResendDeliveryUnknown`.
            log.error(
                "Survey send outcome unknown for %s recipients "
                "(grad_year=%s stage=%s); claim kept, they will NOT be retried",
                len(claimed),
                graduation_year,
                stage,
            )
            error = exc
            break
        except ServiceError as exc:
            # Resend answered, and answered no — nothing was queued.
            await _release_claim(
                session,
                graduation_year=graduation_year,
                stage=stage,
                cycle_seq=cycle_seq,
                batch=claimed,
            )
            error = exc
            break
        sent += len(claimed)
        # Pace from Resend's own headers: if the window is exhausted wait for
        # its reset, otherwise a small gap to stay under the req/s cap.
        if remaining is not None and remaining <= 0 and reset:
            await asyncio.sleep(min(reset, _MAX_PACE_SLEEP_SECONDS))
        else:
            await asyncio.sleep(_INTER_BATCH_DELAY_SECONDS)
    return sent, retry_after, error


# ------------------------------------------------- the one send entry point ---


@dataclass(frozen=True)
class SendOutcome:
    """What one call to :func:`send_survey_stage` did."""

    graduation_year: int
    # The stage sent, or None when nothing was owed at any permitted stage.
    stage: int | None
    eligible: list[Recipient]  # everyone eligible this year, deduped
    targets: list[Recipient]  # of those, not yet logged for `stage`
    prepared: list[Recipient]  # targets after the limit / send budget
    sent: int
    retry_after: int | None = None
    duplicate_emails: int = 0


async def send_survey_stage(
    session: AsyncSession,
    *,
    graduation_year: int,
    max_stage: int,
    actor_user_id: int | None,
    dry_run: bool = False,
    limit: int | None = None,
    recipients: list[Recipient] | None = None,
    scheduled: bool = False,
    cycle_seq: int | None = None,
) -> SendOutcome:
    """Send one stage of a year's survey — the ONLY way either caller sends.

    Owns the whole irreversible step end to end: choose the stage, work out who
    is owed it, claim them, send, record the audit trail and commit. Both the
    manual console send (:func:`send_campaign`) and the daily cron
    (``survey_schedule.run_due_schedules``) go through here.

    That is the point. The unscheduled second send of 2026-08-02 happened
    because the manual path called the raw sender WITHOUT the callback that
    writes ``survey_send_log`` — it emailed a whole cohort and recorded nothing,
    so the cron saw a cohort that had never had its initial and sent it again.
    The fix is not to pass the callback at that one call site (which leaves the
    same trap for the next caller) but to make it impossible to send without
    recording: the raw sender is private, and logging is not optional here.

    ``recipients`` may be pre-loaded by the caller purely to avoid re-running the
    cohort query the cron already ran; when omitted they are loaded here. Either
    way they come from :func:`_load_recipients`, so "who is eligible" has exactly
    one implementation.

    ``limit`` is the caller's own ceiling (the console's explicit limit, or the
    scheduler's shared daily budget). Resend's 429 is still the real brake.
    """
    settings = get_settings()
    base_url = settings.survey_app_base_url
    from_email = settings.survey_from_email
    if not base_url:
        raise ServiceError("SURVEY_APP_BASE_URL is not configured.")
    if not from_email:
        raise ServiceError("SURVEY_FROM_EMAIL is not configured.")
    from_field = f"{settings.survey_from_name} <{from_email}>"

    if recipients is None:
        recipients = await _load_recipients(session, graduation_year)
    eligible, duplicates = dedupe_by_email(recipients)
    if duplicates:
        # Visible to staff, not silent: each of these alumni shares an inbox with
        # someone we ARE emailing, and the email carries an edit token.
        log.warning(
            "Survey %s: %s alumni share an email address with another recipient "
            "and were skipped (alumni_ids=%s)",
            graduation_year,
            len(duplicates),
            ",".join(str(r.alumni_id) for r in duplicates[:20]),
        )

    # Which campaign this send belongs to (#357). Resolved ONCE and threaded
    # through target selection, claiming and any release, so a single send can
    # never straddle two cycles — re-reading it per batch would let a concurrent
    # "start new cycle" split one send across both.
    #
    # The cron passes the cycle it already read off the schedule row it is
    # iterating; re-resolving it here would make the completion check and the
    # send that follows it two independent reads that can disagree. Only the
    # manual path, which has no schedule in hand, looks it up.
    if cycle_seq is None:
        cycle_seq = await current_cycle_seq(session, graduation_year)

    stage, targets = await select_stage_targets(
        session,
        graduation_year=graduation_year,
        recipients=eligible,
        max_stage=max_stage,
        cycle_seq=cycle_seq,
    )
    prepared = [] if stage is None else targets
    if limit is not None:
        prepared = prepared[: max(limit, 0)]

    sent = 0
    retry_after: int | None = None
    error: ServiceError | None = None
    if not dry_run and prepared and stage is not None:
        sent, retry_after, error = await _send_and_log(
            session,
            prepared,
            graduation_year=graduation_year,
            stage=stage,
            cycle_seq=cycle_seq,
            base_url=base_url,
            from_field=from_field,
        )

    # The audit row is the TRAIL, not the ledger — usage is counted from
    # `survey_send_log` (see `get_send_usage`), which an engineer actor's
    # rerouted audit row cannot silently zero out.
    session.add(
        AuditLog(
            user_id=actor_user_id,
            action_type="send_survey_dry_run" if dry_run else "send_survey",
            entity_type="survey_campaign",
            entity_id=graduation_year,
            new_value=(
                f"grad_year={graduation_year} stage={stage} cycle={cycle_seq} "
                f"scheduled={scheduled} recipients={len(eligible)} "
                f"targets={len(targets)} prepared={len(prepared)} "
                f"sent={sent} dry_run={dry_run}"
                + (f" duplicate_emails={len(duplicates)}" if duplicates else "")
                + (f" throttled_retry_after={retry_after}" if retry_after else "")
                + (" failed=True" if error is not None else "")
            ),
        )
    )
    await session.commit()

    if error is not None and sent == 0:
        # Nothing went out and something is genuinely wrong (bad key, unverified
        # domain, network). The trail is committed; surface it rather than
        # reporting a clean "sent 0". When SOME batches did land we stay quiet
        # and report the partial send — that accounting is now durable.
        raise error

    return SendOutcome(
        graduation_year=graduation_year,
        stage=stage,
        eligible=eligible,
        targets=targets,
        prepared=prepared,
        sent=sent,
        retry_after=retry_after,
        duplicate_emails=len(duplicates),
    )


async def send_campaign(
    session: AsyncSession,
    *,
    graduation_year: int,
    actor_user_id: int | None,
    dry_run: bool = True,
    limit: int | None = None,
) -> SurveySendResult:
    """The console's manual send for a graduation year.

    A thin shell over :func:`send_survey_stage`: it resolves the stage ceiling
    from the year's schedule (:func:`campaign_max_stage`) and translates the
    outcome into the console's result shape. Everything that matters — who is
    eligible, who has already had this stage, the send log, the audit row, the
    commit boundary — is the shared helper's, identical to the cron's.
    """
    settings = get_settings()
    base_url = settings.survey_app_base_url
    max_stage = await campaign_max_stage(session, graduation_year)
    outcome = await send_survey_stage(
        session,
        graduation_year=graduation_year,
        max_stage=max_stage,
        actor_user_id=actor_user_id,
        dry_run=dry_run,
        limit=limit,
    )
    # What is left of THIS stage: the targets we did not send to. (Before, this
    # was measured against every eligible alum, which counted people who had
    # already received this exact email as still owed it.)
    done = len(outcome.prepared) if dry_run else outcome.sent
    remaining = len(outcome.targets) - done
    return SurveySendResult(
        graduation_year=graduation_year,
        total_recipients=len(outcome.eligible),
        prepared=len(outcome.prepared),
        sent=outcome.sent,
        remaining=remaining,
        dry_run=dry_run,
        retry_after_seconds=outcome.retry_after,
        sample=[
            SurveySendSample(
                email=r.email,
                link=_survey_link(base_url, r.alumni_id, graduation_year),
            )
            for r in outcome.prepared[:3]
        ],
    )
