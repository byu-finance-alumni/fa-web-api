"""Send the survey to ONE alum from their profile (api#669).

Same email, same copy, same 7-day link and same send rules as a class send --
just aimed at one person. Decisions (Jake, 2026-11-11):

* Anyone who can edit alumni may press it (``RequireAlumniEdit``).
* Re-sending is allowed. The confirm dialog warns "Last sent on <date>".
* The class's later send does NOT skip them. That is why one-off emails are
  logged in their own tables (``models.survey_one_off``) and never in
  ``survey_send_log``: a row there is the class campaign's "already emailed"
  guard, keyed by cycle, and a class year can only be sent once.
* Same reminders: day 7 and day 14 if they have not replied. The daily cron
  sends them (:func:`send_due_reminders`, called from
  ``survey_schedule._run_due_schedules_locked``).

What it SHARES with every other send, so the rules cannot drift:

* the render and the link (``survey_email._build_survey_email``), the staff
  message (``survey_message.get_for_send``) and the reminder note on stages 1/2;
* the recipient address rule (``email_reach.resolve_email``: personal, then
  work) and the deliverability gate;
* suppression (``survey_email._suppressed_from_send``: deceased flag, or a
  Deceased / Do Not Contact label);
* the send lock (``survey_email.send_lock``) and the account-wide budget
  (``survey_email.remaining_send_allowance``). Every one-off email is a row in
  ``survey_one_off_send_log``, which ``survey_email.get_send_usage`` counts;
* claim-before-send: the log row is committed before Resend is called, released
  if Resend definitively refuses, kept if the outcome is unknown.

"Kill switch or pause like any other send": the class campaigns are stopped per
graduation year (pause, pause-all, cancel, cancel-all all set the year's
``survey_schedule.status``). A one-off is refused, and its reminders are held,
while the alum's OWN class campaign is ``paused`` or ``cancelled`` -- exactly
the years a class send would also be stopped for.

⚠️ NO UNSUBSCRIBE LINK, by standing project rule: alumni emails never get one.
"""

from __future__ import annotations

import asyncio
import datetime
import logging
from contextlib import suppress
from dataclasses import dataclass

from sqlalchemy import delete, func, insert, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from app.core import email_reach
from app.core.errors import ConflictError, NotFoundError, ServiceError
from app.models.alumni import Alumni
from app.models.audit import AuditLog
from app.models.contact import AlumniContactInfo
from app.models.employment import CurrentEmployment
from app.models.survey_one_off import SurveyOneOff, SurveyOneOffSendLog
from app.models.survey_response import SurveyResponse
from app.models.survey_schedule import SurveySchedule, SurveySendLog
from app.schemas.survey import SurveyOneOffPreview, SurveyOneOffResult
from app.services import survey_email, survey_message

log = logging.getLogger(__name__)

STAGE_INITIAL = survey_email.STAGE_INITIAL
STAGE_REMINDER_1 = survey_email.STAGE_REMINDER_1
STAGE_REMINDER_2 = survey_email.STAGE_REMINDER_2
_STAGE_WINDOW_DAYS = survey_email._STAGE_WINDOW_DAYS

#: A one-off older than this gets no more reminders. Without a cut-off a long
#: pause of the class campaign would fire a "reminder" months later.
REMINDER_GIVE_UP_DAYS = 28

AUDIT_ACTION = "send_survey_one_off"

# Stable reason codes (the frontend may key on them) and their plain wording.
REASON_NOT_ALUMNI = "not_alumni"
REASON_ARCHIVED = "archived"
REASON_SUPPRESSED = "suppressed"
REASON_UNREACHABLE = "unreachable"
REASON_NO_YEAR = "no_graduation_year"
REASON_CAMPAIGN_PAUSED = "campaign_paused"
REASON_CAMPAIGN_CANCELLED = "campaign_cancelled"

