"""Survey send-scheduler service (#542).

Auto-sends the annual "confirm your info" survey on a cadence, driven by a daily
Vercel cron (``POST /survey/cron/run`` → :func:`run_due_schedules`).

Each ``survey_schedule`` row is one graduation year's campaign: an initial send on
``start_date``, then a 1-week and a 2-week reminder to the non-responders. The
days elapsed since ``start_date`` set a CEILING on which stage may go out (0 / 1
/ 2); WHICH stage actually goes out, and whether the campaign is finished at all,
comes from the delivery log. So the cron is idempotent — it can run every day and
only sends what is genuinely still owed.

Double-emailing is prevented by ``survey_send_log``: the sender CLAIMS each batch
there (and commits) before it calls Resend, and later runs exclude anyone already
logged for the (year, stage). Those same rows are what the profile's Surveys tab
reports as "sent, no reply yet" (``profile._derive_survey_history``) — so the
send log is a read source, not just a guard, and nothing here should ALSO write
the legacy ``surveys`` table (see ``models.crm.Survey``). It is also the usage
ledger the daily/monthly budget is measured against
(``survey_email.get_send_usage``).

Sending itself belongs entirely to :func:`survey_email.send_survey_stage`, shared
with the console's manual send — this module decides WHICH years and stages are
due, never how to send or how to record it. That split is deliberate: the manual
send used to call the raw sender directly without recording anything, which is
what produced the unscheduled second send of 2026-08-02.

A campaign can be stopped two ways. ``cancel`` is terminal — it never resumes.
``pause`` is reversible: it drops the row out of the runnable set so the cron
skips it, and :func:`resume_schedule` shifts ``start_date`` forward by the paused
duration so the stage arithmetic picks up exactly where it stopped instead of
silently ageing past the reminder windows. See the "pause / resume" section.

The send is Resend-governed: on a 429 it stops, the claim for the throttled batch
is released, and the remainder is picked up on the next cron run. Every scheduled
send writes the same ``send_survey`` audit row the manual send writes — as the
TRAIL, not the ledger; usage is counted from ``survey_send_log``.
"""

from __future__ import annotations

import datetime
import logging
from collections.abc import Sequence

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ConflictError, ServiceError
from app.models.audit import AuditLog
from app.models.survey_schedule import SurveySchedule, SurveySendLog
from app.models.survey_send_config import SurveySendConfig
from app.models.user import User
from app.schemas.survey import (
    SurveyNewCyclePreview,
    SurveyScheduleCancelAllResult,
    SurveyScheduleCreateRequest,
    SurveyScheduleItem,
    SurveySchedulePauseAllResult,
    SurveyScheduleRunItem,
    SurveyScheduleRunSummary,
    SurveySendConfigItem,
)
from app.services import survey_email

log = logging.getLogger(__name__)

# Campaign states (mirror the DB CHECK constraint).
STATUS_SCHEDULED = "scheduled"
STATUS_ACTIVE = "active"
STATUS_PAUSED = "paused"
STATUS_COMPLETED = "completed"
STATUS_CANCELLED = "cancelled"
# The ONLY statuses the cron acts on (`_load_schedules_due`). `paused` is
# deliberately absent — that single omission is what actually stops the sending,
# and it is why pause needs no change to the cron itself.
_RUNNABLE_STATUSES = (STATUS_SCHEDULED, STATUS_ACTIVE)

# Send stages and the day-since-start windows that select them. Defined in
# `survey_email` (they describe `survey_send_log`, which BOTH senders write) and
# re-exported here so this module's callers are unchanged.
STAGE_INITIAL = survey_email.STAGE_INITIAL
STAGE_REMINDER_1 = survey_email.STAGE_REMINDER_1
STAGE_REMINDER_2 = survey_email.STAGE_REMINDER_2
_STAGE_WINDOW_DAYS = survey_email._STAGE_WINDOW_DAYS
_stage_for = survey_email.stage_for


