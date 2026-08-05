"""Per-alumnus survey campaign reset — the engineer's replacement for SQL (#395).

Re-surveying ONE person had no route: staff hand-ran DELETE statements against
the database twice. This module is that operation, done once, correctly, with a
guard and an audit trail.

IT DESTROYS NOTHING (revised 2026-08-05)
----------------------------------------
The first version of this deleted the alum's ``survey_responses`` rows (every
status), their ``survey_send_log`` rows, and their staged survey photos. Jake,
2026-08-05: *"when you reset the campaign the responses should not be reset, they
should still be in the db."* He is right — the submitted answers are the product
of the survey, and "this person should be asked again" is no reason to throw away
what they said last time, least of all an answer nobody has reviewed yet.

So a reset is now an EVENT, recorded in ``survey_reset_log``, and every query
that decides eligibility ignores what predates it. No row is deleted, no row is
even updated: the responses stay exactly as submitted, the send log stays
append-only, staged photos stay in the bucket, and the profile's Surveys tab
keeps rendering the lot (``profile._derive_survey_history``, which marks the
superseded ones as belonging to an earlier cycle).

WHAT ACTUALLY BLOCKS A RE-SEND (both, always)
---------------------------------------------
Two independent tables hold an alumnus out of a campaign, and clearing only one
leaves them just as stuck — which is exactly the trap that sent people back to
SQL:

* ``survey_send_log`` — UNIQUE on ``(graduation_year, alumni_id, stage,
  cycle_seq, reset_seq)``. A row means "we already emailed them at this stage of
  this campaign", so the sender skips them. See
  :class:`app.models.survey_schedule.SurveySendLog`.
* ``survey_responses`` — the 365-day re-survey window. A ``pending`` or
  ``applied`` row inside the window makes them "already replied" and excludes
  them from the send (:data:`app.services.survey_email.RESPONDED_STATUSES`, via
  ``_replied_recently_exists``).

The reset supersedes BOTH, for that one ``alumni_id`` and nothing else.
Deliberately NOT scoped to a year or a cycle: the operator's question is "make
this person surveyable again", and a leftover row from any campaign can answer it
wrongly. There is no bulk or cohort form of this function and there must never be
one — re-running a whole cohort is `start_new_cycle`, which advances the cycle.

EVERY ELIGIBILITY QUERY HAS TO KNOW
-----------------------------------
If one of them misses the reset, the console and the sender disagree about who is
eligible — the standing bug class in this area. The complete list, all of which
apply :func:`survey_email.response_not_superseded` /
:func:`survey_email.send_not_superseded`:

* ``survey_email._replied_recently_exists`` — the send exclusion itself, and
  through ``_survey_cohort_query`` also ``eligible_alumni_query``,
  ``unreachable_alumni_query``, ``unreachable_counts_by_year`` and
  ``recipient_breakdown``'s ``already_responded``.
* ``survey_email.list_graduation_years`` — the console year picker's "N replied".
* ``survey_email.logged_alumni_ids`` — the double-send guard, used by
  ``select_stage_targets`` for BOTH the cron and the manual console send.
* ``survey_schedule._cycle_non_responders`` — the manual-follow-up counts and the
  non-responder call sheet (both the reply test and the all-three-stages test).
* :func:`get_state` below, which must report what the sender would actually do.

Two counters deliberately do NOT filter, because they measure emails sent rather
than people blocked: ``survey_schedule._sent_counts_by_stage`` and
``survey_email.get_send_usage`` (the Resend budget meter). A reset alum who is
emailed twice really did cost two sends.

A PENDING RESPONSE SURVIVES AND STAYS REVIEWABLE
------------------------------------------------
Resetting someone whose answer is still awaiting review does not touch that
answer: it stays ``pending``, stays in the admin review queue
(``survey_responses.list_pending``), keeps its staged photo, and can still be
applied to the record afterwards. Applying it later does not re-block them —
supersession is decided by ``submitted_at``, not by status. This is the single
biggest reason the old behaviour was wrong: it deleted those answers unread.

NOTHING ELSE IS PER-ALUMNUS SURVEY STATE
----------------------------------------
The survey link is a **stateless signed token** (``survey_email.make_survey_token``
/ ``verify_survey_token``) — an HMAC over the alumni id and an expiry, with no
row anywhere — so there is no token table to clear and a fresh send simply mints
a new link. ``survey_schedule`` is per graduation YEAR, not per person, and must
not be touched by a single-alumnus reset.
"""

