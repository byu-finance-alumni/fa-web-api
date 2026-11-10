"""The new-people survey (api#661): one shared link, a holding queue, a review.

The career directors email ONE link to alumni and professionals who are not in
the database yet. What they submit is HELD in ``new_people_submissions``;
nobody is added until a reviewer approves the row, either by creating a new
record (alum or friend) or by applying it to an existing one. Reject purges the
held answers.

REUSE, NOT RESTATEMENT. Every survey field here is a key from
``survey_responses._FIELDS`` and is cleaned by that module's own ``_coerce``, so
a value the normal survey would refuse is refused here too. An approve writes
through ``survey_responses.apply_payload_to_record``, the same code a normal
survey apply runs, so the record ends up exactly as if the person had answered
the normal survey: same whitelist, same side rows, same comment-to-Note, same
field-level audit.

What is NEW is small: the three BYU-alum keys (``new.graduation_year``,
``new.byu_degree``, the optional ``new.byu_id``), the required ANY-OF groups
(plus the state, required when the country is the US) (the normal survey has none,
because it edits a record that already exists), the link itself, and the
global cap on pending rows.

NOT COUNTED. Held rows live only in ``new_people_submissions``, which nothing
outside this module reads, so they never reach alumni counts, KPIs, campaign
tallies, ``survey_responses`` or the normal review queue.
"""

from __future__ import annotations

import asyncio
import datetime
import hmac
import logging
import re
import secrets

from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit_context import AUDIT_SOURCE_SURVEY, audit_source_scope, new_change_set_id
from app.core.config import get_settings
from app.core.errors import ConflictError, InvalidRequestError, NotFoundError
from app.models.alumni import Alumni
from app.models.audit import AuditLog
from app.models.contact import AlumniContactInfo
from app.models.employment import EducationHistory
from app.models.new_people import (
    STATUS_APPROVED,
    STATUS_PENDING,
    STATUS_REJECTED,
    NewPeopleSurveyLink,
    NewPersonSubmission,
)
from app.models.survey_response import COMMENTS_KEY
from app.schemas.alumni import AlumniBase
from app.schemas.new_people import (
    NewPeopleLinkRead,
    NewPeopleRequiredFields,
    NewPersonApproveRequest,
    NewPersonApproveResult,
    NewPersonMergePreview,
    NewPersonPossibleMatch,
    NewPersonSubmissionField,
    NewPersonSubmissionItem,
)
from app.schemas.survey import SURVEY_COMMENTS_MAX, SurveyChange, clean_survey_comment
from app.services import failure_alert
from app.services import survey_responses as sr
from app.services.alumni import _audit
from app.services.attendee_match import _norm_col, _norm_email

log = logging.getLogger(__name__)

# One message for a wrong key AND a switched-off link, so the response never
# says whether a guessed key was right.
LINK_CLOSED_MESSAGE = "This survey link is closed."

STALE_MESSAGE = "This submission changed since you opened it."
NOT_PENDING_MESSAGE = "This submission is no longer waiting for review."

# The most submissions that may wait for review at once. A leaked link can
# otherwise fill the table (and the reviewers' queue) with junk at whatever rate
# the per-instance limiters let through; past this the form politely refuses
# until staff work the queue down. Hundreds is far beyond a real mailing's
# backlog.
PENDING_CAP = 500
PENDING_CAP_MESSAGE = (
    "We can't take new submissions right now. Please try again in a few days."
)

# Tanya's intro message, as the public intro page shows it, and the email the
# staff "Email the link" send uses (`new_people_invites`). ⚠️ The public page
# renders its OWN copy (fa-web-app `src/lib/newPeopleSurvey.ts`,
# NEW_PEOPLE_INTRO_PARAGRAPHS / NEW_PEOPLE_INTRO_SIGNOFF) because it draws before
# any API call; the two MUST stay word for word identical. Change both or neither.
NEW_PEOPLE_INTRO_HEADING = "Stay connected with BYU Finance"
NEW_PEOPLE_INTRO_PARAGRAPHS: tuple[str, ...] = (
    "Whether you're a BYU Finance alum or a professional who stays engaged with "
    "our students, your experience and willingness to help are a valued part of "
    "our community.",
    "We're updating our records so we can stay connected with you throughout your "
    "career. Please take a moment to complete this survey with your current career "
    "and contact information. A few optional questions will also help us "
    "understand how you'd like to stay involved and support our students.",
    "Thank you for being part of the BYU Finance community. We look forward to "
    "staying connected with you in the years ahead!",
)
NEW_PEOPLE_INTRO_SIGNOFF: tuple[str, ...] = (
    "Warmest regards,",
    "Tanya Harmon & Amy Densley",
    "BYU Finance Career Directors",
)

# The single link row's id (a CHECK pins it).
_LINK_ID = 1
# `secrets.token_urlsafe(32)` is 43 URL-safe characters (256 bits).
_KEY_BYTES = 32
_KEY_MAX = 128

# The keys only the BYU-alum path carries. A friend's answers to them are
# dropped on submit.
NEW_GRADUATION_YEAR = "new.graduation_year"
NEW_BYU_DEGREE = "new.byu_degree"
# Optional. Held and stored DIGITS ONLY, the `alumni.byu_id` convention (the
# partial unique index `uq_alumni_byu_id_active` compares the raw column). PII:
# never logged.
NEW_BYU_ID = "new.byu_id"
NEW_KEYS = (NEW_GRADUATION_YEAR, NEW_BYU_DEGREE, NEW_BYU_ID)
NEW_KEY_LABELS = {
    NEW_GRADUATION_YEAR: "BYU graduation year",
    NEW_BYU_DEGREE: "BYU degree",
    NEW_BYU_ID: "BYU ID",
}
BYU_ID_INVALID_MESSAGE = "BYU ID must be 9 digits."
# A person may type spaces or dashes ("123-45-6789"); nothing else is forgiven.
_BYU_ID_SEPARATORS = re.compile(r"[\s-]")
_BYU_ID_RE = re.compile(r"^[0-9]{9}$")
# The Postgres name of the index a racing BYU ID write would trip.
_BYU_ID_UNIQUE_INDEX = "uq_alumni_byu_id_active"

