"""Email the new-people survey link from the site (api#661 add-on).

Staff paste one or more addresses on the New people tab; each plausible,
not-yet-known address gets ONE email with the shared survey link.

WHAT IS SKIPPED (never emailed, each reported back once with a reason):

* ``invalid``          -- not a plausible address, by the public survey's own
  shape gate (``survey_responses._valid_email``);
* ``duplicate``        -- listed more than once in this paste (sent once);
* ``in_database``      -- matches a contact email of an existing alumni/friend
  record (they get the normal survey, not an invitation to be "added");
* ``recently_invited`` -- this address was invited in the last 30 days.

ALL OR NOTHING. The link must be ON (409). The send must fit the shared Resend
budget the survey and the digest spend from, or NOTHING is sent (409); there is
no partial send. The whole batch is one Resend call (≤ 50 < Resend's 100). If
Resend refuses it, or the outcome is unknown, the answer is 502 and no ledger
row is written.

THE LEDGER (``new_people_invites``) is written only AFTER Resend accepts the
batch, one row per email, holding the sha256 of the normalised address and
NEVER the address itself. It feeds the 30-day skip and, through
:func:`sent_counts`, ``survey_email.get_send_usage``: the console meter, the
cron's pacing and the survey's send gate all see invite emails, exactly as they
see the digest's (see ``opportunity_link_digest`` for why a ledger rather than
a reserve).

THE SEND RUNS UNDER THE SURVEY'S ``send_lock``, like the digest, so the survey
can never read its budget while invite emails are in flight, and two staff
sending at once cannot both pass the budget and the 30-day check.

⚠️ NO UNSUBSCRIBE LINK, by standing project rule.
"""

from __future__ import annotations

import asyncio
import datetime
import hashlib
import logging
import re
import time
from html import escape

import httpx
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.errors import ConflictError, InvalidRequestError, ServiceError
from app.models.audit import AuditLog
from app.models.contact import AlumniContactInfo
from app.models.new_people import NewPeopleInvite
from app.schemas.new_people import NewPeopleInviteResult, NewPeopleInviteSkipped
from app.services import mailer, new_people_survey
from app.services import survey_responses as sr
from app.services.attendee_match import _norm_col

log = logging.getLogger(__name__)

#: Most unique addresses one send may name (the paste is refused above this).
MAX_ADDRESSES = 50
#: An address invited within this many days is skipped.
RECENT_DAYS = 30

SUBJECT = "Join the BYU Finance Alumni Network"
BUTTON_LABEL = "Start the survey"

TOO_MANY_MESSAGE = f"You can send to up to {MAX_ADDRESSES} addresses at a time."
NO_ADDRESSES_MESSAGE = "Enter at least one email address."
LINK_OFF_MESSAGE = "The survey link is turned off."
RESEND_FAILED_MESSAGE = "The email service didn't accept the send. Nothing was sent."
NOT_CONFIGURED_MESSAGE = "Email sending is not set up on this server. Nothing was sent."
BUSY_MESSAGE = "A survey send is running right now. Try again in a minute."

# The footer names the survey's own "email us" contact
# (`survey_email.survey_support_contact`, the engineer-console row labelled
# "survey"). When none is set the footer line is LEFT OFF -- never a guessed
# address, same rule as the survey itself.

_TIMEOUT_SECONDS = 20.0
# How long to wait for a running survey send to release the shared lock.
_LOCK_WAIT_SECONDS = 10.0
_LOCK_POLL_SECONDS = 0.5

# Any mix of commas, semicolons and whitespace (spaces, tabs, new lines).
_SPLIT_RE = re.compile(r"[\s,;]+")
# Echoed tokens are bounded so a junk paste cannot balloon the response.
_ECHO_MAX = 254

_BTN_STYLE = (
    "display:inline-block;background:#1e2a4a;color:#ffffff;text-decoration:none;"
    "font-size:15px;font-weight:600;padding:11px 20px;border-radius:8px;"
)


# ------------------------------------------------------------------ parsing --


