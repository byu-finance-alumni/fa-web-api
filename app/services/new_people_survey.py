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

What is NEW is small: the two BYU-alum keys (``new.graduation_year``,
``new.byu_degree``), the required ANY-OF groups (the normal survey has none,
because it edits a record that already exists), the link itself, and the
global cap on pending rows.

NOT COUNTED. Held rows live only in ``new_people_submissions``, which nothing
outside this module reads, so they never reach alumni counts, KPIs, campaign
tallies, ``survey_responses`` or the normal review queue.
"""

from __future__ import annotations

import datetime
import hmac
import logging
import re
import secrets

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit_context import AUDIT_SOURCE_SURVEY, audit_source_scope, new_change_set_id
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
    NewPersonPossibleMatch,
    NewPersonSubmissionField,
    NewPersonSubmissionItem,
)
from app.schemas.survey import SURVEY_COMMENTS_MAX, clean_survey_comment
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

# The single link row's id (a CHECK pins it).
_LINK_ID = 1
# `secrets.token_urlsafe(32)` is 43 URL-safe characters (256 bits).
_KEY_BYTES = 32
_KEY_MAX = 128

# The two keys only the BYU-alum path carries.
NEW_GRADUATION_YEAR = "new.graduation_year"
NEW_BYU_DEGREE = "new.byu_degree"
NEW_KEYS = (NEW_GRADUATION_YEAR, NEW_BYU_DEGREE)
NEW_KEY_LABELS = {
    NEW_GRADUATION_YEAR: "BYU graduation year",
    NEW_BYU_DEGREE: "BYU degree",
}
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
    ("employment.current_country",),
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
    return None


def clean_fields(kind: str, fields: dict[str, str]) -> tuple[dict[str, str], set[str]]:
    """``(payload, refused)`` for a public submission.

    *payload* holds only recognised, non-blank answers the apply path would
    actually write, by the normal survey's own ``_coerce`` rules (plus the two
    ``new.*`` keys on the alum path). Unknown keys are dropped silently, exactly
    as the normal survey drops them. *refused* names known keys whose answer was
    not usable, so a REQUIRED one can be reported back by key; an optional one
    is simply left out, which is what the normal survey does with it.
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
    """The error-envelope ``fields`` entries for every unmet required group."""
    errors: list[dict[str, str]] = []
    for group in _required_for(kind):
        if any(payload.get(k) for k in group):
            continue
        for key in group:
            if group == _EMAIL_GROUP:
                message = "Enter a personal or work email."
            elif key in refused:
                message = "This answer is not valid."
            else:
                message = "This field is required."
            errors.append({"field": key, "message": message})
    return errors


class MissingRequiredFields(Exception):
    """A submission is missing required answers. Carries the envelope fields."""

    def __init__(self, fields: list[dict[str, str]]) -> None:
        self.fields = fields
        super().__init__("Please answer the required questions.")


class PendingCapReached(Exception):
    """The holding queue is full; refuse politely (429)."""


# ------------------------------------------------------------------- submit --


async def submit(
    session: AsyncSession,
    *,
    kind: str,
    fields: dict[str, str],
    comments: str | None,
    fill_seconds: int | None,
) -> None:
    """Hold one submission for review. The caller has already checked the key,
    the size caps and the honeypot."""
    payload, refused = clean_fields(kind, fields)
    errors = missing_required(kind, payload, refused)
    if errors:
        raise MissingRequiredFields(errors)

    pending = await session.scalar(
        select(func.count())
        .select_from(NewPersonSubmission)
        .where(NewPersonSubmission.status == STATUS_PENDING)
    )
    if (pending or 0) >= PENDING_CAP:
        log.warning("new-people survey: pending cap (%d) reached; submission refused", PENDING_CAP)
        raise PendingCapReached()

    comment = clean_survey_comment(comments)
    if comment is not None and len(comment) > SURVEY_COMMENTS_MAX:
        comment = None
    session.add(
        NewPersonSubmission(
            kind=kind,
            payload=payload,
            comments=comment,
            fill_seconds=sr._sane_fill_seconds(fill_seconds),
            status=STATUS_PENDING,
        )
    )
    await session.commit()
    # Kind and counts only: never a submitted value.
    log.info("new-people survey: held a %s submission (%d fields)", kind, len(payload))


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


def _name_key(payload: dict) -> tuple[str, str] | None:
    first = sr._text(payload.get("profile.first_name")).lower()
    last = sr._text(payload.get("profile.last_name")).lower()
    return (first, last) if first and last else None


async def _possible_matches(
    session: AsyncSession, payloads: list[dict]
) -> list[list[NewPersonPossibleMatch]]:
    """Live records that may be each payload's person, in two batched queries:
    case-insensitive email against personal/work email, then first + last name
    (preferred first name counts). Friends included. Email hits come first;
    capped at ``_MATCH_CAP`` per payload."""
    all_emails = sorted({e for p in payloads for e in _emails(p)})
    all_lasts = sorted({k[1] for p in payloads if (k := _name_key(p))})

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
        candidates = [(row, "email") for e in _emails(payload) for row in by_email.get(e, [])]
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
) -> Alumni:
    """A brand-new alum or friend record, holding only what the survey fields
    cannot write: the kind, and for an alum the BYU graduation year (the real
    ``alumni.graduation_year`` column) and degree (an ``education_history``
    row, the profile's Education section). Everything else is written by the
    shared apply afterwards.

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


async def approve(
    session: AsyncSession,
    submission_id: int,
    body: NewPersonApproveRequest,
    actor_user_id: int | None,
) -> NewPersonApproveResult:
    row = await _get_pending_locked(session, submission_id)
    if not _same_instant(body.submitted_at, row.submitted_at):
        raise ConflictError(STALE_MESSAGE)

    payload = dict(row.payload or {})
    # The survey fields only; the `new.*` keys are handled by the create step
    # (and ignored on merge).
    apply_payload = {k: v for k, v in payload.items() if k not in NEW_KEYS and k != COMMENTS_KEY}
    if row.comments:
        apply_payload[COMMENTS_KEY] = row.comments

    change_set_id = new_change_set_id()
    if body.action == "merge":
        if body.alumni_id is None:
            raise InvalidRequestError("Choose the record to update.")
        alum = (
            await session.execute(select(Alumni).where(Alumni.alumni_id == body.alumni_id))
        ).scalar_one_or_none()
        if alum is None or alum.archived:
            raise NotFoundError("Alum not found.")
        kind = row.kind
        created = False
    else:
        kind = body.kind or row.kind
        alum = await _create_record(session, payload, kind, actor_user_id, change_set_id)
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
        summary_extra=f" action={body.action}",
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
        alumni_id=alumni_id, created=created, duplicate_warnings=warnings
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