_COUNTRY_KEY = "employment.current_country"
_STATE_KEY = "employment.current_state"
# When the country is one of these (trimmed, case-insensitive) the state is
# REQUIRED, for both kinds. Served to the form by GET so it never hard-codes it.
STATE_REQUIRED_COUNTRIES: tuple[str, ...] = (
    "united states",
    "united states of america",
    "usa",
    "us",
    "u.s.",
    "u.s.a.",
)
# education_history.degree is varchar(255); a degree name is short, and a cap
# well under the column keeps the queue readable.
_DEGREE_MAX = 100
_YEAR_RE = re.compile(r"^[0-9]{4}$")

_EMAIL_GROUP = ("contact.personal_email", "contact.work_email")

# Required ANY-OF groups (Jake, 2026-11-10): name, email, company name,
# industry, company location (city + country), LinkedIn -- for both kinds; BYU
# alumni also give graduation year and degree.
REQUIRED_FRIEND: tuple[tuple[str, ...], ...] = (
    ("profile.first_name",),
    ("profile.last_name",),
    _EMAIL_GROUP,
    ("employment.current_employer",),
    ("employment.current_industry",),
    ("employment.current_city",),
    (_COUNTRY_KEY,),
    ("profile.linkedin_url",),
)
REQUIRED_ALUM: tuple[tuple[str, ...], ...] = REQUIRED_FRIEND + (
    (NEW_GRADUATION_YEAR,),
    (NEW_BYU_DEGREE,),
)

# How many possible matches the queue shows per submission.
_MATCH_CAP = 5
# Bound on the batched match lookups, so a long queue cannot pull a table's
# worth of rows into Python.
_MATCH_LOOKUP_ROWS = 2000


def required_fields() -> NewPeopleRequiredFields:
    return NewPeopleRequiredFields(
        alum=[list(g) for g in REQUIRED_ALUM],
        friend=[list(g) for g in REQUIRED_FRIEND],
    )


def _required_for(kind: str) -> tuple[tuple[str, ...], ...]:
    return REQUIRED_ALUM if kind == "alum" else REQUIRED_FRIEND


def state_required_for(country: object) -> bool:
    """Whether *country* is one the state is required for."""
    return sr._text(country).lower() in STATE_REQUIRED_COUNTRIES


def clean_byu_id(value: object) -> str | None:
    """A BYU ID as stored (9 digits), ``""`` when nothing was given, or ``None``
    when it is unusable. Spaces and dashes are stripped first."""
    stripped = _BYU_ID_SEPARATORS.sub("", sr._text(value))
    if not stripped:
        return ""
    return stripped if _BYU_ID_RE.match(stripped) else None


# ------------------------------------------------------------------ the link --


def _new_key() -> str:
    return secrets.token_urlsafe(_KEY_BYTES)


def _now() -> datetime.datetime:
    return datetime.datetime.now(datetime.UTC)


def _link_read(link: NewPeopleSurveyLink) -> NewPeopleLinkRead:
    updated = link.updated_at or link.created_at or _now()
    return NewPeopleLinkRead(
        enabled=bool(link.enabled), key=link.link_key, updated_at=updated.isoformat()
    )


async def _load_link(session: AsyncSession, *, for_update: bool = False):
    stmt = select(NewPeopleSurveyLink).where(NewPeopleSurveyLink.id == _LINK_ID)
    if for_update:
        stmt = stmt.with_for_update()
    return (await session.execute(stmt)).scalar_one_or_none()


def _audit_link(session: AsyncSession, actor_user_id: int | None, action: str, detail: str):
    """An audit row for a link change. NEVER the key: the row would otherwise be
    a second place a live credential is readable."""
    session.add(
        AuditLog(
            user_id=actor_user_id,
            action_type=action,
            entity_type="new_people_survey_link",
            entity_id=_LINK_ID,
            new_value=detail,
        )
    )


async def _get_or_create_link(
    session: AsyncSession, actor_user_id: int | None
) -> NewPeopleSurveyLink:
    link = await _load_link(session, for_update=True)
    if link is not None:
        return link
    now = _now()
    link = NewPeopleSurveyLink(
        id=_LINK_ID,
        link_key=_new_key(),
        enabled=False,
        updated_by_user_id=actor_user_id,
        created_at=now,
        updated_at=now,
    )
    session.add(link)
    _audit_link(session, actor_user_id, "create_new_people_link", "enabled=false")
    return link


async def get_link(session: AsyncSession, actor_user_id: int | None) -> NewPeopleLinkRead:
    """The link, created (disabled) on first read so staff always have one."""
    link = await _load_link(session)
    if link is None:
        link = await _get_or_create_link(session, actor_user_id)
        await session.commit()
    return _link_read(link)


async def set_enabled(
    session: AsyncSession, enabled: bool, actor_user_id: int | None
) -> NewPeopleLinkRead:
    link = await _get_or_create_link(session, actor_user_id)
    link.enabled = enabled
    link.updated_by_user_id = actor_user_id
    link.updated_at = _now()
    _audit_link(
        session,
        actor_user_id,
        "update_new_people_link",
        f"enabled={'true' if enabled else 'false'}",
    )
    await session.commit()
    return _link_read(link)