REASON_TEXT = {
    REASON_NOT_ALUMNI: "Only alumni get the survey.",
    REASON_ARCHIVED: "This record is archived.",
    REASON_SUPPRESSED: "Marked deceased or Do Not Contact.",
    REASON_UNREACHABLE: "No usable email address on file.",
    REASON_NO_YEAR: "No graduation year on file.",
    REASON_CAMPAIGN_PAUSED: "Their class's survey campaign is paused.",
    REASON_CAMPAIGN_CANCELLED: "Their class's survey campaign was stopped.",
}

BUSY_MESSAGE = (
    "A survey send is running right now. Try again in a minute. Nothing was sent."
)
BUDGET_MESSAGE = (
    "Today's or this month's survey email limit is used up. Nothing was sent."
)
THROTTLED_MESSAGE = (
    "The email service asked us to slow down. Try again in a minute. "
    "Nothing was sent."
)

_STOPPED_STATUSES = {
    "paused": REASON_CAMPAIGN_PAUSED,
    "cancelled": REASON_CAMPAIGN_CANCELLED,
}


def _now() -> datetime.datetime:
    return datetime.datetime.now(datetime.UTC)


def _as_utc(value: datetime.datetime) -> datetime.datetime:
    return value.replace(tzinfo=datetime.UTC) if value.tzinfo is None else value


# --------------------------------------------------------------- resolving ----


@dataclass(frozen=True)
class _Target:
    """Everything a one-off needs to know about the alum, resolved once."""

    alumni_id: int
    name: str
    graduation_year: int | None
    reason_code: str | None
    recipient: survey_email.Recipient | None


async def _campaign_status(session: AsyncSession, graduation_year: int) -> str | None:
    return (
        await session.execute(
            select(SurveySchedule.status).where(
                SurveySchedule.graduation_year == graduation_year
            )
        )
    ).scalar_one_or_none()


async def _is_suppressed(session: AsyncSession, alumni_id: int) -> bool:
    """The class send's own suppression predicate, asked of one alum."""
    row = (
        await session.execute(
            select(Alumni.alumni_id).where(
                Alumni.alumni_id == alumni_id, survey_email._suppressed_from_send()
            )
        )
    ).first()
    return row is not None


async def _resolve(session: AsyncSession, alumni_id: int) -> _Target:
    """Who this is, where the email would go, and why it can't (if it can't).

    Raises :class:`NotFoundError` for an id that does not exist. The checks run
    in a fixed order so the reason shown is the most important one."""
    alum = (
        await session.execute(select(Alumni).where(Alumni.alumni_id == alumni_id))
    ).scalar_one_or_none()
    if alum is None:
        raise NotFoundError("Alumni not found.")
    first = (alum.preferred_first_name or alum.first_name or "").strip()
    name = " ".join(p for p in (first, (alum.last_name or "").strip()) if p) or "This alum"
    year = alum.graduation_year

    def _target(reason: str | None, recipient=None) -> _Target:
        return _Target(alumni_id, name, year, reason, recipient)

    if not alum.is_alumni:
        return _target(REASON_NOT_ALUMNI)
    if alum.archived:
        return _target(REASON_ARCHIVED)
    if alum.deceased or await _is_suppressed(session, alumni_id):
        return _target(REASON_SUPPRESSED)

    contact = (
        await session.execute(
            select(AlumniContactInfo).where(AlumniContactInfo.alumni_id == alumni_id)
        )
    ).scalar_one_or_none()
    email, source = email_reach.resolve_email(
        getattr(contact, "personal_email", None),
        getattr(contact, "work_email", None),
    )
    if email is None:
        return _target(REASON_UNREACHABLE)
    job = (
        await session.execute(
            select(CurrentEmployment).where(CurrentEmployment.alumni_id == alumni_id)
        )
    ).scalar_one_or_none()
    recipient = survey_email.Recipient(
        alumni_id=alumni_id,
        first_name=(alum.preferred_first_name or alum.first_name or "there"),
        email=email,
        on_file=survey_email._build_on_file(alum, contact, job),
        email_source=source or email_reach.SOURCE_PERSONAL,
    )
    if year is None:
        return _target(REASON_NO_YEAR, recipient)
    status = await _campaign_status(session, year)
    if status in _STOPPED_STATUSES:
        return _target(_STOPPED_STATUSES[status], recipient)
    return _target(None, recipient)


