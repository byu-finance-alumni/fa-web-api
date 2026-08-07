"""Survey send-scheduler service (#542).

Auto-sends the annual "confirm your info" survey on a cadence, driven by a daily
Vercel cron (``POST /survey/cron/run`` → :func:`run_due_schedules`).

Each ``survey_schedule`` row is one graduation year's campaign: an initial send on
``start_date``, then a 1-week and a 2-week reminder to the non-responders. That
cadence is settled — 7 and 14 days, confirmed by Jake on 2026-08-03, with #151's
older "2 weeks then 3" text amended to match the code rather than the other way
round (#359). The days elapsed since ``start_date`` set a CEILING on which stage
may go out (0 / 1 / 2); WHICH stage actually goes out, and whether the campaign
is finished at all, comes from the delivery log. So the cron is idempotent — it
can run every day and only sends what is genuinely still owed.

After the last reminder the campaign is ``completed``, which says the SENDING is
done and nothing about whether it worked. #151's third step — flag the people who
never replied for manual follow-up — is
:func:`_cycle_non_responders`: they surface as ``non_responders`` on every
schedule item and on the completing cron run, with the names behind
:func:`list_non_responders`. That count is cycle-scoped for the same reason every
other read of the send log is (#357).

Only one send runs at a time. The cron takes ``survey_email.send_lock`` (a
Postgres advisory lock) for the whole run and a second, overlapping run returns
``skipped_locked=True`` rather than sending anything (#358).

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

The account-wide send cap (:func:`_run_allowance`) lives here because the config
row and the console's cap screen do, but it is ENFORCED down in
:func:`survey_email.send_survey_stage` (#417) — reading it only in the cron body,
as this module used to, left the manual send able to email a whole cohort past a
limit the console was showing beside the button.

A campaign is normally created from the console, but a MANUAL send to a year that
has none creates one too (:func:`create_campaign_for_send`, #405) — otherwise the
send delivers the initial email and the cron, which only iterates schedule rows,
never fires either reminder.

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

from sqlalchemy import case, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import email_reach
from app.core.errors import ConflictError, ServiceError
from app.models.alumni import Alumni
from app.models.audit import AuditLog
from app.models.contact import AlumniContactInfo
from app.models.survey_response import SurveyResponse
from app.models.survey_retirement import SurveyCampaignRetirement
from app.models.survey_schedule import SurveySchedule, SurveySendLog
from app.models.survey_send_config import SurveySendConfig
from app.models.user import User
from app.schemas.survey import (
    SurveyNewCyclePreview,
    SurveyNonResponder,
    SurveyScheduleCancelAllResult,
    SurveyScheduleCreateRequest,
    SurveyScheduleDeleteResult,
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
# `completed` means every stage was sent to everyone owed it — NOT that everyone
# answered. Who never answered is `non_responders` (see the follow-up section).
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
    renders rows that HAVE a schedule.

    DELIBERATELY not filtered by `send_not_superseded` (#395). This is a count of
    emails that left the building, not of people currently blocked: an alum who
    was reset and re-emailed really did receive two, and hiding the first would
    under-report what the campaign sent. The same reasoning keeps
    `survey_email.get_send_usage` — the Resend budget meter — unfiltered."""
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


# ------------------------------------------------- needs manual follow-up -----
#
# The third step of #151, which had no implementation at all until #359: after
# the last reminder, whoever still has not replied must be FLAGGED FOR MANUAL
# CHECK. Without it `completed` is indistinguishable from "everyone replied" —
# the campaign ends looking like a success whether it converted the whole cohort
# or none of it, and nobody can act on the difference.
#
# A non-responder is an alum who
#
#   1. received EVERY stage of their year's CURRENT campaign (all three log
#      rows), and
#   2. has not replied.
#
# (1) is why this is cycle-scoped. `survey_send_log` is append-only and spans
# every campaign a year has ever run, so counting it unscoped would fold last
# year's non-responders into this year's number and — worse — report a
# brand-new cycle that has emailed nobody yet as already having a backlog of
# people who "never responded to it". That is exactly the all-time-vs-this-
# campaign bug #357 existed to fix, and the join below is the same one
# `_sent_counts_by_stage` uses.
#
# (2) reuses the module's ONE definition of a reply — `RESPONDED_STATUSES`
# within the 365-day annual window — the same predicate that excludes repliers
# from a send (`eligible_alumni_query`) and drives the console's "N replied"
# tally. `rejected` is deliberately NOT a reply: staff threw that submission
# away, nothing reached the record, so the alum still needs following up. If
# those two definitions ever drift, an alum can be simultaneously "replied" and
# "never responded".