async def reissue(session: AsyncSession, actor_user_id: int | None) -> NewPeopleLinkRead:
    """A new key; the old one stops working at once. ``enabled`` is unchanged."""
    link = await _get_or_create_link(session, actor_user_id)
    link.link_key = _new_key()
    link.updated_by_user_id = actor_user_id
    link.updated_at = _now()
    _audit_link(
        session,
        actor_user_id,
        "reissue_new_people_link",
        f"enabled={'true' if link.enabled else 'false'}",
    )
    await session.commit()
    return _link_read(link)


async def verify_key(session: AsyncSession, key: str) -> None:
    """Raise 404 unless *key* is the live, ENABLED link key.

    Wrong key and switched-off link raise the SAME error. The comparison is
    constant-time (``hmac.compare_digest``) so response timing does not leak how
    much of a guessed key was right, and it is made even when the link is off.
    """
    if not isinstance(key, str) or not key or len(key) > _KEY_MAX:
        raise NotFoundError(LINK_CLOSED_MESSAGE)
    link = await _load_link(session)
    stored = link.link_key if link is not None else ""
    matches = hmac.compare_digest(key.encode("utf-8"), (stored or "").encode("utf-8"))
    if link is None or not stored or not matches or not link.enabled:
        raise NotFoundError(LINK_CLOSED_MESSAGE)


# ------------------------------------------------------------------ cleaning --


def _clean_new_value(key: str, value: str) -> str | None:
    """A ``new.*`` answer made safe to hold, or ``None`` when it is unusable."""
    if key == NEW_GRADUATION_YEAR:
        if not _YEAR_RE.match(value):
            return None
        try:
            AlumniBase._validate_year(int(value))
        except ValueError:
            return None
        return value
    if key == NEW_BYU_DEGREE:
        if len(value) > _DEGREE_MAX or not sr._valid_free_text(value):
            return None
        return value
    if key == NEW_BYU_ID:
        return clean_byu_id(value)
    return None


def clean_fields(kind: str, fields: dict[str, str]) -> tuple[dict[str, str], set[str]]:
    """``(payload, refused)`` for a public submission.

    *payload* holds only recognised, non-blank answers the apply path would
    actually write, by the normal survey's own ``_coerce`` rules (plus the
    ``new.*`` keys on the alum path; a friend's are dropped). Unknown keys are
    dropped silently, exactly as the normal survey drops them. *refused* names
    known keys whose answer was not usable, so a REQUIRED one (or a BYU ID) can
    be reported back by key; an optional one is simply left out, which is what
    the normal survey does with it.
    """
    payload: dict[str, str] = {}
    refused: set[str] = set()
    for key, raw in (fields or {}).items():
        value = sr._text(raw)
        if not value:
            continue
        if key in NEW_KEYS:
            if kind != "alum":
                continue
            cleaned = _clean_new_value(key, value)
            if cleaned == "":
                continue  # a BYU ID of only spaces/dashes: not given
        else:
            field = sr._FIELD_BY_KEY.get(key)
            if field is None:
                continue
            if field.kind in ("bool", "designation"):
                # A yes/no answer: the apply decides what it means, the same
                # way the normal survey stages it.
                cleaned = value
            else:
                coerced = sr._coerce(field, value)
                cleaned = None if coerced is sr._IGNORE or coerced is None else value
        if cleaned is None:
            refused.add(key)
        else:
            payload[key] = cleaned
    return payload, refused


def missing_required(
    kind: str, payload: dict[str, str], refused: set[str]
) -> list[dict[str, str]]:
    """The error-envelope ``fields`` entries for every unmet required group,
    the state when the country is the US, and an unusable BYU ID (optional,
    but a wrong one is reported rather than silently dropped)."""
    errors: list[dict[str, str]] = []

    def _missing(key: str) -> dict[str, str]:
        message = "This answer is not valid." if key in refused else "This field is required."
        return {"field": key, "message": message}

    for group in _required_for(kind):
        if any(payload.get(k) for k in group):
            continue
        for key in group:
            if group == _EMAIL_GROUP:
                errors.append({"field": key, "message": "Enter a personal or work email."})
            else:
                errors.append(_missing(key))
    if state_required_for(payload.get(_COUNTRY_KEY)) and not payload.get(_STATE_KEY):
        errors.append(_missing(_STATE_KEY))
    if NEW_BYU_ID in refused:
        errors.append({"field": NEW_BYU_ID, "message": BYU_ID_INVALID_MESSAGE})
    return errors


class MissingRequiredFields(Exception):
    """A submission is missing required answers. Carries the envelope fields."""

    def __init__(self, fields: list[dict[str, str]]) -> None:
        self.fields = fields
        super().__init__("Please answer the required questions.")


class PendingCapReached(Exception):
    """The holding queue is full; refuse politely (429)."""


# ------------------------------------------------------------------- submit --


# Serialises the dedupe lookup, the pending count and the insert across every
# concurrent submit (and every serverless instance): a transaction-scoped
# Postgres advisory lock, released by the commit or rollback that ends it. An
# arbitrary constant, unique to this feature.
_SUBMIT_LOCK_KEY = 661_000_001

# One alert when the queue fills to this many pending rows. See `_maybe_alert`.
PENDING_ALERT_AT = PENDING_CAP * 8 // 10
_ALERT_TIMEOUT_SECONDS = 5.0


async def _pending_with_email(
    session: AsyncSession, emails: list[str]
) -> NewPersonSubmission | None:
    """The newest PENDING submission sharing any of *emails* (normalised, in
    either email field), row-locked, or None."""
    if not emails:
        return None
    personal = func.lower(func.trim(NewPersonSubmission.payload["contact.personal_email"].astext))
    work = func.lower(func.trim(NewPersonSubmission.payload["contact.work_email"].astext))
    return (
        await session.execute(
            select(NewPersonSubmission)
            .where(
                NewPersonSubmission.status == STATUS_PENDING,
                or_(personal.in_(emails), work.in_(emails)),
            )
            .order_by(NewPersonSubmission.submitted_at.desc())
            .limit(1)
            .with_for_update()
        )
    ).scalar_one_or_none()