def _now() -> datetime.datetime:
    return datetime.datetime.now(datetime.UTC)


def _today() -> datetime.date:
    return _now().date()


# --------------------------------------------------------------- queries ------


async def _load_schedules_due(
    session: AsyncSession, today: datetime.date
) -> list[SurveySchedule]:
    """Schedules that are runnable (scheduled/active) and have started.

    Ordered earliest-scheduled first (then oldest cohort) so that, under the
    shared daily send budget, the campaign that started first drains before a
    later one gets any of the day's allowance."""
    stmt = (
        select(SurveySchedule)
        .where(SurveySchedule.status.in_(_RUNNABLE_STATUSES))
        .where(SurveySchedule.start_date <= today)
        .order_by(SurveySchedule.start_date, SurveySchedule.graduation_year)
    )
    return list((await session.execute(stmt)).scalars().all())


async def _sent_counts_by_stage(
    session: AsyncSession,
) -> dict[tuple[int, int], int]:
    """Delivered-email counts keyed by (graduation_year, stage), for the year's
    CURRENT cycle only (#357).

    Joined to ``survey_schedule`` on both the year and its ``cycle_seq`` so the
    console reports what THIS campaign has sent. Counting the log unscoped is
    what made a re-scheduled year display last year's totals as though they were
    this cycle's — the campaign looked like it had run when it had emailed
    nobody.

    A year with log rows but no schedule row (a manual send for an unscheduled
    year) has no current cycle to report against, so it is absent here and its
    counters read 0 — the same as before this change, since the console only
    renders rows that HAVE a schedule."""
    stmt = (
        select(
            SurveySendLog.graduation_year,
            SurveySendLog.stage,
            func.count(),
        )
        .join(
            SurveySchedule,
            (SurveySchedule.graduation_year == SurveySendLog.graduation_year)
            & (SurveySchedule.cycle_seq == SurveySendLog.cycle_seq),
        )
        .group_by(SurveySendLog.graduation_year, SurveySendLog.stage)
    )
    return {
        (year, stage): n for year, stage, n in (await session.execute(stmt)).all()
    }


async def _creator_names(
    session: AsyncSession, schedules: Sequence[SurveySchedule]
) -> dict[int, str]:
    """Display names of the users who started these campaigns, keyed by user_id.

    Resolved for the WHOLE list in one query — the console lists every graduation
    year, so a per-row lookup would be an N+1. Name formatting mirrors
    ``profile._full_name`` (first + last, falling back to the email); it is
    duplicated rather than imported so this service keeps no dependency on the
    profile service. Only the resolved name is ever surfaced — the raw
    ``created_by_user_id`` stays internal (FERPA, see ``SurveyScheduleItem``)."""
    ids = {
        s.created_by_user_id
        for s in schedules
        if getattr(s, "created_by_user_id", None) is not None
    }
    if not ids:
        return {}
    rows = (
        await session.execute(
            select(
                User.user_id, User.first_name, User.last_name, User.email
            ).where(User.user_id.in_(ids))
        )
    ).all()
    names: dict[int, str] = {}
    for user_id, first, last, email in rows:
        full = " ".join(p for p in (first, last) if p).strip()
        names[user_id] = full or email
    return names