async def _last_one_off_sent_at(
    session: AsyncSession, alumni_id: int
) -> datetime.datetime | None:
    """Newest one-off email for this alum. Never raises: a schema without the
    one-off tables (migration not applied yet) reads as "none"."""
    stmt = (
        select(func.max(SurveyOneOffSendLog.sent_at))
        .join(
            SurveyOneOff,
            SurveyOneOff.survey_one_off_id == SurveyOneOffSendLog.survey_one_off_id,
        )
        .where(SurveyOneOff.alumni_id == alumni_id)
    )
    try:
        async with session.begin_nested():
            return (await session.execute(stmt)).scalar()
    except Exception:  # noqa: BLE001 - a missing table must not break the page
        log.warning("survey_one_off: could not read the one-off send log")
        return None


async def sends_for_alum(
    session: AsyncSession, alumni_id: int
) -> list[tuple[datetime.datetime, int]]:
    """``(sent_at, stage)`` of every one-off email to this alum, oldest first --
    for the profile's Surveys tab. Never raises (an un-migrated schema reads as
    no one-off sends)."""
    stmt = (
        select(SurveyOneOffSendLog.sent_at, SurveyOneOffSendLog.stage)
        .join(
            SurveyOneOff,
            SurveyOneOff.survey_one_off_id == SurveyOneOffSendLog.survey_one_off_id,
        )
        .where(SurveyOneOff.alumni_id == alumni_id)
        .order_by(SurveyOneOffSendLog.sent_at)
    )
    try:
        async with session.begin_nested():
            rows = (await session.execute(stmt)).all()
    except Exception:  # noqa: BLE001 - a missing table must not break the profile
        log.warning("survey_one_off: could not read one-off sends for a profile")
        return []
    return [(r[0], int(r[1])) for r in rows]


async def last_sent_at(session: AsyncSession, alumni_id: int) -> datetime.datetime | None:
    """The last survey email of ANY kind -- class campaign or one-off."""
    class_last = (
        await session.execute(
            select(func.max(SurveySendLog.sent_at)).where(
                SurveySendLog.alumni_id == alumni_id
            )
        )
    ).scalar()
    one_off_last = await _last_one_off_sent_at(session, alumni_id)
    stamps = [_as_utc(s) for s in (class_last, one_off_last) if s is not None]
    return max(stamps) if stamps else None


async def _last_replied_at(
    session: AsyncSession, alumni_id: int
) -> datetime.datetime | None:
    """Their newest reply inside the 365-day window -- the same rows that hold
    them out of a class send (``survey_email._recent_reply_criteria``)."""
    return (
        await session.execute(
            select(func.max(SurveyResponse.submitted_at)).where(
                SurveyResponse.alumni_id == alumni_id,
                SurveyResponse.submitted_at >= survey_email._resurvey_cutoff(),
                SurveyResponse.status.in_(survey_email.RESPONDED_STATUSES),
                survey_email.response_not_superseded(),
            )
        )
    ).scalar()


async def get_preview(session: AsyncSession, alumni_id: int) -> SurveyOneOffPreview:
    """What the profile's "Send survey" button shows before anything is sent."""
    target = await _resolve(session, alumni_id)
    r = target.recipient
    return SurveyOneOffPreview(
        alumni_id=alumni_id,
        name=target.name,
        graduation_year=target.graduation_year,
        email=r.email if r else None,
        email_source=r.email_source if r else None,
        can_send=target.reason_code is None,
        reason_code=target.reason_code,
        reason=REASON_TEXT.get(target.reason_code) if target.reason_code else None,
        last_sent_at=await last_sent_at(session, alumni_id),
        last_replied_at=await _last_replied_at(session, alumni_id),
    )


# ----------------------------------------------------------------- sending ----