from __future__ import annotations

import datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import NotFoundError
from app.models.alumni import Alumni
from app.models.audit import AuditLog
from app.models.contact import AlumniContactInfo
from app.models.survey_reset import SurveyResetLog
from app.models.survey_response import SurveyResponse
from app.models.survey_schedule import SurveySchedule, SurveySendLog
from app.schemas.survey import (
    SurveyAlumniResponse,
    SurveyAlumniSend,
    SurveyAlumniState,
    SurveyResetResult,
)
from app.services import survey_email

# Human labels for `survey_send_log.stage`. Same three stages the scheduler
# sends; spelled out here because the engineer reading this screen is diagnosing
# a stuck send, not reading a log table.
_STAGE_LABELS: dict[int, str] = {
    survey_email.STAGE_INITIAL: "Initial email",
    survey_email.STAGE_REMINDER_1: "1-week reminder",
    survey_email.STAGE_REMINDER_2: "2-week reminder",
}


def _stage_label(stage: int) -> str:
    return _STAGE_LABELS.get(stage, f"Stage {stage}")


def _as_utc(value: datetime.datetime) -> datetime.datetime:
    """``value`` as an aware UTC datetime.

    ``survey_responses.submitted_at`` is ``DateTime(timezone=True)`` and Postgres
    hands it back aware, but the 365-day window below compares it against an
    aware cutoff in PYTHON — and comparing an aware datetime to a naive one is a
    TypeError, not a wrong answer. Treating a naive value as UTC keeps that
    comparison from being able to raise at all.
    """
    return (
        value.replace(tzinfo=datetime.UTC) if value.tzinfo is None else value
    )


def _name(alum: Alumni) -> str:
    """The alumnus's name for display and for the confirmation copy.

    Never empty: the confirm step must be able to NAME the person it is about to
    re-open, so this falls back to the id rather than to a blank.
    """
    full = " ".join(p for p in (alum.first_name, alum.last_name) if p).strip()
    return full or (alum.preferred_first_name or "").strip() or f"Alum #{alum.alumni_id}"


async def _load_alum(
    session: AsyncSession, alumni_id: int, *, lock: bool = False
) -> Alumni:
    stmt = select(Alumni).where(Alumni.alumni_id == alumni_id)
    if lock:
        # Serializes two resets of the SAME alumnus (a double-click, two tabs):
        # both would otherwise read the same reset count and try to claim the
        # same `reset_seq`, and one would die on the unique constraint. Row-level
        # and per-alumnus, so it never blocks anyone else's work.
        stmt = stmt.with_for_update()
    alum = (await session.execute(stmt)).scalar_one_or_none()
    if alum is None:
        raise NotFoundError("Alum not found.")
    return alum


async def _reset_rows(
    session: AsyncSession, alumni_id: int
) -> list[SurveyResetLog]:
    """Every reset this alumnus has had, oldest first."""
    return list(
        (
            await session.scalars(
                select(SurveyResetLog)
                .where(SurveyResetLog.alumni_id == alumni_id)
                .order_by(SurveyResetLog.reset_seq)
            )
        ).all()
    )