async def submit(
    session: AsyncSession,
    *,
    kind: str,
    fields: dict[str, str],
    comments: str | None,
    fill_seconds: int | None,
) -> None:
    """Hold one submission for review. The caller has already checked the key,
    the size caps and the honeypot.

    ONE PENDING ROW PER EMAIL. A second submission from an email that already
    has a pending row REPLACES that row in place (newest wins, like the normal
    survey's #655 merge, but whole rather than per field: these people have no
    record whose values a partial answer could fall back on). ``submitted_at``
    moves to now, so a reviewer holding the old version gets the 409 stale check
    rather than approving answers that have since been replaced.

    RACE-SAFE CAP. The lookup, the count and the insert all run under one
    transaction-scoped advisory lock, so two requests cannot both read 499 and
    both insert. A replacement adds no row, so it is allowed at the cap.
    """
    payload, refused = clean_fields(kind, fields)
    errors = missing_required(kind, payload, refused)
    if errors:
        raise MissingRequiredFields(errors)
    comment = clean_survey_comment(comments)
    if comment is not None and len(comment) > SURVEY_COMMENTS_MAX:
        comment = None
    fill = sr._sane_fill_seconds(fill_seconds)

    await session.execute(select(func.pg_advisory_xact_lock(_SUBMIT_LOCK_KEY)))

    existing = await _pending_with_email(session, _emails(payload))
    if existing is not None:
        existing.kind = kind
        existing.payload = payload
        existing.comments = comment
        existing.fill_seconds = fill
        existing.submitted_at = func.now()
        replaced_id = existing.submission_id
        await session.commit()
        # Ids and counts only: never a submitted value.
        log.info(
            "new-people survey: replaced pending submission %s (%d fields)",
            replaced_id,
            len(payload),
        )
        return

    pending = (
        await session.scalar(
            select(func.count())
            .select_from(NewPersonSubmission)
            .where(NewPersonSubmission.status == STATUS_PENDING)
        )
        or 0
    )
    if pending >= PENDING_CAP:
        await session.rollback()  # releases the advisory lock
        log.warning("new-people survey: pending cap (%d) reached; submission refused", PENDING_CAP)
        raise PendingCapReached()

    session.add(
        NewPersonSubmission(
            kind=kind,
            payload=payload,
            comments=comment,
            fill_seconds=fill,
            status=STATUS_PENDING,
        )
    )
    await session.commit()
    log.info("new-people survey: held a %s submission (%d fields)", kind, len(payload))
    await _maybe_alert(pending + 1)


async def _maybe_alert(pending_now: int) -> None:
    """ONE alert when the queue fills to ``PENDING_ALERT_AT``.

    Deduplicated by construction rather than by a cooldown: the count and the
    insert run under the advisory lock, so exactly one request across every
    instance is the one that takes the queue from ``PENDING_ALERT_AT - 1`` to
    ``PENDING_ALERT_AT``, and only that request sends. It re-arms only if
    reviewers work the queue back below the line and it fills again. A cooldown
    in process memory would instead send once per warm instance.

    Best-effort, after the commit: the submission is already held, and a
    failing alerter must never turn it into an error for the person submitting.
    Counts only, never anything a person submitted.
    """
    if pending_now != PENDING_ALERT_AT:
        return
    log.warning(
        "new-people survey: %d submissions pending (alert threshold, cap %d)",
        pending_now,
        PENDING_CAP,
    )
    if not failure_alert.alerting_enabled():
        return
    env = get_settings().environment
    rows = [
        ("Environment", str(env)),
        ("Pending submissions", str(pending_now)),
        ("Cap", f"{PENDING_CAP} (new submissions are refused at the cap)"),
    ]
    summary = (
        f"The new-people survey queue holds {pending_now} pending submissions "
        f"({PENDING_ALERT_AT * 100 // PENDING_CAP}% of the {PENDING_CAP} cap). "
        "Review the queue, and if it is junk, switch the link off or reissue it."
    )
    try:
        landed = await asyncio.wait_for(
            failure_alert.deliver_alert(
                f"[fa-web-api {env}] New-people survey queue is {pending_now}/{PENDING_CAP}",
                "The shared new-people survey link has filled most of its review "
                "queue. A real mailing rarely does this; a leaked or scripted link "
                "does. Check the queue, then switch the link off or reissue it if "
                "the submissions are junk.",
                rows,
                purpose=failure_alert.SECURITY,
                slack_summary=summary,
            ),
            timeout=_ALERT_TIMEOUT_SECONDS,
        )
        if not landed:
            log.error("new-people survey: the queue alert landed nowhere")
    except Exception:  # noqa: BLE001 - the alert must never fail the submit
        log.error("new-people survey: could not deliver the queue alert")


# -------------------------------------------------------------- review queue --


def _display_fields(payload: dict) -> list[NewPersonSubmissionField]:
    """Answers in form order, display-formatted the way the normal queue shows
    an "after" value."""
    out: list[NewPersonSubmissionField] = []
    for field in sr._FIELDS:
        if field.key in payload:
            out.append(
                NewPersonSubmissionField(
                    field_key=field.key,
                    label=field.label,
                    value=sr._after(field, payload[field.key]),
                )
            )
    for key in NEW_KEYS:
        if key in payload:
            out.append(
                NewPersonSubmissionField(
                    field_key=key, label=NEW_KEY_LABELS[key], value=sr._text(payload[key])
                )
            )
    return out