def normalise(token: str) -> str:
    """Trim and lower-case; a single wrapping ``<...>`` (as mail clients copy
    an address) is dropped."""
    value = token.strip()
    if len(value) >= 2 and value.startswith("<") and value.endswith(">"):
        value = value[1:-1].strip()
    return value.lower()


def email_hash(address: str) -> str:
    """sha256 hex of the NORMALISED address -- the only form the ledger holds."""
    return hashlib.sha256(normalise(address).encode("utf-8")).hexdigest()


def _plausible(address: str) -> bool:
    return 0 < len(address) <= 254 and sr._valid_email(address)


def parse(raw: str) -> tuple[list[str], list[NewPeopleInviteSkipped]]:
    """``(candidates, skipped)`` from the pasted text.

    *candidates* are unique, plausible, normalised addresses in paste order.
    *skipped* holds ``invalid`` (once per distinct token) and ``duplicate``
    (once per plausible address listed more than once; the address itself is
    still a candidate, so it is "sent once, reported once").

    Raises :class:`InvalidRequestError` (422) for an empty paste or more than
    :data:`MAX_ADDRESSES` unique tokens.
    """
    tokens = [normalise(t) for t in _SPLIT_RE.split(raw or "")]
    tokens = [t for t in tokens if t]
    unique: list[str] = list(dict.fromkeys(tokens))
    if not unique:
        raise InvalidRequestError(NO_ADDRESSES_MESSAGE)
    if len(unique) > MAX_ADDRESSES:
        raise InvalidRequestError(TOO_MANY_MESSAGE)
    counts: dict[str, int] = {}
    for t in tokens:
        counts[t] = counts.get(t, 0) + 1
    candidates: list[str] = []
    skipped: list[NewPeopleInviteSkipped] = []
    for t in unique:
        if not _plausible(t):
            skipped.append(NewPeopleInviteSkipped(email=t[:_ECHO_MAX], reason="invalid"))
            continue
        candidates.append(t)
        if counts[t] > 1:
            skipped.append(NewPeopleInviteSkipped(email=t, reason="duplicate"))
    return candidates, skipped


# ----------------------------------------------------------------- lookups --


async def _in_database(session: AsyncSession, emails: list[str]) -> set[str]:
    """Which of *emails* (normalised) are a contact email on ANY record, alum or
    friend, archived or not: inviting someone already on file to be "added"
    would only make a duplicate."""
    if not emails:
        return set()
    cols = (
        AlumniContactInfo.personal_email,
        AlumniContactInfo.work_email,
        AlumniContactInfo.best_contact,
    )
    rows = (
        await session.execute(
            select(*cols)
            .where(or_(*(_norm_col(c).in_(emails) for c in cols)))
            .limit(len(emails) * 10)
        )
    ).all()
    wanted = set(emails)
    found: set[str] = set()
    for row in rows:
        for value in row:
            if isinstance(value, str) and normalise(value) in wanted:
                found.add(normalise(value))
    return found


async def _recently_invited(session: AsyncSession, emails: list[str]) -> set[str]:
    """Which of *emails* were invited in the last :data:`RECENT_DAYS` days."""
    if not emails:
        return set()
    by_hash = {email_hash(e): e for e in emails}
    cutoff = datetime.datetime.now(datetime.UTC) - datetime.timedelta(days=RECENT_DAYS)
    hashes = (
        (
            await session.execute(
                select(NewPeopleInvite.email_hash)
                .where(
                    NewPeopleInvite.email_hash.in_(list(by_hash)),
                    NewPeopleInvite.sent_at >= cutoff,
                )
                .distinct()
            )
        )
        .scalars()
        .all()
    )
    return {by_hash[h] for h in hashes if h in by_hash}