async def get_state(session: AsyncSession, alumni_id: int) -> SurveyAlumniState:
    """This alumnus's full survey state — what was sent, what came back, and
    what (if anything) is holding them out of the next send.

    Read-only. Shown BEFORE the reset button is armed so the engineer can decide
    whether a reset is warranted at all; ``blocked_reasons`` empty means it is
    not, because there is nothing to unblock — the reset would be a no-op that
    only adds a row to the reset log.

    Every "does this block?" answer here is computed with the SAME rules the
    sender applies, supersession included, so this screen cannot promise
    something the send would then contradict.
    """
    alum = await _load_alum(session, alumni_id)

    email = await session.scalar(
        select(AlumniContactInfo.personal_email).where(
            AlumniContactInfo.alumni_id == alumni_id
        )
    )
    if not email:
        email = await session.scalar(
            select(AlumniContactInfo.work_email).where(
                AlumniContactInfo.alumni_id == alumni_id
            )
        )

    # The cohort's campaign, when their year has one. Its `cycle_seq` is what
    # makes a send-log row current (blocking) rather than old history — the same
    # cycle scoping the scheduler uses (#357). A year with no schedule is read as
    # the first cycle, matching `survey_email.FIRST_CYCLE`.
    schedule = None
    if alum.graduation_year is not None:
        schedule = (
            await session.execute(
                select(SurveySchedule).where(
                    SurveySchedule.graduation_year == alum.graduation_year
                )
            )
        ).scalar_one_or_none()
    current_cycle = schedule.cycle_seq if schedule else survey_email.FIRST_CYCLE

    resets = await _reset_rows(session, alumni_id)
    reset_count = len(resets)
    last_reset_at = resets[-1].reset_at if resets else None

    send_rows = list(
        (
            await session.scalars(
                select(SurveySendLog)
                .where(SurveySendLog.alumni_id == alumni_id)
                .order_by(SurveySendLog.sent_at)
            )
        ).all()
    )
    sends = [
        SurveyAlumniSend(
            graduation_year=s.graduation_year,
            cycle_seq=s.cycle_seq,
            stage=s.stage,
            stage_label=_stage_label(s.stage),
            sent_at=s.sent_at,
            # Superseded rows are kept and shown — the email was really sent —
            # but they can no longer block anything, so they must not read as
            # "current campaign" either.
            superseded=s.reset_seq < reset_count,
            current_cycle=(
                s.reset_seq == reset_count
                and s.cycle_seq == current_cycle
                and s.graduation_year == alum.graduation_year
            ),
        )
        for s in send_rows
    ]

    response_rows = list(
        (
            await session.scalars(
                select(SurveyResponse)
                .where(SurveyResponse.alumni_id == alumni_id)
                .order_by(SurveyResponse.submitted_at.desc())
            )
        ).all()
    )
    cutoff = survey_email._resurvey_cutoff()

    def superseded(submitted_at: datetime.datetime) -> bool:
        """The Python twin of ``survey_email.response_not_superseded``."""
        return last_reset_at is not None and _as_utc(
            last_reset_at
        ) >= _as_utc(submitted_at)

    responses = [
        SurveyAlumniResponse(
            survey_response_id=r.survey_response_id,
            submitted_at=r.submitted_at,
            status=r.status,
            field_count=len(r.payload or {}),
            has_photo=bool(r.staged_photo_path),
            superseded=superseded(r.submitted_at),
            # The exact predicate the send exclusion applies, not an
            # approximation of it — so what this screen calls "blocking" is what
            # actually blocks.
            blocks_resend=(
                _as_utc(r.submitted_at) >= cutoff
                and r.status in survey_email.RESPONDED_STATUSES
                and not superseded(r.submitted_at)
            ),
        )
        for r in response_rows
    ]

    reasons: list[str] = []
    if alum.archived:
        reasons.append(
            "This record is archived, so no survey campaign includes them. "
            "Restoring the record is the fix — a reset will not change this."
        )
    blocking = [r for r in responses if r.blocks_resend]
    if blocking:
        newest = max(r.submitted_at for r in blocking)
        reasons.append(
            f"They replied on {newest.date().isoformat()}, which is inside the "
            "365-day re-survey window, so the campaign skips them until it "
            "passes."
        )
    current_sends = sorted({s.stage for s in sends if s.current_cycle})
    if current_sends:
        stages = ", ".join(_stage_label(s).lower() for s in current_sends)
        reasons.append(
            f"They were already emailed in the current campaign ({stages}), so "
            "that stage will not send to them again."
        )

    return SurveyAlumniState(
        alumni_id=alum.alumni_id,
        name=_name(alum),
        graduation_year=alum.graduation_year,
        email=email or None,
        archived=bool(alum.archived),
        schedule_status=schedule.status if schedule else None,
        schedule_start_date=schedule.start_date if schedule else None,
        schedule_cycle_seq=schedule.cycle_seq if schedule else None,
        sends=sends,
        responses=responses,
        reset_count=reset_count,
        last_reset_at=last_reset_at,
        blocked_reasons=reasons,
    )