# All three stages must be present before someone counts as a non-responder —
# an alum still owed a reminder has not finished the campaign and is not yet a
# manual-follow-up case.
_ALL_STAGES = (STAGE_INITIAL, STAGE_REMINDER_1, STAGE_REMINDER_2)


def _cycle_non_responders():
    """``(graduation_year, alumni_id, last_sent_at)`` for everyone who completed
    their year's CURRENT campaign without replying — the manual-follow-up set.

    Everything is decided in SQL (a correlated NOT EXISTS + a HAVING over the
    send log), so it stays one query whatever the cohort size."""
    replied = (
        select(SurveyResponse.survey_response_id)
        .where(
            SurveyResponse.alumni_id == SurveySendLog.alumni_id,
            SurveyResponse.submitted_at >= survey_email._resurvey_cutoff(),
            SurveyResponse.status.in_(survey_email.RESPONDED_STATUSES),
            # A reply an engineer reset has superseded no longer counts as a
            # reply anywhere (#395) — including here, or the call sheet would
            # disagree with the send about who still owes us an answer.
            survey_email.response_not_superseded(),
        )
        .exists()
    )
    return (
        select(
            SurveySendLog.graduation_year,
            SurveySendLog.alumni_id,
            func.max(SurveySendLog.sent_at).label("last_sent_at"),
        )
        # The cycle scope: only log rows belonging to the campaign the year is
        # on RIGHT NOW. Drop this join and the count becomes an all-time one.
        .join(
            SurveySchedule,
            (SurveySchedule.graduation_year == SurveySendLog.graduation_year)
            & (SurveySchedule.cycle_seq == SurveySendLog.cycle_seq),
        )
        # Pre-reset sends do not count toward the three stages either (#395).
        # Someone reset mid-campaign is owed the whole campaign again, so they
        # are not a finished-and-silent follow-up case until it has run.
        .where(survey_email.send_not_superseded())
        .where(~replied)
        .group_by(SurveySendLog.graduation_year, SurveySendLog.alumni_id)
        .having(func.count(func.distinct(SurveySendLog.stage)) == len(_ALL_STAGES))
    )


def _qualifying_reply(status_filter: tuple[str, ...]):
    """Correlated EXISTS: this send-log row's alumnus has replied.

    The SAME definition of "replied" the sender and the follow-up list use — a
    reply inside the re-survey window, in a status that counts, not superseded by
    a later reset (#395). Sharing it is the point: a progress table built on a
    looser rule would show a cohort as answered while the sender still considers
    them owed an email.
    """
    return (
        select(SurveyResponse.survey_response_id)
        .where(
            SurveyResponse.alumni_id == SurveySendLog.alumni_id,
            SurveyResponse.submitted_at >= survey_email._resurvey_cutoff(),
            SurveyResponse.status.in_(status_filter),
            survey_email.response_not_superseded(),
        )
        .exists()
    )


def _cycle_progress():
    """``(graduation_year, recipients, replied, awaiting_review)`` per year, for
    the campaign each year is on RIGHT NOW.

    This is the "how is it actually going" question, which the existing counts
    could not answer between the first email and the last. ``sent_initial`` and
    friends say what LEFT; ``non_responders`` only counts people who have had all
    three emails, so mid-campaign it is legitimately zero no matter how many have
    replied. Neither tells you the response rate while the campaign is running,
    which is the whole of #543.

    Counted over the send log rather than over responses, so every number shares
    one denominator: you cannot reply to a survey you were never sent, and a
    stray response from someone outside this cycle cannot push the rate above
    100%.

    ``awaiting_review`` is the actionable one — replies sitting in the queue
    waiting for someone to apply or reject them.
    """
    replied = _qualifying_reply(survey_email.RESPONDED_STATUSES)
    pending = _qualifying_reply(("pending",))
    return (
        select(
            SurveySendLog.graduation_year,
            func.count(func.distinct(SurveySendLog.alumni_id)).label("recipients"),
            func.count(func.distinct(case((replied, SurveySendLog.alumni_id)))).label(
                "replied"
            ),
            func.count(func.distinct(case((pending, SurveySendLog.alumni_id)))).label(
                "awaiting_review"
            ),
        )
        # The cycle scope, identical to `_cycle_non_responders`. Without this join
        # the counts become all-time and a year on its second campaign reports
        # last year's replies as this year's (#357).
        .join(
            SurveySchedule,
            (SurveySchedule.graduation_year == SurveySendLog.graduation_year)
            & (SurveySchedule.cycle_seq == SurveySendLog.cycle_seq),
        )
        .where(survey_email.send_not_superseded())
        .group_by(SurveySendLog.graduation_year)
    )