def _to_item(
    sched: SurveySchedule,
    counts: dict[tuple[int, int], int],
    creators: dict[int, str],
) -> SurveyScheduleItem:
    year = sched.graduation_year
    created_by_id = getattr(sched, "created_by_user_id", None)
    return SurveyScheduleItem(
        survey_schedule_id=sched.survey_schedule_id,
        graduation_year=year,
        start_date=sched.start_date,
        status=sched.status,
        cycle_seq=getattr(sched, "cycle_seq", 1),
        last_run_at=sched.last_run_at,
        created_at=getattr(sched, "created_at", None),
        created_by=creators.get(created_by_id) if created_by_id else None,
        paused_at=getattr(sched, "paused_at", None),
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
    creators = await _creator_names(session, schedules)
    return [_to_item(s, counts, creators) for s in schedules]


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
    creators = await _creator_names(session, [sched])
    return _to_item(sched, counts, creators)


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
    upsert while controlling their own transaction boundary.

    ``cycle_seq`` is deliberately NOT advanced here (#357). This path is the
    "I typed the wrong start date" correction, and it must stay non-destructive:
    advancing the cycle would make every already-emailed alum a target again, so
    fixing a typo would blast the cohort a second time. Starting the next annual
    campaign is the separate, explicit :func:`start_new_cycle`."""
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


async def preview_new_cycle(
    session: AsyncSession, graduation_year: int
) -> SurveyNewCyclePreview | None:
    """What starting a new cycle for this year WOULD do — for the confirmation.

    Returns ``None`` when the year has no schedule (nothing to start a new cycle
    of). Nothing is mutated and nothing is sent.

    The counts exist because "Start new survey cycle" is irreversible: staff see
    how many alumni would be emailed, and how many of those already received the
    previous cycle, BEFORE committing. ``previously_emailed`` is the number this
    cycle would reach who are in the CURRENT cycle's send log — i.e. the people
    who would get a second email — which is the number that makes the blast size
    real rather than abstract."""
    sched = (
        await session.execute(
            select(SurveySchedule).where(
                SurveySchedule.graduation_year == graduation_year
            )
        )
    ).scalar_one_or_none()
    if sched is None:
        return None
    # Same eligibility the send itself uses, so the preview cannot promise a
    # different population than the campaign would actually reach.
    recipients = await survey_email._load_recipients(session, graduation_year)
    eligible, _duplicates = survey_email.dedupe_by_email(recipients)
    eligible_ids = {r.alumni_id for r in eligible}
    already = set(
        (
            await session.execute(
                select(SurveySendLog.alumni_id).where(
                    SurveySendLog.graduation_year == graduation_year,
                    SurveySendLog.cycle_seq == sched.cycle_seq,
                )
            )
        )
        .scalars()
        .all()
    )
    return SurveyNewCyclePreview(
        graduation_year=graduation_year,
        current_cycle=sched.cycle_seq,
        next_cycle=sched.cycle_seq + 1,
        current_status=sched.status,
        would_email=len(eligible_ids),
        previously_emailed=len(eligible_ids & already),
    )


async def start_new_cycle(
    session: AsyncSession,
    *,
    graduation_year: int,
    start_date: datetime.date,
    actor_user_id: int | None,
) -> SurveyScheduleItem | None:
    """Begin the NEXT campaign for a graduation year (#357) — the annual re-run.

    Advances ``cycle_seq``, which is what makes the whole cohort eligible again:
    the send log is scoped by cycle, so a new cycle starts with an empty
    "already emailed" set while every previous cycle's rows stay intact as
    history. Nothing is deleted.

    Deliberately SEPARATE from :func:`create_schedule` (Jake, 2026-08-03). The
    two intents — "fix the start date I mistyped" and "run this year's survey
    again" — are indistinguishable at the data layer but have opposite
    consequences, one of them an irreversible second email to the whole cohort.
    Splitting them means the destructive one is never reachable by accident from
    the corrective one.

    Returns ``None`` when the year has no schedule to advance; the caller turns
    that into a 404. Callers MUST confirm with the user first — see
    :func:`preview_new_cycle` for the counts that confirmation shows."""
    sched = (
        await session.execute(
            select(SurveySchedule).where(
                SurveySchedule.graduation_year == graduation_year
            )
        )
    ).scalar_one_or_none()
    if sched is None:
        return None

    previous_cycle = sched.cycle_seq
    sched.cycle_seq = previous_cycle + 1
    sched.start_date = start_date
    sched.status = STATUS_SCHEDULED
    sched.created_by_user_id = actor_user_id
    # A new cycle is not a resumption of the old one: clear the pause state so a
    # campaign paused in cycle N does not resume-shift cycle N+1's start_date.
    sched.paused_at = None
    sched.paused_from_status = None
    sched.last_run_at = None
    session.add(
        AuditLog(
            user_id=actor_user_id,
            action_type="survey_cycle_started",
            entity_type="survey_campaign",
            entity_id=graduation_year,
            new_value=(
                f"grad_year={graduation_year} "
                f"cycle={previous_cycle}->{sched.cycle_seq} "
                f"start_date={start_date.isoformat()}"
            ),
        )
    )
    await session.commit()
    return await get_schedule(session, graduation_year)


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


# ------------------------------------------------------------ pause / resume ---
#
# PAUSE is the REVERSIBLE stop; `cancel` is the terminal one. Pausing only flips
# the status out of `_RUNNABLE_STATUSES`, which is by itself enough to stop the
# daily cron — `_load_schedules_due` never selects a `paused` row.
#
# Resume is the hard part. The stage a campaign sends is derived purely from
# `today - start_date` (`_stage_for`), so a naive pause silently EATS the rest of
# the campaign: pause on day 3, resume three weeks later, and `elapsed` is 24 —
# past the 2-week window — so the very next cron run marks the campaign
# `completed` and the two reminders never go out. Nothing would look broken; the
# cohort would just never be reminded.
#
# So resume restores the campaign to where it was in its CADENCE, not where the
# calendar has drifted to, by pushing `start_date` forward by the paused
# duration. `paused_at` is therefore load-bearing state, not an audit stamp.
#
# What resume does NOT need to reconstruct: partially-sent stages. Who has been
# emailed lives in `survey_send_log` (see `_logged_alumni_ids`), never in dates,
# and pause/resume never touch it. A half-finished initial therefore resumes on
# its own — `run_due_schedules` sends STAGE_INITIAL to anyone missing a stage-0
# log row before it will look at any reminder, whatever `elapsed` says.


def _resumed_start_date(
    *,
    start_date: datetime.date,
    paused_on: datetime.date | None,
    resumed_on: datetime.date,
) -> datetime.date:
    """The ``start_date`` a resuming campaign should carry.

    Two cases, because "shift by the paused duration" is only right for a
    campaign that was actually IN FLIGHT:

    * **In flight** (``paused_on > start_date``): shift forward by exactly the
      paused duration, so ``today - start_date`` — and therefore the stage — is
      the same number it was the day it was paused. The cadence continues rather
      than restarting: a campaign paused on day 3 resumes on day 3.

    * **Never started** (``paused_on <= start_date``): nothing had elapsed, so
      there is no cadence to preserve and shifting would be wrong — it would push
      a future start further into the future for no reason. It keeps its original
      start date... unless that date PASSED during the pause, in which case it
      starts today. That last clamp matters: honouring a start date 10 days gone
      would land the campaign at `elapsed=10`, and because the initial-first rule
      sends stage 0 anyway, it would fire the initial today and the 1-week
      reminder TOMORROW. Starting from today gives it the full, correct cadence,
      which is what "it never started" should mean.

    The two branches agree on the boundary (``paused_on == start_date`` yields
    ``resumed_on`` either way), so which side of the comparison it falls on is
    not load-bearing.

    ``paused_on`` is None only for a `paused` row that predates / bypassed the
    pause service (a hand-edited DB row): there is no duration to shift by, so
    the start date is left exactly as found rather than invented.
    """
    if paused_on is None:
        return start_date
    if paused_on <= start_date:
        return max(start_date, resumed_on)
    return start_date + (resumed_on - paused_on)


async def _load_schedule_row(
    session: AsyncSession, graduation_year: int
) -> SurveySchedule | None:
    stmt = select(SurveySchedule).where(
        SurveySchedule.graduation_year == graduation_year
    )
    return (await session.execute(stmt)).scalar_one_or_none()


async def pause_schedule(
    session: AsyncSession, graduation_year: int, *, actor_user_id: int | None
) -> SurveyScheduleItem | None:
    """Pause one year's campaign — a stop it can come back from.

    Returns None if there is no schedule for the year (the route 404s). Pausing
    an already-paused campaign is a no-op success: the caller asked for a state
    it is already in, and two admins hitting the button together must not fight
    over `paused_at`. Anything else (`completed`/`cancelled`) is a 409 rather
    than a silent no-op — it means the caller believes a campaign is running that
    isn't, and quietly succeeding would confirm that misconception."""
    sched = await _load_schedule_row(session, graduation_year)
    if sched is None:
        return None
    if sched.status == STATUS_PAUSED:
        return await get_schedule(session, graduation_year)
    if sched.status not in _RUNNABLE_STATUSES:
        raise ConflictError(
            f"Only a scheduled or active campaign can be paused — the "
            f"{graduation_year} campaign is {sched.status}."
        )
    # Remember what to come back to. Deriving it on resume is not reliable:
    # `last_run_at` (and the send log) survive a re-schedule — `_upsert_schedule`
    # resets status to `scheduled` but leaves both — so a re-scheduled year would
    # resume as `active` having never started.
    sched.paused_from_status = sched.status
    sched.paused_at = _now()
    sched.status = STATUS_PAUSED
    session.add(
        AuditLog(
            user_id=actor_user_id,
            action_type="pause_survey_schedule",
            entity_type="survey_campaign",
            entity_id=graduation_year,
            old_value=sched.paused_from_status,
            new_value=f"paused grad_year={graduation_year}",
        )
    )
    await session.commit()
    return await get_schedule(session, graduation_year)


async def resume_schedule(
    session: AsyncSession, graduation_year: int, *, actor_user_id: int | None
) -> SurveyScheduleItem | None:
    """Resume a paused campaign where its cadence left off (see the notes above).

    Returns None if there is no schedule for the year. Resuming a campaign that
    is already running is a no-op success (it is already in the requested state);
    resuming a `completed` or `cancelled` one is a 409 — cancel is terminal by
    design and resume must not become a back door around that."""
    sched = await _load_schedule_row(session, graduation_year)
    if sched is None:
        return None
    if sched.status in _RUNNABLE_STATUSES:
        return await get_schedule(session, graduation_year)
    if sched.status != STATUS_PAUSED:
        raise ConflictError(
            f"Only a paused campaign can be resumed — the {graduation_year} "
            f"campaign is {sched.status}. Re-schedule it instead."
        )
    resumed_on = _today()
    paused_at = sched.paused_at
    # `paused_at` is stored as timestamptz (UTC); `_today()` is the UTC date the
    # scheduler itself compares `start_date` against, so both sides of the shift
    # are measured on the same clock.
    paused_on = (
        paused_at.astimezone(datetime.UTC).date() if paused_at is not None else None
    )
    previous_start = sched.start_date
    sched.start_date = _resumed_start_date(
        start_date=previous_start, paused_on=paused_on, resumed_on=resumed_on
    )
    # Restore the status it held when paused; fall back to `scheduled` only for a
    # row that was never paused through this service (nothing recorded).
    sched.status = sched.paused_from_status or STATUS_SCHEDULED
    sched.paused_at = None
    sched.paused_from_status = None
    session.add(
        AuditLog(
            user_id=actor_user_id,
            action_type="resume_survey_schedule",
            entity_type="survey_campaign",
            entity_id=graduation_year,
            old_value=f"paused start_date={previous_start.isoformat()}",
            new_value=(
                f"resumed grad_year={graduation_year} status={sched.status} "
                f"start_date={sched.start_date.isoformat()}"
            ),
        )
    )
    await session.commit()
    return await get_schedule(session, graduation_year)


async def pause_all_schedules(
    session: AsyncSession, *, actor_user_id: int | None
) -> SurveySchedulePauseAllResult:
    """Pause EVERY runnable (scheduled/active) campaign at once — the reversible
    twin of :func:`cancel_all_schedules`.

    Same shape for the same reasons: one ``UPDATE ... RETURNING`` rather than a
    read-then-write loop, so the stop is atomic and the daily cron cannot pick up
    a year this call has already decided to stop; only ``_RUNNABLE_STATUSES``
    rows are touched, so paused/completed/cancelled campaigns keep their state;
    and it always writes ONE audit row, even for a no-op, because reaching for a
    blanket stop is itself an intervention worth tracing.

    Idempotent: a second press finds nothing runnable and reports 0, leaving the
    first press's ``paused_at`` stamps untouched — so "resume" still shifts by
    the real paused duration.

    ``paused_from_status`` is set from the row's CURRENT ``status`` in the same
    statement. Postgres evaluates every SET expression against the pre-UPDATE
    row, so it captures `scheduled`/`active` and not the `paused` being written.
    """
    result = await session.execute(
        update(SurveySchedule)
        .where(SurveySchedule.status.in_(_RUNNABLE_STATUSES))
        .values(
            paused_from_status=SurveySchedule.status,
            status=STATUS_PAUSED,
            paused_at=_now(),
        )
        .returning(SurveySchedule.graduation_year)
        .execution_options(synchronize_session=False)
    )
    years = sorted(result.scalars().all())
    session.add(
        AuditLog(
            user_id=actor_user_id,
            action_type="pause_all_survey_schedules",
            entity_type="survey_campaign",
            # No single entity — the years paused are named in new_value.
            entity_id=None,
            new_value=(
                f"paused={len(years)} "
                f"grad_years={','.join(str(y) for y in years) or 'none'}"
            ),
        )
    )
    await session.commit()
    return SurveySchedulePauseAllResult(paused=len(years), graduation_years=years)


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


async def cancel_all_schedules(
    session: AsyncSession, *, actor_user_id: int | None
) -> SurveyScheduleCancelAllResult:
    """Kill switch: cancel EVERY runnable (scheduled/active) campaign at once.

    A single ``UPDATE ... RETURNING`` rather than a read-then-write loop, so the
    stop is atomic — the daily cron can't pick up a year that this call has
    already decided to stop. Only ``scheduled``/``active`` rows are touched
    (``_RUNNABLE_STATUSES`` — the exact set ``_load_schedules_due`` picks up), so
    completed and already-cancelled campaigns keep their history. Idempotent:
    with nothing running it cancels nothing and reports 0.

    Always writes ONE audit row, even for a no-op — reaching for the kill switch
    is itself an intervention worth tracing, and the row records which years were
    stopped. For an engineer actor that row is rerouted into
    ``engineer_action_log`` by the audit hook (#199), which is where this action's
    trail actually lands since the endpoint is engineer-only.
    """
    result = await session.execute(
        update(SurveySchedule)
        .where(SurveySchedule.status.in_(_RUNNABLE_STATUSES))
        .values(status=STATUS_CANCELLED)
        .returning(SurveySchedule.graduation_year)
        .execution_options(synchronize_session=False)
    )
    years = sorted(result.scalars().all())
    session.add(
        AuditLog(
            user_id=actor_user_id,
            action_type="cancel_all_survey_schedules",
            entity_type="survey_campaign",
            # No single entity — the years cancelled are named in new_value.
            entity_id=None,
            new_value=(
                f"cancelled={len(years)} "
                f"grad_years={','.join(str(y) for y in years) or 'none'}"
            ),
        )
    )
    await session.commit()
    return SurveyScheduleCancelAllResult(
        cancelled=len(years), graduation_years=years
    )


# --------------------------------------------------------------- send cap ------

# Fallback if the seeded config row is ever missing (Resend Free tier).
_DEFAULT_DAILY_LIMIT = 100
_DEFAULT_MONTHLY_LIMIT = 3000


async def get_send_config(session: AsyncSession) -> SurveySendConfigItem:
    """The single-row send cap (id=1). Falls back to enabled Free-tier defaults
    if the row is somehow missing (the migration seeds it)."""
    row = (
        await session.execute(
            select(SurveySendConfig).where(SurveySendConfig.id == 1)
        )
    ).scalar_one_or_none()
    if row is None:
        return SurveySendConfigItem(
            enabled=True,
            daily_limit=_DEFAULT_DAILY_LIMIT,
            monthly_limit=_DEFAULT_MONTHLY_LIMIT,
        )
    return SurveySendConfigItem(
        enabled=row.enabled,
        daily_limit=row.daily_limit,
        monthly_limit=row.monthly_limit,
    )


async def update_send_config(
    session: AsyncSession,
    *,
    enabled: bool,
    daily_limit: int,
    monthly_limit: int,
    actor_user_id: int | None,
) -> SurveySendConfigItem:
    """Set the send cap (in-console admin control). Upserts the single row."""
    row = (
        await session.execute(
            select(SurveySendConfig).where(SurveySendConfig.id == 1)
        )
    ).scalar_one_or_none()
    if row is None:
        row = SurveySendConfig(id=1)
        session.add(row)
    row.enabled = enabled
    row.daily_limit = daily_limit
    row.monthly_limit = monthly_limit
    row.updated_by_user_id = actor_user_id
    await session.commit()
    return await get_send_config(session)


async def _run_allowance(session: AsyncSession) -> int | None:
    """How many emails this cron run may send under the configured cap, or
    ``None`` for unlimited (cap disabled).

    The cap is account-wide: the daily and monthly budgets minus what Resend has
    already sent today / this month (the same usage tally the console shows). The
    run may send up to whichever budget is tighter."""
    config = await get_send_config(session)
    if not config.enabled:
        return None
    usage = await survey_email.get_send_usage(session)
    daily_remaining = max(0, config.daily_limit - usage.sent_today)
    monthly_remaining = max(0, config.monthly_limit - usage.sent_this_month)
    return min(daily_remaining, monthly_remaining)


# --------------------------------------------------------------- cron core -----


async def run_due_schedules(
    session: AsyncSession, actor_user_id: int | None = None
) -> SurveyScheduleRunSummary:
    """Send whatever is due across all runnable schedules (the cron core).

    Schedules are processed earliest-scheduled first. For each, the elapsed days
    give a CEILING on which stage may go out (:func:`survey_email.ceiling_stage_for`)
    and the send log decides the rest: the run sends the LOWEST stage at or below
    that ceiling which still has recipients with no log row. So the initial always
    finishes before any reminder, and — new — a reminder that could not drain
    inside its own week is finished later instead of being abandoned.

    A campaign is COMPLETED only when every stage has been offered (the ceiling
    has reached the 2-week reminder) AND none of them has a single unsent
    recipient left. Completion used to be decided from the calendar alone, before
    the recipients were even loaded: schedule twenty years at once against the
    default 100/day budget and the later cohorts flipped to `completed` on day 21
    having sent ZERO emails — terminally, with a summary that read like a clean
    finish. Elapsed days now cap what may be sent; they never declare it done.

    Sends are paced by a shared, account-wide budget (:func:`_run_allowance`): at
    most ``daily_limit``/day and ``monthly_limit``/month across ALL years, so a
    cohort trickles out over several days. When the cap is disabled the budget is
    unlimited and everything eligible is attempted (Resend's own 429 is then the
    only brake). Delivery is claimed and committed per batch inside
    :func:`survey_email.send_survey_stage`, so a re-run — or a resume after a 429
    or a spent budget — never re-emails anyone. On a 429, or once the budget is
    spent, we stop the whole run and the next daily cron resumes. Returns a
    per-year summary.
    """
    # Fail fast on misconfiguration rather than reporting an empty, clean run.
    # `send_survey_stage` checks these too; this only moves the error earlier.
    settings = survey_email.get_settings()
    if not settings.survey_app_base_url:
        raise ServiceError("SURVEY_APP_BASE_URL is not configured.")
    if not settings.survey_from_email:
        raise ServiceError("SURVEY_FROM_EMAIL is not configured.")

    today = _today()
    schedules = await _load_schedules_due(session, today)
    # Snapshot the values we need up front: committing a batch mid-loop expires
    # the ORM instances, and re-reading an expired attribute in async context
    # would fail. Attribute *writes* (status/last_run_at) below are still fine.
    due = [(s, s.graduation_year, s.start_date) for s in schedules]

    # Shared daily/monthly send budget for this whole run (None = cap disabled).
    allowance = await _run_allowance(session)

    ran: list[SurveyScheduleRunItem] = []
    for sched, year, start_date in due:
        elapsed = (today - start_date).days
        max_stage = survey_email.ceiling_stage_for(elapsed)

        recipients = await survey_email._load_recipients(session, year)
        eligible, _dupes = survey_email.dedupe_by_email(recipients)
        # Asked here purely to decide COMPLETION — which has to happen before the
        # budget check, so a year the budget starves is never mistaken for a
        # finished one. `send_survey_stage` asks again and owns the answer it
        # sends on; nothing changes in between, and it is a pair of indexed
        # lookups on `survey_send_log`. `eligible` is passed through so the
        # cohort query itself is not repeated.
        # Scoped to the campaign's CURRENT cycle (#357). Read from the schedule
        # row we already hold rather than re-querying, so this completion check
        # and the send that follows it agree on which cycle they are in.
        cycle_seq = getattr(sched, "cycle_seq", survey_email.FIRST_CYCLE)
        _stage, targets = await survey_email.select_stage_targets(
            session,
            graduation_year=year,
            recipients=eligible,
            max_stage=max_stage,
            cycle_seq=cycle_seq,
        )

        if not targets:
            # Nothing is owed at any stage the calendar permits.
            sched.last_run_at = _now()
            if max_stage >= STAGE_REMINDER_2:
                # Every stage has been offered AND drained — genuinely finished.
                sched.status = STATUS_COMPLETED
                ran.append(
                    SurveyScheduleRunItem(
                        graduation_year=year, stage=None, sent=0, remaining=0
                    )
                )
            else:
                # Still inside the cadence — the later stages are simply not due
                # yet. Emphatically NOT complete.
                sched.status = STATUS_ACTIVE
                ran.append(
                    SurveyScheduleRunItem(
                        graduation_year=year, stage=None, sent=0, remaining=0
                    )
                )
            continue

        # Apply the shared budget: send at most `allowance` of this year's
        # targets this run, then stop — the rest resumes on the next cron. This
        # comes AFTER the completion decision on purpose: a year starved of
        # budget still owes emails and must never be completed.
        if allowance is not None and allowance <= 0:
            break

        outcome = await survey_email.send_survey_stage(
            session,
            graduation_year=year,
            max_stage=max_stage,
            actor_user_id=actor_user_id,
            limit=allowance,
            recipients=eligible,
            scheduled=True,
            # The SAME cycle the completion check above used (#357) — passed,
            # not re-read, so the two cannot disagree mid-run.
            cycle_seq=cycle_seq,
        )
        sent = outcome.sent
        retry_after = outcome.retry_after
        if allowance is not None:
            allowance -= sent

        sched.status = STATUS_ACTIVE
        sched.last_run_at = _now()
        ran.append(
            SurveyScheduleRunItem(
                graduation_year=year,
                stage=outcome.stage,
                sent=sent,
                remaining=len(outcome.targets) - sent,
                retry_after_seconds=retry_after,
            )
        )
        if retry_after is not None:
            # Resend's limit is account-wide — stop; the next cron run resumes.
            break
        if allowance is not None and allowance <= 0:
            # Budget spent for this run — the rest resumes on the next cron.
            break

    await session.commit()
    return SurveyScheduleRunSummary(ran=ran)