async def reset_alumnus(
    session: AsyncSession, alumni_id: int, *, actor_user_id: int | None
) -> SurveyResetResult:
    """Make ONE alumnus surveyable again, WITHOUT DELETING ANYTHING.

    Writes a single ``survey_reset_log`` row. From that moment every eligibility
    query treats their earlier replies and their earlier survey emails as
    belonging to a finished cycle, so the next campaign send reaches them — and
    every one of those rows is still in the database, still on their profile,
    still reviewable if it was awaiting review, with its staged photo intact.

    Nothing outside this alumnus is touched: no schedule, no other person, no
    cohort. Not undoable in the sense that the log row stays, but nothing is
    lost, which is the whole point of the rewrite (Jake, 2026-08-05).

    Audited as ``reset_survey_campaign`` against the alumnus, carrying what was
    superseded and what was preserved. The actor is an engineer, so the audit
    layer reroutes the row into the append-only ``engineer_action_log`` (#199) —
    the trail exists either way, and now the reset log carries it too.
    """
    alum = await _load_alum(session, alumni_id, lock=True)
    name = _name(alum)

    previous_seq = int(
        await session.scalar(
            select(func.coalesce(func.max(SurveyResetLog.reset_seq), 0)).where(
                SurveyResetLog.alumni_id == alumni_id
            )
        )
        or 0
    )
    reset_seq = previous_seq + 1
    reset_at = datetime.datetime.now(datetime.UTC)

    # What this reset moves out of the way. Send-log rows still carrying the
    # PREVIOUS sequence are the ones that were blocking; anything older was
    # already superseded by an earlier reset and is not moved twice.
    sends_superseded = int(
        await session.scalar(
            select(func.count())
            .select_from(SurveySendLog)
            .where(
                SurveySendLog.alumni_id == alumni_id,
                SurveySendLog.reset_seq == previous_seq,
            )
        )
        or 0
    )
    # Responses are counted, never touched. "Newly superseded" = submitted before
    # now and after the previous reset (if any), which is the set that stops
    # counting because of THIS action.
    newly = [SurveyResponse.alumni_id == alumni_id, SurveyResponse.submitted_at <= reset_at]
    if previous_seq:
        previous_at = await session.scalar(
            select(SurveyResetLog.reset_at).where(
                SurveyResetLog.alumni_id == alumni_id,
                SurveyResetLog.reset_seq == previous_seq,
            )
        )
        if previous_at is not None:
            newly.append(SurveyResponse.submitted_at > previous_at)
    responses_superseded = int(
        await session.scalar(
            select(func.count()).select_from(SurveyResponse).where(*newly)
        )
        or 0
    )
    responses_preserved = int(
        await session.scalar(
            select(func.count())
            .select_from(SurveyResponse)
            .where(SurveyResponse.alumni_id == alumni_id)
        )
        or 0
    )
    # Called out separately because it is the case the old behaviour got most
    # wrong: an unreviewed answer. It stays pending and stays in the queue.
    pending_preserved = int(
        await session.scalar(
            select(func.count())
            .select_from(SurveyResponse)
            .where(
                SurveyResponse.alumni_id == alumni_id,
                SurveyResponse.status == "pending",
            )
        )
        or 0
    )

    session.add(
        SurveyResetLog(
            alumni_id=alumni_id,
            reset_seq=reset_seq,
            reset_at=reset_at,
            reset_by_user_id=actor_user_id,
            sends_superseded=sends_superseded,
            responses_superseded=responses_superseded,
        )
    )
    session.add(
        AuditLog(
            user_id=actor_user_id,
            action_type="reset_survey_campaign",
            entity_type="alumni",
            entity_id=alumni_id,
            field_name="survey_campaign",
            old_value=(
                f"sends={sends_superseded} responses={responses_superseded} "
                f"reset_seq={reset_seq}"
            ),
            # Not "cleared": nothing was. The trail has to read the way the
            # operation actually behaves, or the next person to read it will
            # believe answers were destroyed.
            new_value=(
                f"superseded (kept {responses_preserved} response(s), "
                f"{pending_preserved} awaiting review)"
            ),
        )
    )
    await session.commit()

    return SurveyResetResult(
        alumni_id=alumni_id,
        name=name,
        reset_seq=reset_seq,
        sends_superseded=sends_superseded,
        responses_superseded=responses_superseded,
        responses_preserved=responses_preserved,
        pending_preserved=pending_preserved,
    )