async def _progress_counts(
    session: AsyncSession,
) -> dict[int, tuple[int, int, int]]:
    """``{year: (recipients, replied, awaiting_review)}`` in ONE query, so the
    console's table costs a fixed number of round trips however many years exist."""
    rows = (await session.execute(_cycle_progress())).all()
    return {
        year: (int(recipients or 0), int(replied or 0), int(awaiting or 0))
        for year, recipients, replied, awaiting in rows
    }


async def _non_responder_counts(session: AsyncSession) -> dict[int, int]:
    """How many alumni need manual follow-up, keyed by graduation year.

    Resolved for EVERY year in one query, like ``_sent_counts_by_stage``, so
    listing the console's schedule table stays a fixed number of round trips."""
    sub = _cycle_non_responders().subquery()
    stmt = select(sub.c.graduation_year, func.count()).group_by(
        sub.c.graduation_year
    )
    return {year: n for year, n in (await session.execute(stmt)).all()}


async def _non_responder_count(
    session: AsyncSession, graduation_year: int
) -> int:
    """The manual-follow-up count for ONE year's current cycle."""
    sub = (
        _cycle_non_responders()
        .where(SurveySendLog.graduation_year == graduation_year)
        .subquery()
    )
    total = (
        await session.execute(select(func.count()).select_from(sub))
    ).scalar()
    return int(total or 0)


async def list_non_responders(
    session: AsyncSession, graduation_year: int
) -> list[SurveyNonResponder] | None:
    """WHO needs manual follow-up for this year — the count made actionable.

    A number alone cannot be worked: staff need the names and addresses to pick
    up the phone. Returns ``None`` when the year has no schedule (the route
    404s), which is distinct from a scheduled year with nobody left to chase
    (an empty list).

    Ordered by name so the list reads like a call sheet and is stable between
    refreshes. Alumni whose record has since been archived drop out — the join
    to ``alumni`` is the current record, and chasing an archived one is not a
    follow-up anyone should be handed."""
    sched = await _load_schedule_row(session, graduation_year)
    if sched is None:
        return None
    sub = (
        _cycle_non_responders()
        .where(SurveySendLog.graduation_year == graduation_year)
        .subquery()
    )
    stmt = (
        select(
            Alumni.alumni_id,
            Alumni.preferred_first_name,
            Alumni.first_name,
            Alumni.last_name,
            AlumniContactInfo.personal_email,
            AlumniContactInfo.work_email,
            sub.c.last_sent_at,
        )
        .join(sub, sub.c.alumni_id == Alumni.alumni_id)
        .outerjoin(
            AlumniContactInfo,
            AlumniContactInfo.alumni_id == Alumni.alumni_id,
        )
        .where(Alumni.archived.is_(False))
        .order_by(Alumni.last_name, Alumni.first_name, Alumni.alumni_id)
    )
    rows = (await session.execute(stmt)).all()
    items: list[SurveyNonResponder] = []
    for alumni_id, preferred, first, last, personal, work, last_sent_at in rows:
        name = " ".join(p for p in (preferred or first, last) if p).strip()
        # The address the survey went to — personal preferred, work as the
        # fallback (#392). Showing the personal column unconditionally left this
        # call sheet blank for exactly the alumni the work-email fallback newly
        # reaches. Display preference, not the send gate: whatever is on file is
        # shown, so staff can see (and fix) an address rather than a blank.
        email = email_reach.preferred_display_email(personal, work)
        items.append(
            SurveyNonResponder(
                alumni_id=alumni_id,
                name=name or f"Alum #{alumni_id}",
                email=email,
                last_sent_at=last_sent_at,
            )
        )
    return items


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