def _name_of(first: object, last: object, fallback: str = "") -> str:
    return " ".join(p for p in (sr._text(first), sr._text(last)) if p) or fallback


def _emails(payload: dict) -> list[str]:
    out: list[str] = []
    for key in _EMAIL_GROUP:
        email = _norm_email(payload.get(key))
        if email and email not in out:
            out.append(email)
    return out


def _byu_id_of(payload: dict) -> str | None:
    """The held BYU ID (re-cleaned, so only a valid 9-digit value), or None."""
    return clean_byu_id(payload.get(NEW_BYU_ID)) or None


def _name_key(payload: dict) -> tuple[str, str] | None:
    first = sr._text(payload.get("profile.first_name")).lower()
    last = sr._text(payload.get("profile.last_name")).lower()
    return (first, last) if first and last else None


async def _possible_matches(
    session: AsyncSession, payloads: list[dict]
) -> list[list[NewPersonPossibleMatch]]:
    """Live records that may be each payload's person, in up to three batched
    queries: the BYU ID against ``alumni.byu_id``, case-insensitive email
    against personal/work email, then first + last name (preferred first name
    counts). Friends included. BYU ID hits come first, then email; capped at
    ``_MATCH_CAP`` per payload."""
    all_emails = sorted({e for p in payloads for e in _emails(p)})
    all_lasts = sorted({k[1] for p in payloads if (k := _name_key(p))})
    all_byu = sorted({b for p in payloads if (b := _byu_id_of(p))})

    by_byu: dict[str, list[tuple]] = {}
    if all_byu:
        rows = (
            await session.execute(
                select(
                    Alumni.alumni_id,
                    Alumni.first_name,
                    Alumni.last_name,
                    Alumni.graduation_year,
                    Alumni.is_alumni,
                    Alumni.byu_id,
                )
                .where(Alumni.archived.is_(False), Alumni.byu_id.in_(all_byu))
                .order_by(Alumni.alumni_id.asc())
                .limit(_MATCH_LOOKUP_ROWS)
            )
        ).all()
        for alumni_id, first, last, year, is_alumni, byu in rows:
            by_byu.setdefault(byu, []).append((alumni_id, first, last, year, is_alumni))

    by_email: dict[str, list[tuple]] = {}
    if all_emails:
        rows = (
            await session.execute(
                select(
                    Alumni.alumni_id,
                    Alumni.first_name,
                    Alumni.last_name,
                    Alumni.graduation_year,
                    Alumni.is_alumni,
                    AlumniContactInfo.personal_email,
                    AlumniContactInfo.work_email,
                )
                .join(AlumniContactInfo, AlumniContactInfo.alumni_id == Alumni.alumni_id)
                .where(
                    Alumni.archived.is_(False),
                    or_(
                        _norm_col(AlumniContactInfo.personal_email).in_(all_emails),
                        _norm_col(AlumniContactInfo.work_email).in_(all_emails),
                    ),
                )
                .order_by(Alumni.alumni_id.asc())
                .limit(_MATCH_LOOKUP_ROWS)
            )
        ).all()
        for alumni_id, first, last, year, is_alumni, personal, work in rows:
            for raw in (personal, work):
                email = _norm_email(raw)
                if email in all_emails:
                    by_email.setdefault(email, []).append(
                        (alumni_id, first, last, year, is_alumni)
                    )

    by_name: dict[tuple[str, str], list[tuple]] = {}
    if all_lasts:
        rows = (
            await session.execute(
                select(
                    Alumni.alumni_id,
                    Alumni.first_name,
                    Alumni.preferred_first_name,
                    Alumni.last_name,
                    Alumni.graduation_year,
                    Alumni.is_alumni,
                )
                .where(Alumni.archived.is_(False), _norm_col(Alumni.last_name).in_(all_lasts))
                .order_by(Alumni.alumni_id.asc())
                .limit(_MATCH_LOOKUP_ROWS)
            )
        ).all()
        for alumni_id, first, preferred, last, year, is_alumni in rows:
            last_key = sr._text(last).lower()
            for given in {sr._text(first).lower(), sr._text(preferred).lower()} - {""}:
                by_name.setdefault((given, last_key), []).append(
                    (alumni_id, first, last, year, is_alumni)
                )

    results: list[list[NewPersonPossibleMatch]] = []
    for payload in payloads:
        seen: set[int] = set()
        matches: list[NewPersonPossibleMatch] = []
        candidates = [(row, "byu_id") for row in by_byu.get(_byu_id_of(payload) or "", [])]
        candidates += [(row, "email") for e in _emails(payload) for row in by_email.get(e, [])]
        key = _name_key(payload)
        if key is not None:
            candidates += [(row, "name") for row in by_name.get(key, [])]
        for (alumni_id, first, last, year, is_alumni), reason in candidates:
            if alumni_id in seen or len(matches) >= _MATCH_CAP:
                continue
            seen.add(alumni_id)
            matches.append(
                NewPersonPossibleMatch(
                    alumni_id=alumni_id,
                    name=_name_of(first, last, "Unnamed record"),
                    graduation_year=year,
                    is_friend=not is_alumni,
                    match_reason=reason,
                )
            )
        results.append(matches)
    return results


