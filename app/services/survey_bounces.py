"""Survey email bounces: record Resend's webhook events, list the hard bounces.

fa-web-app #858. Staff want to see whose survey email bounced so they can fix
the address. Resend reports it on its webhook; this module turns an already
signature-verified delivery into a ``survey_email_events`` row and serves the
console's per-year list.

Owner decisions this encodes:

* PERMANENT bounces only are listed. Transient / undetermined ones are stored
  (cheap, and useful if the policy changes) but never shown.
* It is a LIST. Nothing here marks anyone unreachable or touches alumni data.
* No backfill: emails sent before the message ids were kept cannot be matched.

PRIVACY: no email address and no raw payload is ever written to the event row or
logged. The address the console shows is the one already in
``survey_send_log.sent_to``.
"""

from __future__ import annotations

import datetime
import logging
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.dropdowns import SUPPRESSED_CONTACT_STATUS_LABELS
from app.models.alumni import Alumni
from app.models.contact import AlumniContactInfo
from app.models.survey_email_event import (
    BOUNCE_PERMANENT,
    EVENT_BOUNCED,
    EVENT_COMPLAINED,
    SurveyEmailEvent,
)
from app.models.survey_schedule import SurveySendLog
from app.repositories.alumni import build_alumni_query
from app.schemas.survey import SurveyBouncedAlum, SurveyBouncedPage

log = logging.getLogger(__name__)

#: The event types we keep. Everything else Resend sends is acknowledged and
#: dropped (a 2xx, so Svix does not keep retrying it).
STORED_EVENT_TYPES = frozenset({EVENT_BOUNCED, EVENT_COMPLAINED})

_MAX_ID_LEN = 100
_MAX_TYPE_LEN = 40
_MAX_SUBTYPE_LEN = 60
_GRAD_YEAR_RANGE = (1900, 2100)

#: The bounced list's page size: default and ceiling, the same as the held-out
#: list (`survey_email.HELD_OUT_PAGE_DEFAULT` / `HELD_OUT_PAGE_MAX`).
BOUNCED_PAGE_DEFAULT = 200
BOUNCED_PAGE_MAX = 1000


def _short_str(value: Any, limit: int) -> str | None:
    if not isinstance(value, str):
        return None
    value = value.strip()
    if not value:
        return None
    return value[:limit]


def _parse_when(*candidates: Any) -> datetime.datetime | None:
    for raw in candidates:
        if not isinstance(raw, str) or not raw.strip():
            continue
        try:
            when = datetime.datetime.fromisoformat(raw.strip().replace("Z", "+00:00"))
        except ValueError:
            continue
        if when.tzinfo is None:
            when = when.replace(tzinfo=datetime.UTC)
        return when
    return None


def _as_int(value: Any, lo: int, hi: int) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        n = int(str(value).strip())
    except (TypeError, ValueError):
        return None
    return n if lo <= n <= hi else None


def _tags(data: dict) -> dict[str, str]:
    """Resend's ``tags`` as a plain dict.

    Webhooks echo them as an object (``{"alumni_id": "12"}``); the send API takes
    a list of ``{"name", "value"}`` pairs. Accept both shapes."""
    raw = data.get("tags")
    out: dict[str, str] = {}
    if isinstance(raw, dict):
        for k, v in raw.items():
            if isinstance(k, str) and isinstance(v, str | int):
                out[k] = str(v)
    elif isinstance(raw, list):
        for item in raw:
            if isinstance(item, dict):
                k, v = item.get("name"), item.get("value")
                if isinstance(k, str) and isinstance(v, str | int):
                    out[k] = str(v)
    return out