def _from_field() -> tuple[str, str]:
    settings = survey_email.get_settings()
    base_url = settings.survey_app_base_url
    from_email = settings.survey_from_email
    if not base_url:
        raise ServiceError("SURVEY_APP_BASE_URL is not configured.")
    if not from_email:
        raise ServiceError("SURVEY_FROM_EMAIL is not configured.")
    return base_url, f"{settings.survey_from_name} <{from_email}>"


def _build_email(
    r: survey_email.Recipient,
    *,
    graduation_year: int,
    base_url: str,
    from_field: str,
    message: survey_message.SurveyMessage,
    stage: int,
) -> dict:
    """The class send's own email, with one extra tag so a bounce or a log line
    can tell a one-off from a class send. Tags carry no PII."""
    email = survey_email._build_survey_email(
        r,
        graduation_year=graduation_year,
        base_url=base_url,
        from_field=from_field,
        message=message,
        stage=stage,
    )
    email["tags"] = [*email.get("tags", []), {"name": "one_off", "value": "1"}]
    return email


async def _release(session: AsyncSession, log_ids: list[int]) -> None:
    """Undo claims Resend definitively refused (429 / non-2xx)."""
    if not log_ids:
        return
    await session.execute(
        delete(SurveyOneOffSendLog).where(
            SurveyOneOffSendLog.survey_one_off_send_log_id.in_(log_ids)
        )
    )
    await session.commit()


async def _record_ids(
    session: AsyncSession, log_ids: list[int], message_ids: list[str] | None
) -> None:
    """Best effort: write Resend's message ids onto the rows just sent."""
    if not message_ids or len(message_ids) != len(log_ids):
        return
    try:
        for log_id, message_id in zip(log_ids, message_ids, strict=True):
            await session.execute(
                update(SurveyOneOffSendLog)
                .where(SurveyOneOffSendLog.survey_one_off_send_log_id == log_id)
                .values(resend_email_id=message_id)
            )
        await session.commit()
    except Exception:  # noqa: BLE001 - bookkeeping only, never fails a send
        log.warning("survey_one_off: could not record Resend message ids")
        with suppress(Exception):
            await session.rollback()


def _audit(
    *,
    actor_user_id: int | None,
    alumni_id: int,
    one_off_id: int,
    graduation_year: int,
    stage: int,
    email_source: str,
    scheduled: bool,
    failed: bool = False,
) -> AuditLog:
    """One trail row per one-off email, on the ALUM (so it is in their profile's
    history). Never the address itself."""
    return AuditLog(
        user_id=actor_user_id,
        action_type=AUDIT_ACTION,
        entity_type="alumni",
        entity_id=alumni_id,
        new_value=(
            f"one_off_id={one_off_id} grad_year={graduation_year} stage={stage} "
            f"scheduled={scheduled} email_source={email_source}"
            + (" failed=True" if failed else "")
        ),
    )