async def list_pending(session: AsyncSession) -> list[NewPersonSubmissionItem]:
    """Pending submissions, oldest first, each with its possible matches."""
    rows = (
        (
            await session.execute(
                select(NewPersonSubmission)
                .where(NewPersonSubmission.status == STATUS_PENDING)
                .order_by(
                    NewPersonSubmission.submitted_at.asc(),
                    NewPersonSubmission.submission_id.asc(),
                )
                .limit(PENDING_CAP)
            )
        )
        .scalars()
        .all()
    )
    if not rows:
        return []
    payloads = [dict(r.payload or {}) for r in rows]
    matches = await _possible_matches(session, payloads)
    items: list[NewPersonSubmissionItem] = []
    for row, payload, found in zip(rows, payloads, matches, strict=True):
        emails = [sr._text(payload.get(k)) for k in _EMAIL_GROUP if payload.get(k)]
        items.append(
            NewPersonSubmissionItem(
                submission_id=row.submission_id,
                kind=row.kind,
                submitted_at=row.submitted_at.isoformat(),
                name=_name_of(
                    payload.get("profile.first_name"),
                    payload.get("profile.last_name"),
                    "New person",
                ),
                email=emails[0] if emails else None,
                fields=_display_fields(payload),
                comments=row.comments,
                possible_matches=found,
            )
        )
    return items


# ---------------------------------------------------------- approve / reject --


async def _get_pending_locked(
    session: AsyncSession, submission_id: int
) -> NewPersonSubmission:
    """The submission, row-locked (``FOR UPDATE``), if and only if it is pending.

    The lock is what stops two reviewers (or a double-click) both approving:
    the second blocks until the first commits, then re-reads the row as
    approved and gets a 404 instead of creating a second record. Same pattern
    and reasoning as ``survey_responses._get_pending``. Held to the commit at
    the end of the caller, in the same session.
    """
    row = (
        await session.execute(
            select(NewPersonSubmission)
            .where(NewPersonSubmission.submission_id == submission_id)
            .with_for_update()
        )
    ).scalar_one_or_none()
    if row is None or row.status != STATUS_PENDING:
        raise NotFoundError(NOT_PENDING_MESSAGE)
    return row


def _same_instant(sent: str, stored: datetime.datetime | None) -> bool:
    """Whether the reviewer's ``submitted_at`` names the row's own timestamp."""
    if stored is None or not isinstance(sent, str):
        return False
    try:
        parsed = datetime.datetime.fromisoformat(sent.strip().replace("Z", "+00:00"))
    except ValueError:
        return False
    if parsed.tzinfo is None or stored.tzinfo is None:
        return parsed.replace(tzinfo=None) == stored.replace(tzinfo=None)
    return parsed == stored


def _purge(row: NewPersonSubmission, status: str, actor_user_id: int | None) -> None:
    """Decide the row and drop the held PII (the CHECK requires it)."""
    row.status = status
    row.decided_by_user_id = actor_user_id
    row.decided_at = _now()
    row.payload = None
    row.comments = None


async def _create_record(
    session: AsyncSession,
    payload: dict,
    kind: str,
    actor_user_id: int | None,
    change_set_id: str,
    byu_id: str | None = None,
) -> Alumni:
    """A brand-new alum or friend record, holding only what the survey fields
    cannot write: the kind, and for an alum the BYU graduation year (the real
    ``alumni.graduation_year`` column), degree (an ``education_history`` row,
    the profile's Education section) and BYU ID (``alumni.byu_id``; the caller
    has already checked no active record holds it). Everything else is written
    by the shared apply afterwards.

    A friend is simply ``is_alumni = false``: the visible ``FRIEND-00042`` id
    (#538) is derived from the primary key, so there is nothing else to mint.
    """
    is_alum = kind == "alum"
    year: int | None = None
    degree: str | None = None
    if is_alum:
        raw_year = _clean_new_value(NEW_GRADUATION_YEAR, sr._text(payload.get(NEW_GRADUATION_YEAR)))
        year = int(raw_year) if raw_year else None
        degree = _clean_new_value(NEW_BYU_DEGREE, sr._text(payload.get(NEW_BYU_DEGREE)))
    # Every alumni column the shared apply will READ is set explicitly (to None),
    # so it is loaded state after the flush below. An attribute never set on a
    # new row is absent from its state, and reading one on a flushed row makes
    # SQLAlchemy lazy-load it, which an AsyncSession cannot do (MissingGreenlet).
    blank = {f.column: None for f in sr._FIELDS if f.group == "alumni"}
    alum = Alumni(
        **blank,
        is_alumni=is_alum,
        archived=False,
        deceased=False,
        graduation_year=year,
        byu_id=byu_id if is_alum and byu_id else None,
        profile_updated_by_user_id=actor_user_id,
    )
    session.add(alum)
    await session.flush()
    with audit_source_scope(AUDIT_SOURCE_SURVEY):
        _audit(session, actor_user_id, "create", alum.alumni_id, change_set_id=change_set_id)
        if year is not None:
            _audit(
                session,
                actor_user_id,
                "update",
                alum.alumni_id,
                field_name="graduation_year",
                new_value=year,
                change_set_id=change_set_id,
            )
        if alum.byu_id is not None:
            _audit(
                session,
                actor_user_id,
                "update",
                alum.alumni_id,
                field_name="byu_id",
                new_value=alum.byu_id,
                change_set_id=change_set_id,
            )
        if degree is not None:
            session.add(
                EducationHistory(alumni_id=alum.alumni_id, degree=degree, degree_year=year)
            )
            _audit(
                session,
                actor_user_id,
                "update",
                alum.alumni_id,
                field_name="education.degree",
                new_value=degree,
                change_set_id=change_set_id,
            )
    return alum


DECEASED_MESSAGE = "This person is marked deceased."


async def _live_alum(session: AsyncSession, alumni_id: int) -> Alumni:
    """A live (not archived) record, or 404."""
    alum = (
        await session.execute(select(Alumni).where(Alumni.alumni_id == alumni_id))
    ).scalar_one_or_none()
    if alum is None or alum.archived:
        raise NotFoundError("Alum not found.")
    return alum


def _populated(field, before: str) -> bool:
    """Whether the record's value for *field* is real data a merge would REPLACE.

    A yes/no answer reads "No" when nothing is on file (a missing engagement row
    or an unset flag), so for those only "Yes" counts as populated: turning an
    unset flag on is filling a blank, not overwriting one."""
    if field.kind in ("bool", "designation"):
        return before == "Yes"
    return bool(before)