async def sent_counts(
    session: AsyncSession,
    *,
    start_today: datetime.datetime,
    start_month: datetime.datetime,
    after: datetime.datetime | None = None,
) -> tuple[int, int]:
    """``(today, this_month)`` invite e-mails, for ``survey_email.get_send_usage``.

    NEVER RAISES and never poisons the caller's transaction: same SAVEPOINT +
    count-zero contract as ``opportunity_link_digest.sent_counts``, so an
    un-migrated schema or a fake test session leaves the survey budget exactly as
    it was before this feature. ``after`` is the manual usage baseline's anchor.
    """
    stmt = select(
        func.count().label("month"),
        func.count().filter(NewPeopleInvite.sent_at >= start_today).label("today"),
    ).where(NewPeopleInvite.sent_at >= start_month)
    if after is not None:
        stmt = stmt.where(NewPeopleInvite.sent_at > after)
    try:
        async with session.begin_nested():
            row = (await session.execute(stmt)).first()
    except Exception:  # noqa: BLE001 - the survey send must never fail on this
        log.warning("new_people_invites: could not read the invite ledger; counting zero")
        return (0, 0)
    if not row:
        return (0, 0)
    try:
        return (int(row[1] or 0), int(row[0] or 0))
    except (TypeError, ValueError, IndexError):
        return (0, 0)


# ------------------------------------------------------------------- render --


def survey_url(base_url: str, key: str) -> str:
    """The public new-people survey URL (same base setting as the survey email)."""
    return f"{base_url.rstrip('/')}/survey/new/{key}"


def render_invite_email(
    link: str, contact_name: str | None, contact_email: str | None
) -> tuple[str, str, str]:
    """``(subject, html, text)``. Everything interpolated is HTML-escaped. NO
    unsubscribe link (standing project rule)."""
    paragraphs = new_people_survey.NEW_PEOPLE_INTRO_PARAGRAPHS
    signoff = new_people_survey.NEW_PEOPLE_INTRO_SIGNOFF
    has_contact = bool(contact_name and contact_email)
    footer_text = f"Questions? Email {contact_name} at {contact_email}" if has_contact else ""

    text = "\n".join(
        [
            *(line for p in paragraphs for line in (p, "")),
            *signoff,
            "",
            f"{BUTTON_LABEL}: {link}",
            *(["", footer_text] if has_contact else []),
        ]
    )

    p_style = 'style="margin:0 0 12px;font-size:15px;"'
    paras_html = "".join(f"<p {p_style}>{escape(p)}</p>" for p in paragraphs)
    signoff_html = "<br>".join(escape(line) for line in signoff)
    heading_html = escape(new_people_survey.NEW_PEOPLE_INTRO_HEADING)
    footer_html = (
        '<p style="margin:0;font-size:14px;color:#4b5563;">'
        f"Questions? Email {escape(contact_name or '')} at "
        f'<a href="mailto:{escape(contact_email or "")}" style="color:#1e2a4a;">'
        f"{escape(contact_email or '')}</a></p>"
        if has_contact
        else ""
    )
    html = f"""\
<div style="margin:0;padding:0;background:#f3f4f6;">
  <div style="max-width:600px;margin:0 auto;background:#ffffff;">
    <div style="background:#1e2a4a;padding:16px 24px;">
      <span style="color:#ffffff;font-size:16px;font-weight:600;">{heading_html}</span>
    </div>
    <div style="padding:24px;font-family:Arial,sans-serif;color:#374151;line-height:1.55;">
      {paras_html}
      <p {p_style}>{signoff_html}</p>
      <div style="margin:20px 0;">
        <a href="{escape(link)}" style="{_BTN_STYLE}">{escape(BUTTON_LABEL)}</a>
      </div>
      {footer_html}
    </div>
    <div style="padding:16px 24px;border-top:1px solid #e5e7eb;font-family:Arial,sans-serif;
         font-size:12px;color:#9ca3af;">BYU Marriott School of Business</div>
  </div>
</div>"""
    return SUBJECT, html, text


# --------------------------------------------------------------------- send --