async def send_one_off(
    session: AsyncSession, alumni_id: int, *, actor_user_id: int | None
) -> SurveyOneOffResult:
    """Send the survey to one alum now. Raises instead of sending nothing quietly:

    * :class:`NotFoundError` -- no such alum;
    * :class:`ConflictError` (409) -- they can't be sent to (the reason, in plain
      words), another send holds the lock, the budget is spent, or Resend asked
      us to slow down. Nothing was sent in any of these;
    * :class:`ServiceError` -- Resend refused, or the outcome is unknown.
    """
    base_url, from_field = _from_field()
    target = await _resolve(session, alumni_id)
    if target.reason_code is not None or target.recipient is None:
        raise ConflictError(
            f"{REASON_TEXT.get(target.reason_code or '', 'They cannot be sent the survey.')}"
            " Nothing was sent."
        )
    recipient = target.recipient
    year = target.graduation_year
    assert year is not None  # _resolve refuses a missing year

    async with survey_email.send_lock() as acquired:
        if not acquired:
            raise ConflictError(BUSY_MESSAGE)
        allowance = await survey_email.remaining_send_allowance(session)
        if allowance is not None and allowance <= 0:
            raise ConflictError(BUDGET_MESSAGE)

        # CLAIM, THEN SEND. Same direction as the class send: emailing cannot be
        # undone and a row can.
        started = _now()
        one_off_id = (
            await session.execute(
                insert(SurveyOneOff)
                .values(
                    alumni_id=alumni_id,
                    graduation_year=year,
                    sent_by_user_id=actor_user_id,
                    started_at=started,
                )
                .returning(SurveyOneOff.survey_one_off_id)
            )
        ).scalar_one()
        log_id = (
            await session.execute(
                insert(SurveyOneOffSendLog)
                .values(
                    survey_one_off_id=one_off_id,
                    stage=STAGE_INITIAL,
                    sent_at=started,
                    sent_to=recipient.email[:320],
                )
                .returning(SurveyOneOffSendLog.survey_one_off_send_log_id)
            )
        ).scalar_one()
        await session.commit()

        async def _undo() -> None:
            await _release(session, [log_id])
            await session.execute(
                delete(SurveyOneOff).where(SurveyOneOff.survey_one_off_id == one_off_id)
            )
            await session.commit()

        message = await survey_message.get_for_send(session)
        email = _build_email(
            recipient,
            graduation_year=year,
            base_url=base_url,
            from_field=from_field,
            message=message,
            stage=STAGE_INITIAL,
        )
        try:
            _remaining, _reset, message_ids = survey_email._unpack_batch_result(
                await survey_email._send_batch([email])
            )
        except survey_email.ResendRateLimited as exc:
            await _undo()
            raise ConflictError(THROTTLED_MESSAGE) from exc
        except survey_email.ResendDeliveryUnknown:
            # Possibly delivered: the claim stays, so it counts and shows as sent.
            session.add(
                _audit(
                    actor_user_id=actor_user_id,
                    alumni_id=alumni_id,
                    one_off_id=one_off_id,
                    graduation_year=year,
                    stage=STAGE_INITIAL,
                    email_source=recipient.email_source,
                    scheduled=False,
                    failed=True,
                )
            )
            await session.commit()
            raise
        except ServiceError:
            await _undo()
            raise

        await _record_ids(session, [log_id], message_ids)
        session.add(
            _audit(
                actor_user_id=actor_user_id,
                alumni_id=alumni_id,
                one_off_id=one_off_id,
                graduation_year=year,
                stage=STAGE_INITIAL,
                email_source=recipient.email_source,
                scheduled=False,
            )
        )
        await session.commit()

    return SurveyOneOffResult(
        alumni_id=alumni_id,
        email=recipient.email,
        email_source=recipient.email_source,
        sent_at=started,
    )


# --------------------------------------------------------------- reminders ----


@dataclass(frozen=True)
class ReminderOutcome:
    sent: int = 0
    retry_after: int | None = None