async def _match(
    session: AsyncSession, email_id: str | None, data: dict
) -> tuple[int | None, int | None]:
    """``(alumni_id, graduation_year)`` for the email, or ``(None, None)``.

    First by the Resend message id recorded on the send log at send time; when
    that is absent (bookkeeping failed, or the id never came back) by the
    ``alumni_id`` / ``graduation_year`` tags the sender puts on every email. A
    tag match is only accepted if that alum still exists -- the event row has a
    foreign key, and a tag for a deleted alum must not fail the insert."""
    tags = _tags(data)
    tag_alumni_id = _as_int(tags.get("alumni_id"), 1, 2**63 - 1)
    if email_id:
        row = (
            await session.execute(
                select(SurveySendLog.alumni_id, SurveySendLog.graduation_year)
                .where(SurveySendLog.resend_email_id == email_id)
                .limit(1)
            )
        ).first()
        if row is not None:
            # The send-log id was paired with its recipient BY POSITION in
            # Resend's batch response. The tag rode on the email itself. If the
            # two disagree, one of them is wrong and we cannot tell which --
            # naming nobody beats pinning a bounce on the wrong alum.
            if tag_alumni_id is not None and tag_alumni_id != int(row[0]):
                log.warning(
                    "resend webhook: send-log alum and alumni_id tag disagree; "
                    "bounce left unattributed"
                )
                return None, None
            return int(row[0]), int(row[1])

    alumni_id = tag_alumni_id
    year = _as_int(tags.get("graduation_year"), *_GRAD_YEAR_RANGE)
    if alumni_id is None:
        return None, None
    exists = (
        await session.execute(
            select(Alumni.alumni_id).where(Alumni.alumni_id == alumni_id).limit(1)
        )
    ).first()
    if exists is None:
        return None, None
    return alumni_id, year


async def _insert_event(session: AsyncSession, values: dict[str, Any]):
    """INSERT ... ON CONFLICT (svix_id) DO NOTHING, committed. Returns the new
    row's id tuple, or None when this svix_id was already stored."""
    stmt = (
        pg_insert(SurveyEmailEvent)
        .values(**values)
        .on_conflict_do_nothing(index_elements=["svix_id"])
        .returning(SurveyEmailEvent.survey_email_event_id)
    )
    inserted = (await session.execute(stmt)).first()
    await session.commit()
    return inserted


async def record_webhook_event(
    session: AsyncSession, *, svix_id: str, payload: Any
) -> str:
    """Store one verified Resend delivery. Returns what happened:

    * ``"ignored"``  -- not a bounce/complaint, or not a usable payload;
    * ``"duplicate"`` -- this ``svix_id`` was already stored (a redelivery);
    * ``"stored"``   -- a new row.

    Idempotent on ``svix_id`` via ``ON CONFLICT DO NOTHING``, so two concurrent
    deliveries of the same event cannot both insert."""
    if not isinstance(payload, dict) or not svix_id or len(svix_id) > _MAX_ID_LEN:
        # The route refuses an over-long svix-id; never truncate the
        # idempotency key here (two truncated ids could collide).
        return "ignored"
    event_type = payload.get("type")
    if event_type not in STORED_EVENT_TYPES:
        return "ignored"
    data = payload.get("data")
    if not isinstance(data, dict):
        data = {}

    email_id = _short_str(data.get("email_id"), _MAX_ID_LEN)
    bounce_type = bounce_subtype = None
    if event_type == EVENT_BOUNCED:
        bounce = data.get("bounce")
        if isinstance(bounce, dict):
            bt = _short_str(bounce.get("type"), _MAX_TYPE_LEN)
            bounce_type = bt.lower() if bt else None
            bounce_subtype = _short_str(
                bounce.get("subType") or bounce.get("subtype"), _MAX_SUBTYPE_LEN
            )

    alumni_id, graduation_year = await _match(session, email_id, data)
    occurred_at = _parse_when(payload.get("created_at"), data.get("created_at"))

    values: dict[str, Any] = {
        "svix_id": svix_id,
        "resend_email_id": email_id,
        "alumni_id": alumni_id,
        "graduation_year": graduation_year,
        "event_type": event_type,
        "bounce_type": bounce_type,
        "bounce_subtype": bounce_subtype,
    }
    if occurred_at is not None:
        values["occurred_at"] = occurred_at

    try:
        inserted = await _insert_event(session, values)
    except IntegrityError:
        # The tag-matched alum was deleted between the existence check and this
        # insert (the FK refuses it). Keep the event, unattributed. svix_id
        # idempotency is untouched: the failed insert stored nothing, and the
        # retry is the same ON CONFLICT DO NOTHING.
        await session.rollback()
        if values.get("alumni_id") is None:
            raise
        values["alumni_id"] = None
        values["graduation_year"] = None
        alumni_id = None
        inserted = await _insert_event(session, values)
    outcome = "stored" if inserted is not None else "duplicate"
    # Ids and types only -- never an address, never the body.
    log.info(
        "resend webhook %s: type=%s bounce_type=%s matched=%s",
        outcome,
        event_type,
        bounce_type,
        alumni_id is not None,
    )
    return outcome


