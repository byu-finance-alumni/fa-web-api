"""Survey send-scheduler service (#542).

Auto-sends the annual "confirm your info" survey on a cadence, driven by a daily
Vercel cron (``POST /survey/cron/run`` → :func:`run_due_schedules`).

Each ``survey_schedule`` row is one graduation year's campaign: an initial send on
``start_date``, then a 1-week and a 2-week reminder to the non-responders. The
current STAGE is derived purely from the days elapsed since ``start_date`` (0 / 1
/ 2), so the cron is idempotent — it can run every day and only sends what's due.

Double-emailing is prevented by ``survey_send_log``: after each successful Resend
batch the scheduler records a row per recipient, and later runs exclude anyone
already logged for the (year, stage). The eligible set itself comes from
:func:`survey_email._load_recipients`, which already drops non-sendable addresses
and anyone who replied within the last 365 days — so reminders only ever reach
genuine non-responders.

The send is Resend-governed via :func:`survey_email.send_recipients`: on a 429 it
stops and the remainder is picked up on the next cron run. Every scheduled send
writes the same ``send_survey`` audit row the manual send writes (carrying
``sent=N``), so the console's usage tally still counts these.
"""

from __future__ import annotations

import datetime
import logging

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ServiceError
from app.models.audit import AuditLog
from app.models.survey_schedule import SurveySchedule, SurveySendLog
from app.schemas.survey import (
    SurveyScheduleCreateRequest,
    SurveyScheduleItem,
    SurveyScheduleRunItem,
    SurveyScheduleRunSummary,
)
from app.services import survey_email

log = logging.getLogger(__name__)

# Campaign states (mirror the DB CHECK constraint).
STATUS_SCHEDULED = "scheduled"
STATUS_ACTIVE = "active"
STATUS_COMPLETED = "completed"
STATUS_CANCELLED = "cancelled"
_RUNNABLE_STATUSES = (STATUS_SCHEDULED, STATUS_ACTIVE)

# Send stages and the day-since-start windows that select them.
STAGE_INITIAL = 0
STAGE_REMINDER_1 = 1
STAGE_REMINDER_2 = 2
_STAGE_WINDOW_DAYS = 7  # each stage covers a 7-day window from start_date


def _now() -> datetime.datetime:
    return datetime.datetime.now(datetime.UTC)


def _today() -> datetime.date:
    return _now().date()


def _stage_for(elapsed_days: int) -> int | None:
    """Which stage a campaign is in ``elapsed_days`` after its start.

    0 for the first week, 1 for the second, 2 for the third; ``None`` once the
    2-week-reminder window has passed (campaign complete)."""
    if elapsed_days < 0:
        return None  # not started yet (shouldn't happen — due filter excludes it)
    stage = elapsed_days // _STAGE_WINDOW_DAYS
    return stage if stage <= STAGE_REMINDER_2 else None


# --------------------------------------------------------------- queries ------


async def _load_schedules_due(
    session: AsyncSession, today: datetime.date
) -> list[SurveySchedule]:
    """Schedules that are runnable (scheduled/active) and have started."""
    stmt = (
        select(SurveySchedule)
        .where(SurveySchedule.status.in_(_RUNNABLE_STATUSES))
        .where(SurveySchedule.start_date <= today)
        .order_by(SurveySchedule.graduation_year)
    )
    return list((await session.execute(stmt)).scalars().all())


async def _logged_alumni_ids(
    session: AsyncSession, graduation_year: int, stage: int
) -> set[int]:
    """alumni_ids already emailed for (year, stage) — the double-send guard."""
    stmt = select(SurveySendLog.alumni_id).where(
        SurveySendLog.graduation_year == graduation_year,
        SurveySendLog.stage == stage,
    )
    return set((await session.execute(stmt)).scalars().all())


async def _sent_counts_by_stage(
    session: AsyncSession,
) -> dict[tuple[int, int], int]:
    """Delivered-email counts keyed by (graduation_year, stage)."""
    stmt = select(
        SurveySendLog.graduation_year,
        SurveySendLog.stage,
        func.count(),
    ).group_by(SurveySendLog.graduation_year, SurveySendLog.stage)
    return {
        (year, stage): n for year, stage, n in (await session.execute(stmt)).all()
    }