async def _byu_owner(
    session: AsyncSession, byu_id: str, exclude_alumni_id: int | None = None
) -> tuple | None:
    """``(alumni_id, first_name, last_name)`` of the ACTIVE record holding
    *byu_id* (other than *exclude_alumni_id*), or None. The same scope as the
    ``uq_alumni_byu_id_active`` partial unique index."""
    stmt = select(Alumni.alumni_id, Alumni.first_name, Alumni.last_name).where(
        Alumni.archived.is_(False), Alumni.byu_id == byu_id
    )
    if exclude_alumni_id is not None:
        stmt = stmt.where(Alumni.alumni_id != exclude_alumni_id)
    return (await session.execute(stmt.order_by(Alumni.alumni_id.asc()).limit(1))).first()


def _byu_conflict_message(owner: tuple | None) -> str:
    name = _name_of(owner[1], owner[2], "another record") if owner else "another record"
    return f"That BYU ID already belongs to {name}. Use Update instead."


async def _ensure_byu_free(
    session: AsyncSession, byu_id: str, exclude_alumni_id: int | None = None
) -> None:
    """409 when another active record already holds *byu_id*. Checked BEFORE the
    write so the unique index never sees the collision; ``approve`` still maps
    the index's IntegrityError to the same 409 for a racing write."""
    owner = await _byu_owner(session, byu_id, exclude_alumni_id)
    if owner is not None:
        raise ConflictError(_byu_conflict_message(owner))


def _byu_change(alum: Alumni, payload: dict) -> tuple[str, str] | None:
    """``(before, after)`` when a merge would change *alum*'s BYU ID, else None.

    Compared as digits, so a legacy stored value with dashes that names the same
    ID is no change."""
    after = _byu_id_of(payload)
    if after is None:
        return None
    before = sr._text(alum.byu_id)
    if re.sub(r"[^0-9]", "", before) == after:
        return None
    return before, after


def _is_byu_index_violation(exc: IntegrityError) -> bool:
    return _BYU_ID_UNIQUE_INDEX in str(getattr(exc, "orig", None) or exc)


async def _merge_plan(session: AsyncSession, alum: Alumni, payload: dict) -> list[tuple]:
    """``(field, before, after)`` for every submitted survey field that a merge
    into *alum* would actually change, in form order, by the normal survey's own
    display rules (the same comparison its review queue diff makes)."""
    contacts, jobs, engs = await sr._load_side_rows(session, [alum.alumni_id])
    by_group = {
        "alumni": alum,
        "contact": contacts.get(alum.alumni_id),
        "employment": jobs.get(alum.alumni_id),
        "engagement": engs.get(alum.alumni_id),
    }
    plan = []
    for field in sr._FIELDS:
        if field.key not in payload:
            continue
        raw = payload[field.key]
        if sr._coerce(field, raw) is sr._IGNORE:
            continue
        before = sr._current(field, by_group.get(field.group))
        after = sr._after(field, raw)
        if before != after:
            plan.append((field, before, after))
    return plan


async def preview_merge(
    session: AsyncSession, submission_id: int, alumni_id: int
) -> NewPersonMergePreview:
    """What merging a pending submission into *alumni_id* would change. Reads
    only; nothing is locked or written here."""
    row = (
        await session.execute(
            select(NewPersonSubmission).where(NewPersonSubmission.submission_id == submission_id)
        )
    ).scalar_one_or_none()
    if row is None or row.status != STATUS_PENDING:
        raise NotFoundError(NOT_PENDING_MESSAGE)
    alum = await _live_alum(session, alumni_id)
    held = dict(row.payload or {})
    payload = {k: v for k, v in held.items() if k not in NEW_KEYS}
    plan = await _merge_plan(session, alum, payload)
    changes = [
        SurveyChange(field_key=f.key, label=f.label, before=before, after=after)
        for f, before, after in plan
    ]
    overwrites = [f.key for f, before, _ in plan if _populated(f, before)]
    # The BYU ID is the one `new.*` key a merge writes (fill-blank, like the rest).
    byu = _byu_change(alum, held)
    if byu is not None:
        before, after = byu
        changes.append(
            SurveyChange(
                field_key=NEW_BYU_ID, label=NEW_KEY_LABELS[NEW_BYU_ID], before=before, after=after
            )
        )
        if before:
            overwrites.append(NEW_BYU_ID)
    return NewPersonMergePreview(
        alumni_id=alum.alumni_id,
        name=_name_of(alum.first_name, alum.last_name, "Unnamed record"),
        deceased=bool(alum.deceased),
        changes=changes,
        overwrites=overwrites,
    )


async def approve(
    session: AsyncSession,
    submission_id: int,
    body: NewPersonApproveRequest,
    actor_user_id: int | None,
) -> NewPersonApproveResult:
    """Create a record from the submission, or merge it into one.

    BYU ID: both paths check that no OTHER active record holds it before
    writing (409 naming the holder). The partial unique index is the backstop
    for a write that races the check; its IntegrityError is rolled back and
    answered with the same 409, never a 500."""
    byu_written: list = []  # [(byu_id, exclude_alumni_id)] once a write is staged
    try:
        return await _approve(session, submission_id, body, actor_user_id, byu_written)
    except IntegrityError as exc:
        if not byu_written or not _is_byu_index_violation(exc):
            raise
        await session.rollback()
        byu_id, exclude = byu_written[0]
        log.warning("new-people survey: BYU ID collision on approve of %s", submission_id)
        raise ConflictError(
            _byu_conflict_message(await _byu_owner(session, byu_id, exclude))
        ) from None