def owed_stage(
    started_at: datetime.datetime, sent_stages: set[int], today: datetime.date
) -> int | None:
    """Which reminder (1 or 2) this one-off is owed today, or None.

    Same calendar as a class campaign: day 7 opens the first reminder and day 14
    the second, counted in whole UTC days from the send. The LOWEST unsent stage
    goes first, so a missed run catches up in order. Nothing is owed before the
    send itself is logged, or after :data:`REMINDER_GIVE_UP_DAYS`."""
    if STAGE_INITIAL not in sent_stages:
        return None
    elapsed = (today - _as_utc(started_at).date()).days
    if elapsed < _STAGE_WINDOW_DAYS or elapsed > REMINDER_GIVE_UP_DAYS:
        return None
    ceiling = min(elapsed // _STAGE_WINDOW_DAYS, STAGE_REMINDER_2)
    for stage in range(STAGE_REMINDER_1, ceiling + 1):
        if stage not in sent_stages:
            return stage
    return None


# The outer row of the "newest one-off per alum" read, so the "is there a newer
# one?" subquery can correlate to it.
_Outer = aliased(SurveyOneOff, name="one_off_outer")


async def _candidates(
    session: AsyncSession, now: datetime.datetime
) -> list[tuple[int, int, int, datetime.datetime]]:
    """``(one_off_id, alumni_id, year, started_at)`` for each alum's NEWEST
    one-off inside the reminder window, oldest first.

    Only the newest per alum: a re-send starts a fresh day 0, and the older
    one-off's reminders would only duplicate it. Never raises -- an
    un-migrated schema means no reminders, never a failed cron."""
    newer = (
        select(SurveyOneOff.survey_one_off_id)
        .where(
            SurveyOneOff.alumni_id == _Outer.alumni_id,
            SurveyOneOff.survey_one_off_id > _Outer.survey_one_off_id,
        )
        .exists()
    )
    cutoff = now - datetime.timedelta(days=REMINDER_GIVE_UP_DAYS + 1)
    stmt = (
        select(
            _Outer.survey_one_off_id,
            _Outer.alumni_id,
            _Outer.graduation_year,
            _Outer.started_at,
        )
        .where(_Outer.started_at >= cutoff, ~newer)
        .order_by(_Outer.started_at, _Outer.survey_one_off_id)
    )
    try:
        async with session.begin_nested():
            rows = (await session.execute(stmt)).all()
    except Exception:  # noqa: BLE001 - reminders must never break the class cron
        log.warning("survey_one_off: could not read one-off sends; no reminders")
        return []
    return [(int(r[0]), int(r[1]), int(r[2]), r[3]) for r in rows]


async def _sent_stages(session: AsyncSession, one_off_id: int) -> set[int]:
    rows = (
        await session.execute(
            select(SurveyOneOffSendLog.stage).where(
                SurveyOneOffSendLog.survey_one_off_id == one_off_id
            )
        )
    ).scalars().all()
    return {int(s) for s in rows}


async def _replied_since(
    session: AsyncSession, alumni_id: int, started_at: datetime.datetime
) -> bool:
    row = (
        await session.execute(
            select(SurveyResponse.survey_response_id)
            .where(
                SurveyResponse.alumni_id == alumni_id,
                SurveyResponse.submitted_at >= started_at,
                SurveyResponse.status.in_(survey_email.RESPONDED_STATUSES),
            )
            .limit(1)
        )
    ).first()
    return row is not None


async def _claim_reminder(
    session: AsyncSession, one_off_id: int, stage: int, sent_to: str
) -> int | None:
    """Reserve (one_off, stage). None when it is already taken."""
    stmt = (
        pg_insert(SurveyOneOffSendLog)
        .values(survey_one_off_id=one_off_id, stage=stage, sent_to=sent_to[:320])
        .on_conflict_do_nothing(constraint="uq_survey_one_off_send_log_stage")
        .returning(SurveyOneOffSendLog.survey_one_off_send_log_id)
    )
    return (await session.execute(stmt)).scalar()


async def send_due_reminders(
    session: AsyncSession, *, limit: int | None
) -> ReminderOutcome:
    """Send every day-7 / day-14 one-off reminder that is due, up to ``limit``.

    Called by the daily cron WITH the send lock already held, and with what is
    left of the run's budget as ``limit`` (None = cap off). Each reminder goes
    only if, today, the alum:

    * has not replied since the one-off went out;
    * would still be allowed a one-off (not deceased / Do Not Contact, still
      reachable, class campaign not paused or stopped -- a held reminder goes
      out on a later run if the campaign resumes).

    The address is resolved again, so a corrected email is used. Same render as
    a class reminder: the staff reminder note goes on top.

    Never raises for one-off bookkeeping problems; a 429 is returned as
    ``retry_after`` so the cron stops, like a class send."""
    if limit is not None and limit <= 0:
        return ReminderOutcome()
    now = _now()
    candidates = await _candidates(session, now)
    if not candidates:
        return ReminderOutcome()
    base_url, from_field = _from_field()

    due: list[tuple[int, int, int, int, survey_email.Recipient]] = []
    for one_off_id, alumni_id, year, started_at in candidates:
        stage = owed_stage(started_at, await _sent_stages(session, one_off_id), now.date())
        if stage is None:
            continue
        if await _replied_since(session, alumni_id, _as_utc(started_at)):
            continue
        try:
            target = await _resolve(session, alumni_id)
        except NotFoundError:
            continue
        if target.reason_code is not None or target.recipient is None:
            continue
        due.append((one_off_id, alumni_id, year, stage, target.recipient))
        if limit is not None and len(due) >= limit:
            break
    if not due:
        return ReminderOutcome()

    message = await survey_message.get_for_send(session)
    sent = 0
    for chunk_start in range(0, len(due), survey_email._BATCH_MAX):
        chunk = due[chunk_start : chunk_start + survey_email._BATCH_MAX]
        claimed: list[tuple[int, int, int, int, survey_email.Recipient, int]] = []
        for one_off_id, alumni_id, year, stage, r in chunk:
            log_id = await _claim_reminder(session, one_off_id, stage, r.email)
            if log_id is not None:
                claimed.append((one_off_id, alumni_id, year, stage, r, int(log_id)))
        await session.commit()
        if not claimed:
            continue
        emails = [
            _build_email(
                r,
                graduation_year=year,
                base_url=base_url,
                from_field=from_field,
                message=message,
                stage=stage,
            )
            for _o, _a, year, stage, r, _l in claimed
        ]
        log_ids = [c[5] for c in claimed]
        try:
            _remaining, _reset, message_ids = survey_email._unpack_batch_result(
                await survey_email._send_batch(emails)
            )
        except survey_email.ResendRateLimited as exc:
            await _release(session, log_ids)
            return ReminderOutcome(sent=sent, retry_after=exc.retry_after)
        except survey_email.ResendDeliveryUnknown:
            log.error(
                "One-off survey reminder outcome unknown for %s emails; claim kept",
                len(claimed),
            )
            break
        except ServiceError:
            await _release(session, log_ids)
            log.error("One-off survey reminders refused by Resend; will retry next run")
            break
        sent += len(claimed)
        await _record_ids(session, log_ids, message_ids)
        for one_off_id, alumni_id, year, stage, r, _l in claimed:
            session.add(
                _audit(
                    actor_user_id=None,
                    alumni_id=alumni_id,
                    one_off_id=one_off_id,
                    graduation_year=year,
                    stage=stage,
                    email_source=r.email_source,
                    scheduled=True,
                )
            )
        await session.commit()
        await asyncio.sleep(survey_email._INTER_BATCH_DELAY_SECONDS)
    return ReminderOutcome(sent=sent)


# ------------------------------------------------------------------ usage -----


async def sent_counts(
    session: AsyncSession,
    *,
    start_today: datetime.datetime,
    start_month: datetime.datetime,
    after: datetime.datetime | None = None,
) -> tuple[int, int]:
    """``(today, this_month)`` one-off emails, for ``survey_email.get_send_usage``.

    NEVER RAISES and never poisons the caller's transaction (SAVEPOINT), same
    contract as the digest and invite ledgers: an un-migrated schema counts zero.
    """
    stmt = select(
        func.count().label("month"),
        func.count().filter(SurveyOneOffSendLog.sent_at >= start_today).label("today"),
    ).where(SurveyOneOffSendLog.sent_at >= start_month)
    if after is not None:
        stmt = stmt.where(SurveyOneOffSendLog.sent_at > after)
    try:
        async with session.begin_nested():
            row = (await session.execute(stmt)).first()
    except Exception:  # noqa: BLE001 - the survey send must never fail on this
        log.warning("survey_one_off: could not read the one-off send log; counting zero")
        return (0, 0)
    if not row:
        return (0, 0)
    return (int(row[1] or 0), int(row[0] or 0))


__all__ = [
    "REASON_TEXT",
    "REMINDER_GIVE_UP_DAYS",
    "ReminderOutcome",
    "get_preview",
    "last_sent_at",
    "owed_stage",
    "send_due_reminders",
    "send_one_off",
    "sends_for_alum",
    "sent_counts",
]