def _to_item(
    sched: SurveySchedule, counts: dict[tuple[int, int], int]
) -> SurveyScheduleItem:
    year = sched.graduation_year
    return SurveyScheduleItem(
        survey_schedule_id=sched.survey_schedule_id,
        graduation_year=year,
        start_date=sched.start_date,
        status=sched.status,
        last_run_at=sched.last_run_at,
        created_at=getattr(sched, "created_at", None),
        sent_initial=counts.get((year, STAGE_INITIAL), 0),
        sent_reminder_1=counts.get((year, STAGE_REMINDER_1), 0),
        sent_reminder_2=counts.get((year, STAGE_REMINDER_2), 0),
    )


# ----------------------------------------------------------- management --------


async def list_schedules(session: AsyncSession) -> list[SurveyScheduleItem]:
    """Every schedule (newest cohort first) with its per-stage delivered counts."""
    schedules = list(
        (
            await session.execute(
                select(SurveySchedule).order_by(SurveySchedule.graduation_year.desc())
            )
        )
        .scalars()
        .all()
    )
    counts = await _sent_counts_by_stage(session)
    return [_to_item(s, counts) for s in schedules]


async def get_schedule(
    session: AsyncSession, graduation_year: int
) -> SurveyScheduleItem | None:
    """One schedule + counts, re-queried fresh (safe to call after a commit)."""
    sched = (
        await session.execute(
            select(SurveySchedule).where(
                SurveySchedule.graduation_year == graduation_year
            )
        )
    ).scalar_one_or_none()
    if sched is None:
        return None
    counts = await _sent_counts_by_stage(session)
    return _to_item(sched, counts)