async def _approve(
    session: AsyncSession,
    submission_id: int,
    body: NewPersonApproveRequest,
    actor_user_id: int | None,
    byu_written: list,
) -> NewPersonApproveResult:
    row = await _get_pending_locked(session, submission_id)
    if not _same_instant(body.submitted_at, row.submitted_at):
        raise ConflictError(STALE_MESSAGE)

    payload = dict(row.payload or {})
    # The survey fields only; the `new.*` keys are handled by the create step
    # (on merge only the BYU ID is used, below).
    apply_payload = {k: v for k, v in payload.items() if k not in NEW_KEYS and k != COMMENTS_KEY}
    if row.comments:
        apply_payload[COMMENTS_KEY] = row.comments

    change_set_id = new_change_set_id()
    notes: list[str] = []
    summary_extra = f" action={body.action}"
    if body.action == "merge":
        if body.alumni_id is None:
            raise InvalidRequestError("Choose the record to update.")
        alum = await _live_alum(session, body.alumni_id)
        if alum.deceased:
            raise ConflictError(DECEASED_MESSAGE)
        if (row.kind == "alum") != bool(alum.is_alumni):
            notes.append("kind_mismatch")
        # FILL BLANKS ONLY unless told otherwise: a populated field is replaced
        # only when the reviewer listed it in `overwrite_fields` (after seeing
        # it in the preview). An unrecognised key there is simply never matched.
        overwrite = set(body.overwrite_fields)
        plan = await _merge_plan(session, alum, apply_payload)
        kept = [f.key for f, before, _ in plan if _populated(f, before) and f.key not in overwrite]
        allowed = {f.key for f, _, _ in plan} - set(kept)
        apply_payload = {
            k: v for k, v in apply_payload.items() if k in allowed or k == COMMENTS_KEY
        }
        # The BYU ID: fill a blank, replace a populated one only when listed.
        byu = _byu_change(alum, payload)
        if byu is not None and byu[0] and NEW_BYU_ID not in overwrite:
            kept.append(NEW_BYU_ID)
        elif byu is not None:
            before, after = byu
            await _ensure_byu_free(session, after, alum.alumni_id)
            byu_written.append((after, alum.alumni_id))
            alum.byu_id = after
            with audit_source_scope(AUDIT_SOURCE_SURVEY):
                _audit(
                    session,
                    actor_user_id,
                    "update",
                    alum.alumni_id,
                    field_name="byu_id",
                    old_value=before or None,
                    new_value=after,
                    change_set_id=change_set_id,
                )
        summary_extra += f" kept={len(kept)}"
        kind = row.kind
        created = False
    else:
        kind = body.kind or row.kind
        # A friend record never takes a BYU ID (like the year and degree).
        byu_id = _byu_id_of(payload) if kind == "alum" else None
        if byu_id:
            await _ensure_byu_free(session, byu_id)
            byu_written.append((byu_id, None))
        alum = await _create_record(
            session, payload, kind, actor_user_id, change_set_id, byu_id=byu_id
        )
        created = True

    alumni_id = alum.alumni_id
    warnings = await sr.apply_payload_to_record(
        session,
        alum,
        apply_payload,
        actor_user_id=actor_user_id,
        submitted_at=row.submitted_at,
        summary_action="apply_new_person_submission",
        summary_ref=f"new_people_submission={submission_id}",
        log_label=f"New-people submission {submission_id}",
        summary_extra=summary_extra,
        change_set_id=change_set_id,
    )
    _purge(row, STATUS_APPROVED, actor_user_id)
    row.alumni_id = alumni_id
    # Ids and the decision only: the answers are in the alumni record's own
    # field-level audit rows above.
    session.add(
        AuditLog(
            user_id=actor_user_id,
            action_type="approve_new_person_submission",
            entity_type="new_people_submission",
            entity_id=submission_id,
            new_value=f"action={body.action} kind={kind} alumni_id={alumni_id}",
        )
    )
    await session.commit()
    return NewPersonApproveResult(
        alumni_id=alumni_id, created=created, duplicate_warnings=warnings, warnings=notes
    )


async def reject(session: AsyncSession, submission_id: int, actor_user_id: int | None) -> None:
    """Refuse a submission: purge the held answers, keep the bare row and an
    audit entry that says it happened (no PII)."""
    row = await _get_pending_locked(session, submission_id)
    kind = row.kind
    _purge(row, STATUS_REJECTED, actor_user_id)
    session.add(
        AuditLog(
            user_id=actor_user_id,
            action_type="reject_new_person_submission",
            entity_type="new_people_submission",
            entity_id=submission_id,
            new_value=f"kind={kind}",
        )
    )
    await session.commit()


async def reject_many(
    session: AsyncSession, submission_ids: list[int], actor_user_id: int | None
) -> int:
    """Bulk reject: every id that is still pending is purged like a single
    reject; ids that are not pending (or do not exist) are skipped. Rows are
    locked in id order so two overlapping bulk rejects cannot deadlock. ONE
    audit row records the count."""
    ids = sorted(set(submission_ids))
    rows = (
        (
            await session.execute(
                select(NewPersonSubmission)
                .where(
                    NewPersonSubmission.submission_id.in_(ids),
                    NewPersonSubmission.status == STATUS_PENDING,
                )
                .order_by(NewPersonSubmission.submission_id.asc())
                .with_for_update()
            )
        )
        .scalars()
        .all()
    )
    for row in rows:
        _purge(row, STATUS_REJECTED, actor_user_id)
    session.add(
        AuditLog(
            user_id=actor_user_id,
            action_type="bulk_reject_new_person_submissions",
            entity_type="new_people_submission",
            new_value=f"rejected={len(rows)} requested={len(ids)}",
        )
    )
    await session.commit()
    return len(rows)