def _display_name(a: Alumni) -> str:
    name = " ".join(
        p for p in (a.preferred_first_name or a.first_name, a.last_name) if p
    ).strip()
    return name or f"Alum #{a.alumni_id}"


async def list_bounced(
    session: AsyncSession,
    graduation_year: int,
    *,
    limit: int = BOUNCED_PAGE_DEFAULT,
) -> SurveyBouncedPage:
    """Alumni whose survey email for ``graduation_year`` PERMANENTLY bounced.

    Capped at ``limit`` names (``total`` is the uncapped count). The list is one
    graduation year's hard bounces, so the uncapped set is small by nature; the
    cap bounds the disclosure per read regardless.

    One row per alumnus -- their most recent permanent bounce -- ordered by name
    so it reads like a worklist. Each row carries the address that bounced (from
    the send log) and whether it is still on the profile, so staff can see at a
    glance whether someone has already fixed it."""
    # The same population rule as `/unreachable` (`survey_email._survey_cohort_query`):
    # live alumni only -- not archived, flagged as alumni, not deceased, not Do
    # Not Contact. An archived or suppressed person is not someone staff should
    # be sent to chase an address for. The cohort's WHERE is applied to the
    # joined Alumni directly (as `repositories.alumni` does for its row query),
    # so its correlated EXISTS binds to this query's alumni row.
    cohort = build_alumni_query(
        deceased=False, suppress_labels=SUPPRESSED_CONTACT_STATUS_LABELS
    )
    stmt = (
        select(SurveyEmailEvent, SurveySendLog.sent_to, Alumni)
        .join(Alumni, Alumni.alumni_id == SurveyEmailEvent.alumni_id)
        .outerjoin(
            SurveySendLog,
            SurveySendLog.resend_email_id == SurveyEmailEvent.resend_email_id,
        )
        .where(
            SurveyEmailEvent.graduation_year == graduation_year,
            SurveyEmailEvent.event_type == EVENT_BOUNCED,
            SurveyEmailEvent.bounce_type == BOUNCE_PERMANENT,
        )
    )
    if cohort.whereclause is not None:
        stmt = stmt.where(cohort.whereclause)
    rows = (
        await session.execute(
            stmt
            .order_by(
                SurveyEmailEvent.occurred_at.desc(),
                SurveyEmailEvent.survey_email_event_id.desc(),
            )
        )
    ).all()
    latest: dict[int, tuple[SurveyEmailEvent, str | None]] = {}
    alumni: dict[int, Alumni] = {}
    for event, sent_to, alum in rows:
        if event.alumni_id not in latest:
            latest[event.alumni_id] = (event, sent_to)
            alumni[event.alumni_id] = alum
    if not latest:
        return SurveyBouncedPage(
            graduation_year=graduation_year, total=0, limit=limit, items=[]
        )

    ids = list(latest)
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

    items: list[SurveyBouncedAlum] = []
    for alumni_id, (event, sent_to) in latest.items():
        a = alumni.get(alumni_id)
        if a is None:
            continue
        still_on_file: bool | None = None
        if sent_to:
            contact = contacts.get(alumni_id)
            on_file = {
                (v or "").strip().lower()
                for v in (
                    getattr(contact, "personal_email", None),
                    getattr(contact, "work_email", None),
                )
                if v
            }
            still_on_file = sent_to.strip().lower() in on_file
        items.append(
            SurveyBouncedAlum(
                alumni_id=alumni_id,
                name=_display_name(a),
                bounced_address=sent_to,
                bounce_subtype=event.bounce_subtype,
                bounced_at=event.occurred_at,
                address_still_on_file=still_on_file,
            )
        )
    items.sort(key=lambda i: (i.name.lower(), i.alumni_id))
    return SurveyBouncedPage(
        graduation_year=graduation_year,
        total=len(items),
        limit=limit,
        items=items[: max(limit, 0)],
    )