async def _upsert_schedule(
    session: AsyncSession,
    *,
    graduation_year: int,
    start_date: datetime.date,
    actor_user_id: int | None,
) -> None:
    """Create or replace one year's schedule row — WITHOUT committing.

    Replacing resets the campaign to ``scheduled`` with the new start date; the
    delivery log is left intact so an already-sent stage is never re-sent. The
    caller owns the commit, so the single- and bulk-create paths share this
    upsert while controlling their own transaction boundary."""
    existing = (
        await session.execute(
            select(SurveySchedule).where(
                SurveySchedule.graduation_year == graduation_year
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        existing.start_date = start_date
        existing.status = STATUS_SCHEDULED
        existing.created_by_user_id = actor_user_id
    else:
        session.add(
            SurveySchedule(
                graduation_year=graduation_year,
                start_date=start_date,
                status=STATUS_SCHEDULED,
                created_by_user_id=actor_user_id,
            )
        )


async def create_schedule(
    session: AsyncSession,
    *,
    graduation_year: int,
    start_date: datetime.date,
    actor_user_id: int | None,
) -> SurveyScheduleItem:
    """Create — or replace — the schedule for a graduation year (unique per year).

    Replacing resets the campaign to ``scheduled`` with the new start date; the
    delivery log is left intact so an already-sent stage is never re-sent."""
    await _upsert_schedule(
        session,
        graduation_year=graduation_year,
        start_date=start_date,
        actor_user_id=actor_user_id,
    )
    await session.commit()
    item = await get_schedule(session, graduation_year)
    assert item is not None  # just upserted
    return item


async def create_schedules_bulk(
    session: AsyncSession,
    *,
    items: list[SurveyScheduleCreateRequest],
    actor_user_id: int | None,
) -> list[SurveyScheduleItem]:
    """Create/replace schedules for many graduation years in one transaction.

    Each item is upserted with the same logic as :func:`create_schedule`. A
    duplicate ``graduation_year`` in the payload collapses to a single row —
    last one wins — so the result never has two schedules for the same year.
    Everything is committed once and the full, refreshed schedule list is
    returned (mirroring what :func:`list_schedules` would serve). An empty list
    is a no-op that just returns the current schedules.

    Start-date validation intentionally mirrors :func:`create_schedule`, which
    does not reject past dates — so none is added here."""
    # Dedupe by graduation_year, last occurrence wins (dict preserves order).
    deduped: dict[int, datetime.date] = {
        item.graduation_year: item.start_date for item in items
    }
    for graduation_year, start_date in deduped.items():
        await _upsert_schedule(
            session,
            graduation_year=graduation_year,
            start_date=start_date,
            actor_user_id=actor_user_id,
        )
    await session.commit()
    return await list_schedules(session)


async def cancel_schedule(
    session: AsyncSession, graduation_year: int
) -> SurveyScheduleItem | None:
    """Cancel a schedule (no further sends). Returns None if there is none."""
    existing = (
        await session.execute(
            select(SurveySchedule).where(
                SurveySchedule.graduation_year == graduation_year
            )
        )
    ).scalar_one_or_none()
    if existing is None:
        return None
    existing.status = STATUS_CANCELLED
    await session.commit()
    return await get_schedule(session, graduation_year)


# --------------------------------------------------------------- cron core -----


async def run_due_schedules(
    session: AsyncSession, actor_user_id: int | None = None
) -> SurveyScheduleRunSummary:
    """Send whatever is due across all runnable schedules (the cron core).

    For each due schedule, derive the current stage from the elapsed days, compute
    the stage's recipients (initial: not-yet-sent; reminder: initial-recipients
    who haven't replied and haven't had this reminder), and send them
    Resend-governed. Each delivered recipient is logged right after its batch, so
    a re-run — or a resume after a 429 — never re-emails anyone. On a rate-limit we
    stop the whole run (Resend's limit is account-wide) and let the next cron run
    resume. Returns a per-year summary.
    """
    settings = survey_email.get_settings()
    base_url = settings.survey_app_base_url
    from_email = settings.survey_from_email
    if not base_url:
        raise ServiceError("SURVEY_APP_BASE_URL is not configured.")
    if not from_email:
        raise ServiceError("SURVEY_FROM_EMAIL is not configured.")
    from_field = f"{settings.survey_from_name} <{from_email}>"

    today = _today()
    schedules = await _load_schedules_due(session, today)
    # Snapshot the values we need up front: committing a batch mid-loop expires
    # the ORM instances, and re-reading an expired attribute in async context
    # would fail. Attribute *writes* (status/last_run_at) below are still fine.
    due = [(s, s.graduation_year, s.start_date) for s in schedules]

    ran: list[SurveyScheduleRunItem] = []
    for sched, year, start_date in due:
        stage = _stage_for((today - start_date).days)
        if stage is None:
            # Past the last reminder window — the campaign is finished.
            sched.status = STATUS_COMPLETED
            sched.last_run_at = _now()
            ran.append(
                SurveyScheduleRunItem(
                    graduation_year=year, stage=None, sent=0, remaining=0
                )
            )
            continue

        recipients = await survey_email._load_recipients(session, year)
        logged_initial = await _logged_alumni_ids(session, year, STAGE_INITIAL)
        if stage == STAGE_INITIAL:
            targets = [r for r in recipients if r.alumni_id not in logged_initial]
        else:
            # Reminders: only those who GOT the initial and haven't yet had this
            # reminder. Repliers are already gone via _load_recipients.
            logged_current = await _logged_alumni_ids(session, year, stage)
            targets = [
                r
                for r in recipients
                if r.alumni_id in logged_initial
                and r.alumni_id not in logged_current
            ]

        async def _log_batch(
            chunk: list[survey_email.Recipient], _year: int = year, _stage: int = stage
        ) -> None:
            for r in chunk:
                session.add(
                    SurveySendLog(
                        graduation_year=_year, alumni_id=r.alumni_id, stage=_stage
                    )
                )
            # Commit per batch so a crash/throttle mid-run never re-emails the
            # recipients this batch already delivered to.
            await session.commit()

        sent, retry_after = await survey_email.send_recipients(
            targets,
            graduation_year=year,
            base_url=base_url,
            from_field=from_field,
            on_batch_sent=_log_batch,
        )

        sched.status = STATUS_ACTIVE
        sched.last_run_at = _now()
        # Same audit row the manual send writes, so usage tallies count these.
        session.add(
            AuditLog(
                user_id=actor_user_id,
                action_type="send_survey",
                entity_type="survey_campaign",
                entity_id=year,
                new_value=(
                    f"grad_year={year} stage={stage} scheduled=True "
                    f"recipients={len(targets)} sent={sent}"
                    + (
                        f" throttled_retry_after={retry_after}"
                        if retry_after
                        else ""
                    )
                ),
            )
        )
        ran.append(
            SurveyScheduleRunItem(
                graduation_year=year,
                stage=stage,
                sent=sent,
                remaining=len(targets) - sent,
                retry_after_seconds=retry_after,
            )
        )
        if retry_after is not None:
            # Resend's limit is account-wide — stop; the next cron run resumes.
            break

    await session.commit()
    return SurveyScheduleRunSummary(ran=ran)