async def _all_time_send_counts(session: AsyncSession) -> dict[int, int]:
    """Emails ever sent for each graduation year, ACROSS EVERY CYCLE (#398).

    Distinct from :func:`_sent_counts_by_stage`, which is current-cycle only.
    This is the number the delete confirmation states out loud: "the record of
    the N emails this year has been sent is kept". It no longer decides WHETHER a
    campaign may be deleted — any campaign may (see :func:`delete_schedule`) —
    but it is exactly the figure a person needs to see before pressing a button
    labelled "delete", because that word reads like those emails go with it.
    Superseded rows are counted too: neither a reset nor a retirement un-sends an
    email."""
    stmt = select(SurveySendLog.graduation_year, func.count()).group_by(
        SurveySendLog.graduation_year
    )
    return {year: int(n) for year, n in (await session.execute(stmt)).all()}


def _to_item(
    sched: SurveySchedule,
    counts: dict[tuple[int, int], int],
    creators: dict[int, str],
    non_responders: dict[int, int] | None = None,
    all_time_sent: dict[int, int] | None = None,
    progress: dict[int, tuple[int, int, int]] | None = None,
) -> SurveyScheduleItem:
    year = sched.graduation_year
    created_by_id = getattr(sched, "created_by_user_id", None)
    recipients, replied, awaiting_review = (progress or {}).get(year, (0, 0, 0))
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
        non_responders=(non_responders or {}).get(year, 0),
        emails_sent_all_time=(all_time_sent or {}).get(year, 0),
        recipients=recipients,
        replied=replied,
        awaiting_review=awaiting_review,
    )


# ----------------------------------------------------------- management --------