async def _post_batch(api_key: str, emails: list[dict]) -> list[str] | None:
    """One Resend batch call. Returns the per-email ids (or None if unreadable).
    Raises :class:`ServiceError` (502) for ANY failure: refused, rate-limited or
    an unknown outcome -- the caller writes no ledger row in every case."""
    from app.services import survey_email  # local: survey_email imports this module

    try:
        response = await mailer.post_json(
            mailer.RESEND_BATCH_URL, api_key=api_key, payload=emails, timeout=_TIMEOUT_SECONDS
        )
    except httpx.HTTPError as exc:
        log.error("new_people_invites: could not reach Resend")
        raise ServiceError(RESEND_FAILED_MESSAGE) from exc
    if not response.is_success:
        log.error("new_people_invites: Resend refused the batch (HTTP %s)", response.status_code)
        raise ServiceError(RESEND_FAILED_MESSAGE)
    return survey_email._resend_message_ids(response, len(emails))


async def send_invites(
    session: AsyncSession, raw: str, actor_user_id: int | None
) -> NewPeopleInviteResult:
    """Parse, skip, budget-check, send ONE batch, then record it."""
    from app.services import survey_email  # local: survey_email imports this module

    candidates, skipped = parse(raw)

    link = await new_people_survey._load_link(session)
    if link is None or not link.enabled or not link.link_key:
        raise ConflictError(LINK_OFF_MESSAGE)

    deadline = time.monotonic() + _LOCK_WAIT_SECONDS
    while True:
        async with survey_email.send_lock() as acquired:
            if acquired:
                return await _send_locked(
                    session, candidates, skipped, link.link_key, actor_user_id
                )
        if time.monotonic() >= deadline:
            raise ConflictError(BUSY_MESSAGE)
        await asyncio.sleep(_LOCK_POLL_SECONDS)


async def _send_locked(
    session: AsyncSession,
    candidates: list[str],
    skipped: list[NewPeopleInviteSkipped],
    key: str,
    actor_user_id: int | None,
) -> NewPeopleInviteResult:
    from app.services import survey_email  # local: survey_email imports this module

    known = await _in_database(session, candidates)
    recent = await _recently_invited(session, [c for c in candidates if c not in known])
    to_send: list[str] = []
    for address in candidates:
        if address in known:
            skipped.append(NewPeopleInviteSkipped(email=address, reason="in_database"))
        elif address in recent:
            skipped.append(NewPeopleInviteSkipped(email=address, reason="recently_invited"))
        else:
            to_send.append(address)

    if to_send:
        allowance = await survey_email.remaining_send_allowance(session)
        if allowance is not None and allowance < len(to_send):
            raise ConflictError(f"Only {allowance} emails left in today's sending budget.")

        settings = get_settings()
        if not (
            settings.survey_app_base_url and settings.survey_from_email and settings.resend_api_key
        ):
            raise ServiceError(NOT_CONFIGURED_MESSAGE)
        contact = await survey_email.survey_support_contact(session)
        contact_name = contact.name if contact else None
        contact_email = contact.email if contact else None
        subject, html, text = render_invite_email(
            survey_url(settings.survey_app_base_url, key), contact_name, contact_email
        )
        sender = mailer.from_field(settings.survey_from_email, settings.survey_from_name)
        batch = [
            {
                "from": sender,
                "to": [address],
                "subject": subject,
                "html": html,
                "text": text,
                # No PII: tells a bounce webhook this was not a survey email.
                "tags": [{"name": "kind", "value": "new_people_invite"}],
            }
            for address in to_send
        ]
        ids = await _post_batch(settings.resend_api_key, batch)
        # Resend accepted: NOW the ledger. Hashes only, never the address.
        for i, address in enumerate(to_send):
            session.add(
                NewPeopleInvite(
                    email_hash=email_hash(address),
                    sent_by_user_id=actor_user_id,
                    resend_email_id=ids[i] if ids else None,
                )
            )

    # Counts only: never an address.
    session.add(
        AuditLog(
            user_id=actor_user_id,
            action_type="send_new_people_invites",
            entity_type="new_people_invite",
            new_value=f"sent={len(to_send)} skipped={len(skipped)}",
        )
    )
    await session.commit()
    log.info("new-people invites: sent %d, skipped %d", len(to_send), len(skipped))
    return NewPeopleInviteResult(sent=len(to_send), sent_to=to_send, skipped=skipped)