async def list_schedules(session: AsyncSession) -> list[SurveyScheduleItem]:
    """Every schedule (newest cohort first) with its per-stage delivered counts
    and its manual-follow-up count."""
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
    non_responders = await _non_responder_counts(session)
    all_time = await _all_time_send_counts(session)
    progress = await _progress_counts(session)
    creators = await _creator_names(session, schedules)
    return [
        _to_item(s, counts, creators, non_responders, all_time, progress)
        for s in schedules
    ]


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
    non_responders = await _non_responder_counts(session)
    all_time = await _all_time_send_counts(session)
    progress = await _progress_counts(session)
    creators = await _creator_names(session, [sched])
    return _to_item(sched, counts, creators, non_responders, all_time, progress)


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

    ``cycle_seq`` is deliberately NOT advanced for an EXISTING row (#357). That
    path is the "I typed the wrong start date" correction, and it must stay
    non-destructive: advancing the cycle would make every already-emailed alum a
    target again, so fixing a typo would blast the cohort a second time. Starting
    the next annual campaign is the separate, explicit :func:`start_new_cycle`.

    A row being created FRESH is a different question, and it is where deleting a
    campaign is made safe (#398): the year may have retired cycles whose send-log
    rows are still in the table, so the new campaign starts at
    :func:`survey_email.current_cycle_seq` — one above the highest retired cycle
    — rather than at 1. Defaulting to 1 there is the #357 failure: the retired
    rows would read as this campaign's, everyone would look already-emailed, and
    it would complete having sent to nobody. For a year that has never had a
    campaign this is 1, exactly as before."""
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
                cycle_seq=await survey_email.next_cycle_seq(
                    session, graduation_year
                ),
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
    real rather than abstract.

    ``previously_emailed`` counts EVERY send-log row in the current cycle,
    including ones an engineer reset has superseded (#395): the question it
    answers is "who has already had an email from us this cycle", and a reset
    person had one. Only `eligible` is a blocking question, and it comes from
    `_load_recipients`, which applies the reset rules."""
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


# --------------------------------------- campaign left behind by a send (#405) -
#
# Jake, 2026-08-05: he cleared every campaign, then sent to a graduation year from
# the console. Resend confirms the emails; the Surveys console showed no campaign
# for that year.
#
# That was correct as built — `POST /survey/campaigns/{year}/send` writes
# `survey_send_log` rows and nothing else — and the consequence was not. The
# SCHEDULE ROW is what the cron iterates, and the cadence (day 0 / +7 / +14) is
# derived from its `start_date`. With no row there is nothing for the cron to
# pick up, so a manual send delivers the initial email and THE TWO REMINDERS
# NEVER FIRE. Nothing says so: the send reports success, the console lists no
# campaign, and the silence looks like "no campaign was scheduled" rather than
# "two thirds of a campaign was silently dropped".
#
# AUTOMATIC, NOT A REFUSAL. The alternative was to refuse the send until a
# campaign exists, which is more predictable. Chosen against, for four reasons:
#
#   1. There is nothing left for the operator to decide. The cohort is the year
#      they picked, the cadence is fixed, and the start date is "when the initial
#      went out" — which the send log already knows exactly. A refusal makes them
#      re-enter facts the system holds, and the one field they could get wrong
#      (the start date) is precisely the one that silently mis-times reminders.
#   2. The failure it prevents is invisible; the state it creates is not. A
#      spurious campaign is listed in the console and deletable (#398, the same
#      day). A missing one shows nowhere and drops two emails.
#   3. Refusing leaves the ALREADY-BROKEN cohorts unfixable from the console —
#      Jake's cohort has send-log rows and no campaign right now, and pressing
#      Send again is how it gets one (nothing is re-emailed; see below).
#   4. Silently sending with no campaign is the one option that is definitely
#      wrong, and a refusal is not obviously better than the ask in the issue.
#
# THE CREATION IS DRIVEN BY THE SEND LOG, NOT BY THIS CALL'S OUTCOME. The
# campaign exists if and only if an email for (year, current cycle, stage 0) is
# on record, and its `start_date` is the EARLIEST such row. Three things fall out
# of that one rule, rather than needing three rules:
#
#   * a DRY RUN claims nothing, so there is nothing to find and nothing is
#     created — the guard is structural, not a second `if dry_run`;
#   * a send that emailed nobody because nobody was eligible creates no empty
#     campaign;
#   * a REPEAT send for a year that was emailed before this fix (sent=0, every
#     recipient already claimed) still leaves the campaign behind, BACKDATED to
#     the day those emails really went out — which is the repair path for the
#     cohorts already in this state. Using `today` there would shift the whole
#     cadence by however many days had passed.
#
# A PARTIAL SEND (the `limit` query param, or Resend throttling the run) creates a
# campaign that claims nothing about the people it did not reach. Every count the
# console shows comes from `survey_send_log`, so `sent_initial` is the real,
# partial number; and `select_stage_targets` scans stage 0 first, so the next cron
# run finishes the initial for the remainder BEFORE any reminder goes out,
# whatever the calendar says. The start date is the first delivered email either
# way, so the reminders are timed off a real event.


async def create_campaign_for_send(
    session: AsyncSession,
    *,
    graduation_year: int,
    cycle_seq: int,
    actor_user_id: int | None,
) -> bool:
    """Leave a campaign behind for a manual send to an unscheduled year (#405).

    Returns True when a ``survey_schedule`` row was created. Callers must only
    reach here having established that the year has NO schedule; this never
    replaces or edits an existing campaign (that is :func:`create_schedule` and
    :func:`start_new_cycle`, which mean different things).

    ``cycle_seq`` is PASSED IN — the cycle the send itself claimed under — and is
    never re-resolved here. That is the whole safety argument of this function.
    A manual send with no schedule resolves its cycle through
    :func:`survey_email.current_cycle_seq`, which for such a year returns
    ``next_cycle_seq`` — exactly what ``_upsert_schedule`` would have chosen for a
    fresh row. So the two agree today by construction, and threading the value
    through means they cannot be made to disagree by a later edit to either. The
    campaign therefore opens on the cycle the just-written send-log rows are in,
    which is what makes those rows read as ITS stage 0: the cycle-scoped
    double-send guard (``survey_email.logged_alumni_ids``) sees them, finds every
    recipient already logged for stage 0, and the campaign picks up at the
    reminders instead of re-emailing the cohort.

    Created ``active``, not ``scheduled``: the initial has already gone out. Both
    are runnable so the cron behaves identically either way, but ``scheduled``
    would tell an operator this campaign has not sent anything yet.
    """
    start_date = await _first_send_date(
        session, graduation_year=graduation_year, cycle_seq=cycle_seq
    )
    if start_date is None:
        # Nothing was emailed for this cycle — a dry run, or a send with no
        # eligible recipients. There is no campaign to leave behind.
        return False

    session.add(
        SurveySchedule(
            graduation_year=graduation_year,
            start_date=start_date,
            status=STATUS_ACTIVE,
            cycle_seq=cycle_seq,
            created_by_user_id=actor_user_id,
            last_run_at=_now(),
        )
    )
    session.add(
        AuditLog(
            user_id=actor_user_id,
            action_type="create_survey_schedule",
            entity_type="survey_campaign",
            entity_id=graduation_year,
            new_value=(
                f"created by a manual send; grad_year={graduation_year} "
                f"cycle={cycle_seq} start_date={start_date.isoformat()} "
                f"status={STATUS_ACTIVE}"
            ),
        )
    )
    await session.commit()
    return True


async def _first_send_date(
    session: AsyncSession, *, graduation_year: int, cycle_seq: int
) -> datetime.date | None:
    """The day this cycle's INITIAL email actually went out, or None if it hasn't.

    The reminder cadence is measured from ``start_date``, so a campaign created
    after the fact has to be anchored to a real event rather than to the clock at
    the moment somebody pressed a button. ``min(sent_at)`` over the cycle's stage-0
    rows is that event, recorded by the claim itself.

    Stage 0 specifically: a later stage is only reachable when a schedule already
    exists, so it cannot be the start of a campaign there is none of.

    Superseded rows (#395) are deliberately NOT filtered out. The question is
    "when did we first email this cohort", and a reset does not un-send an email;
    excluding them could only move the start date later, which is the direction
    that mis-times reminders.

    UTC, matching ``_today()`` — the clock ``_load_schedules_due`` compares
    ``start_date`` against.
    """
    first = await session.scalar(
        select(func.min(SurveySendLog.sent_at)).where(
            SurveySendLog.graduation_year == graduation_year,
            SurveySendLog.cycle_seq == cycle_seq,
            SurveySendLog.stage == STAGE_INITIAL,
        )
    )
    if first is None:
        return None
    if first.tzinfo is not None:
        first = first.astimezone(datetime.UTC)
    return first.date()


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
    session: AsyncSession, graduation_year: int, *, actor_user_id: int | None = None
) -> SurveyScheduleItem | None:
    """Cancel a schedule (no further sends). Returns None if there is none.

    Terminal and non-destructive: the row stays, keeping its ``cycle_seq`` and
    its place beside the send log, so the year's history still reads as "cycle N
    ran, and here is what it sent".

    NOT a weaker :func:`delete_schedule`, and not superseded by it (#398): delete
    now works on any campaign, but "stop this cohort's emails and leave the
    campaign listed" is a different intent from "get this campaign off my
    screen", and an operator wants both. Cancel is the one that keeps the year on
    the console with its counts attached.

    Audited, matching pause/resume and the two blanket switches — stopping a
    cohort's campaign is an intervention someone will later want explained."""
    existing = (
        await session.execute(
            select(SurveySchedule).where(
                SurveySchedule.graduation_year == graduation_year
            )
        )
    ).scalar_one_or_none()
    if existing is None:
        return None
    previous = existing.status
    existing.status = STATUS_CANCELLED
    session.add(
        AuditLog(
            user_id=actor_user_id,
            action_type="cancel_survey_schedule",
            entity_type="survey_campaign",
            entity_id=graduation_year,
            field_name="status",
            old_value=previous,
            new_value=STATUS_CANCELLED,
        )
    )
    await session.commit()
    return await get_schedule(session, graduation_year)


# ------------------------------------------------------ delete a campaign ------
#
# #398, Jake 2026-08-05: "make it so in the surveys you can delete the survey
# campaigns next to the resume button." A campaign scheduled for the wrong year
# or created by mistake was stuck there forever — pausing hid it, the row stayed.
#
# ANY CAMPAIGN, ANY STATUS (revised the same day). The first cut could only
# delete a campaign that had never emailed anyone; everything else was offered
# `cancel`, and an already-cancelled campaign got no control at all. Jake: "it
# still won't let me delete a campaign in the engineer dashboard" — his campaigns
# have all either sent or are already cancelled. Told why and offered the
# options, he chose: delete any campaign, KEEP the emails.
#
# DELETING A CAMPAIGN TAKES THE SCHEDULE ROW AND NOTHING ELSE. `survey_send_log`
# (what we emailed) and `survey_responses` (what alumni told us) are never
# touched, here or anywhere else — the same rule the per-alumnus reset was
# rewritten around on the same day.
#
# THE CONSTRAINT THAT MADE THE REFUSAL REAL, AND HOW IT IS ANSWERED
# -----------------------------------------------------------------
# `survey_schedule` is the ONLY holder of a year's `cycle_seq`, and the send log
# is scoped by it. Drop the row for a year with send-log rows in cycle 2 and the
# next campaign for that year starts at cycle 1 again: every alum reads as
# already emailed, `select_stage_targets` returns nothing at every stage, and the
# campaign "completes" having sent to nobody. That is #357 verbatim — a bug this
# codebase has already paid for once, and it fails SILENTLY.
#
# So the cycle number is the one thing that must outlive the row, and RETIRING
# the cycle is what this function does. It writes a `survey_campaign_retirement`
# row carrying the deleted campaign's `cycle_seq`, and
# `survey_email.current_cycle_seq` resolves a year with no schedule to one ABOVE
# the highest retired cycle. The retired campaign's send-log rows keep their own
# cycle number and simply stop being current:
#
#   * the cycle-scoped double-send guard (`logged_alumni_ids`) no longer sees
#     them, so the alumni that campaign emailed are eligible again;
#   * the send log's UNIQUE (year, alumni, stage, cycle, reset) cannot collide
#     with them, so the next campaign's claims are really inserted rather than
#     swallowed by `_claim_batch`'s ON CONFLICT DO NOTHING — which is the half of
#     this that would otherwise fail silently, exactly as it would have for the
#     per-alumnus reset without `reset_seq` (#395).
#
# That is deliberately the SAME mechanism as the reset, one level up: an
# append-only event that supersedes, never a rewrite of the rows superseded. A
# reset retires one alumnus's sends by `reset_seq`; this retires one campaign's
# sends by `cycle_seq`.
#
# WHAT A NEW CAMPAIGN FOR THAT YEAR THEN DOES. It emails everyone eligible,
# INCLUDING the people the deleted campaign emailed. Alumni who ANSWERED are
# still held out by the 365-day annual re-survey window — unchanged, and the same
# as after `start_new_cycle`. Deleting a campaign is not a way to re-ask someone
# who already replied; the per-alumnus reset is.
#
# CANCEL SURVIVES, and is not a lesser delete. Stopping a live campaign while
# keeping it listed is its own thing an operator wants, so both are offered:
# cancel for a campaign that is still running, delete for any campaign at all.


async def delete_schedule(
    session: AsyncSession, graduation_year: int, *, actor_user_id: int | None
) -> SurveyScheduleDeleteResult | None:
    """Delete a graduation year's campaign — whatever its status (#398).

    Returns ``None`` when the year has no schedule (the route 404s). Never
    refuses otherwise: `scheduled`, `active`, `paused`, `completed` and
    `cancelled` all delete, because a campaign nobody can remove is the complaint
    this is fixing.

    Removes the ``survey_schedule`` row and writes ONE
    ``survey_campaign_retirement`` row in its place, retiring the cycle it was on
    (see the section notes above). Nothing else changes: every ``survey_send_log``
    row and every ``survey_responses`` row stays exactly as it is, still on the
    alumni's profiles, still counted by the Resend usage meter — the emails
    really were sent. The counts are reported back and audited so the console can
    say what was retired rather than imply it was destroyed.
    """
    sched = (
        await session.execute(
            select(SurveySchedule).where(
                SurveySchedule.graduation_year == graduation_year
            )
        )
    ).scalar_one_or_none()
    if sched is None:
        return None

    previous_status = sched.status
    start_date = sched.start_date
    cycle_seq = getattr(sched, "cycle_seq", survey_email.FIRST_CYCLE)

    # This campaign's own emails — the rows the retirement moves out of the way.
    # Scoped to the cycle, because an earlier cycle's rows were retired by
    # whatever retired THEM and are not this action's doing.
    emails_retired = int(
        await session.scalar(
            select(func.count())
            .select_from(SurveySendLog)
            .where(
                SurveySendLog.graduation_year == graduation_year,
                SurveySendLog.cycle_seq == cycle_seq,
            )
        )
        or 0
    )
    responses_kept = int(
        await session.scalar(
            select(func.count())
            .select_from(SurveyResponse)
            .where(SurveyResponse.graduation_year == graduation_year)
        )
        or 0
    )

    session.add(
        SurveyCampaignRetirement(
            graduation_year=graduation_year,
            cycle_seq=cycle_seq,
            # Stamped here rather than left to the column default, so the event
            # carries the moment the operator acted (and so the row is complete
            # before it is flushed) — the same choice `reset_alumnus` makes.
            retired_at=_now(),
            retired_by_user_id=actor_user_id,
            previous_status=previous_status,
            start_date=start_date,
            sends_retired=emails_retired,
            responses_kept=responses_kept,
        )
    )
    await session.delete(sched)
    session.add(
        AuditLog(
            user_id=actor_user_id,
            action_type="delete_survey_schedule",
            entity_type="survey_campaign",
            entity_id=graduation_year,
            old_value=(
                f"status={previous_status} cycle={cycle_seq} "
                f"start_date={start_date.isoformat()}"
            ),
            # Spelled out because the obvious reading of "deleted a campaign" is
            # that the emails and answers went with it. They did not — they were
            # RETIRED, which is a statement about what the next campaign can see,
            # not about what is in the database.
            new_value=(
                f"deleted; retired cycle={cycle_seq} "
                f"(emails_retired={emails_retired} kept, "
                f"responses_kept={responses_kept}); "
                f"next campaign for {graduation_year} starts at "
                f"cycle={cycle_seq + 1}"
            ),
        )
    )
    await session.commit()
    return SurveyScheduleDeleteResult(
        graduation_year=graduation_year,
        previous_status=previous_status,
        retired_cycle=cycle_seq,
        next_cycle=cycle_seq + 1,
        emails_retired=emails_retired,
        responses_kept=responses_kept,
    )


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
    """How many emails may still be sent under the configured cap, or ``None``
    for unlimited (cap disabled).

    The cap is account-wide: the daily and monthly budgets minus what Resend has
    already sent today / this month (the same usage tally the console shows). A
    send may go up to whichever budget is tighter.

    NOT the cron's private number, despite living here (#417). It is read by the
    cron once per run to pace ALL years against one budget, and again inside
    :func:`survey_email.send_survey_stage`, which is where it is actually
    ENFORCED — so the console's manual send is bounded by it too. It used to be
    called from `_run_due_schedules_locked` alone, which is exactly why "Send
    now" could email an entire cohort straight past a limit the console was
    displaying beside the button. Reached through this module's attribute rather
    than copied, so the meter, the pacing and the gate stay one number."""
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

    ONE RUN AT A TIME (#358). The whole thing happens under
    :func:`survey_email.send_lock`; a second run — another cron delivery, or an
    admin's manual send — that cannot take the lock returns immediately with
    ``skipped_locked=True`` and an empty ``ran``. It does not wait and does not
    error: nothing is lost by skipping, since whatever was due is still due on
    the next run, and the alternative (two runs each reading the full daily
    budget before either has claimed anything) spends it twice.

    COMPLETION IS NOT SUCCESS (#359). A completed campaign reports
    ``non_responders`` — the alumni who received all three emails and never
    replied. They are the ones #151's third step wanted flagged for manual
    follow-up, and without the number a campaign that converted nobody looks
    exactly like one that converted everybody.
    """
    # Fail fast on misconfiguration rather than reporting an empty, clean run.
    # `send_survey_stage` checks these too; this only moves the error earlier.
    # Deliberately BEFORE the lock: a misconfigured deployment should say so on
    # every attempt, not depend on winning a race first.
    settings = survey_email.get_settings()
    if not settings.survey_app_base_url:
        raise ServiceError("SURVEY_APP_BASE_URL is not configured.")
    if not settings.survey_from_email:
        raise ServiceError("SURVEY_FROM_EMAIL is not configured.")

    async with survey_email.send_lock() as acquired:
        if not acquired:
            log.warning(
                "Survey cron skipped: another send already holds the lock."
            )
            return SurveyScheduleRunSummary(ran=[], skipped_locked=True)
        return await _run_due_schedules_locked(session, actor_user_id)


async def _run_due_schedules_locked(
    session: AsyncSession, actor_user_id: int | None
) -> SurveyScheduleRunSummary:
    """The cron body, run with the send lock held. See :func:`run_due_schedules`."""
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
                # Finished is not the same as answered: report how many of this
                # cycle's recipients never replied, so `completed` carries the
                # manual-follow-up backlog with it instead of hiding it (#359).
                # One extra query, and only on the run that completes a campaign.
                sched.status = STATUS_COMPLETED
                needs_followup = await _non_responder_count(session, year)
                if needs_followup:
                    log.info(
                        "Survey campaign %s (cycle %s) completed with %s "
                        "alumni who never responded — manual follow-up needed.",
                        year,
                        cycle_seq,
                        needs_followup,
                    )
                ran.append(
                    SurveyScheduleRunItem(
                        graduation_year=year,
                        stage=None,
                        sent=0,
                        remaining=0,
                        non_responders=needs_followup,
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
        #
        # `send_survey_stage` now re-reads the same budget and clamps to the
        # tighter of the two (#417), which does NOT double-count: every delivered
        # email is claimed and committed before the next year is considered, so
        # the re-read has already absorbed exactly what was subtracted from
        # `allowance` here. This local running total stays because it is what
        # PACES one run across several years — the earliest campaign drains
        # first — and because it is the one figure that stays correct after a 429
        # releases a claim (the re-read would then read looser; `min` keeps this).
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
