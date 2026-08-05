"""Per-alumnus survey campaign reset — the engineer's replacement for SQL (#395).

Re-surveying ONE person had no route: staff hand-ran DELETE statements against
the database twice. This module is that operation, done once, correctly, with a
guard and an audit trail.

WHAT ACTUALLY BLOCKS A RE-SEND (both, always)
---------------------------------------------
Two independent tables hold an alumnus out of a campaign, and clearing only one
leaves them just as stuck — which is exactly the trap that sent people back to
SQL:

* ``survey_send_log`` — UNIQUE on ``(graduation_year, alumni_id, stage,
  cycle_seq)``. A row means "we already emailed them at this stage of this
  campaign", so the sender skips them. See
  :class:`app.models.survey_schedule.SurveySendLog`.
* ``survey_responses`` — the 365-day re-survey window. A ``pending`` or
  ``applied`` row inside the window makes them "already replied" and excludes
  them from the send (:data:`app.services.survey_email.RESPONDED_STATUSES`, via
  ``_replied_recently_exists``).

The reset therefore deletes from BOTH, for that one ``alumni_id`` and nothing
else. Deliberately NOT scoped to a year or a cycle: the operator's question is
"make this person surveyable again", and a leftover row from any campaign can
answer it wrongly. There is no bulk or cohort form of this function and there
must never be one — re-running a whole cohort is `start_new_cycle`, which
advances the cycle and deletes nothing.

RESPONSES ARE DELETED AT EVERY STATUS, INCLUDING ``rejected``
------------------------------------------------------------
``rejected`` rows do not block a send (staff threw that submission away, so the
alum stays surveyable) — but a reset that left them behind would not be a reset:
the profile's Surveys tab derives from these rows
(``profile._derive_survey_history``), so a "reset" person would still show the
old history. The state view below reports which rows are actually blocking, so
the engineer can see the difference before committing.

THIS DESTROYS SUBMITTED ANSWERS
-------------------------------
Deleting a ``pending`` response throws away an alum's submission that nobody has
reviewed yet — it cannot be recovered and it is not written to the record first.
That is the whole reason :func:`get_state` exists: someone can look blocked
simply because they legitimately answered three months ago, and the right move
is then to leave them alone, not to delete a real reply.

NOTHING ELSE IS PER-ALUMNUS SURVEY STATE
----------------------------------------
The survey link is a **stateless signed token** (``survey_email.make_survey_token``
/ ``verify_survey_token``) — an HMAC over the alumni id and an expiry, with no
row anywhere — so there is no token table to clear and a fresh send simply mints
a new link. ``survey_schedule`` is per graduation YEAR, not per person, and must
not be touched by a single-alumnus reset. The only other per-alumnus artefact is
a staged survey photo in the ``headshots`` bucket under ``survey-pending/<id>``,
which is deleted here alongside its row so a reset never orphans an image.
"""

from __future__ import annotations

import datetime

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import NotFoundError
from app.models.alumni import Alumni
from app.models.audit import AuditLog
from app.models.contact import AlumniContactInfo
from app.models.survey_response import SurveyResponse
from app.models.survey_schedule import SurveySchedule, SurveySendLog
from app.schemas.survey import (
    SurveyAlumniResponse,
    SurveyAlumniSend,
    SurveyAlumniState,
    SurveyResetResult,
)
from app.services import supabase_storage, survey_email, survey_responses

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

    Never empty: the confirm step must be able to NAME the person whose answers
    are about to be deleted, so this falls back to the id rather than to a blank.
    """
    full = " ".join(p for p in (alum.first_name, alum.last_name) if p).strip()
    return full or (alum.preferred_first_name or "").strip() or f"Alum #{alum.alumni_id}"


async def _load_alum(session: AsyncSession, alumni_id: int) -> Alumni:
    alum = (
        await session.execute(select(Alumni).where(Alumni.alumni_id == alumni_id))
    ).scalar_one_or_none()
    if alum is None:
        raise NotFoundError("Alum not found.")
    return alum


async def get_state(session: AsyncSession, alumni_id: int) -> SurveyAlumniState:
    """This alumnus's full survey state — what was sent, what came back, and
    what (if anything) is holding them out of the next send.

    Read-only. Shown BEFORE the reset button is armed so the engineer can decide
    whether a reset is warranted at all; ``blocked_reasons`` empty means it is
    not, because there is nothing to unblock and the only effect would be
    deleting history.
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
            current_cycle=(
                s.cycle_seq == current_cycle
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
    responses = [
        SurveyAlumniResponse(
            survey_response_id=r.survey_response_id,
            submitted_at=r.submitted_at,
            status=r.status,
            field_count=len(r.payload or {}),
            has_photo=bool(r.staged_photo_path),
            # The exact predicate the send exclusion applies, not an
            # approximation of it — so what this screen calls "blocking" is what
            # actually blocks.
            blocks_resend=(
                _as_utc(r.submitted_at) >= cutoff
                and r.status in survey_email.RESPONDED_STATUSES
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
    current_sends = sorted(
        {s.stage for s in sends if s.current_cycle}
    )
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
        blocked_reasons=reasons,
    )


async def reset_alumnus(
    session: AsyncSession, alumni_id: int, *, actor_user_id: int | None
) -> SurveyResetResult:
    """Clear ONE alumnus's survey campaign state so they can be surveyed again.

    Deletes every ``survey_send_log`` row and every ``survey_responses`` row for
    this alumni_id — both tables, because either one alone still blocks them —
    and removes any staged survey photo from storage so no image is orphaned.
    Nothing outside this alumnus is touched: no schedule, no other person, no
    cohort.

    IRREVERSIBLE. Submitted answers are deleted outright, including a ``pending``
    one that nobody has reviewed; there is no undo and nothing is written to the
    record on the way out. Callers must have shown :func:`get_state` first.

    Audited as ``reset_survey_campaign`` against the alumnus, carrying the counts
    removed. The actor is an engineer, so the audit layer reroutes the row into
    the append-only ``engineer_action_log`` (#199) — the trail exists either way.
    """
    alum = await _load_alum(session, alumni_id)
    name = _name(alum)

    # Staged photos first: they live in object storage, which is not part of the
    # transaction. Deleting them BEFORE the rows means a storage failure aborts
    # with the rows still present (retryable) rather than after the only pointers
    # to those objects are gone (an unfindable orphan).
    staged_paths = [
        p
        for p in (
            await session.scalars(
                select(SurveyResponse.staged_photo_path).where(
                    SurveyResponse.alumni_id == alumni_id,
                    SurveyResponse.staged_photo_path.is_not(None),
                )
            )
        ).all()
        if p
    ]
    for path in staged_paths:
        await supabase_storage.delete_object(survey_responses._HEADSHOT_BUCKET, path)

    # EVERY status, including `rejected` — see the module docstring.
    responses_deleted = int(
        (
            await session.execute(
                delete(SurveyResponse).where(SurveyResponse.alumni_id == alumni_id)
            )
        ).rowcount
        or 0
    )
    # Every year and every cycle for this alumnus. Scoping to the current cycle
    # would leave a stale row able to block a later campaign for the same reason
    # the SQL was being run by hand in the first place.
    sends_deleted = int(
        (
            await session.execute(
                delete(SurveySendLog).where(SurveySendLog.alumni_id == alumni_id)
            )
        ).rowcount
        or 0
    )

    session.add(
        AuditLog(
            user_id=actor_user_id,
            action_type="reset_survey_campaign",
            entity_type="alumni",
            entity_id=alumni_id,
            field_name="survey_campaign",
            old_value=(
                f"sends={sends_deleted} responses={responses_deleted} "
                f"staged_photos={len(staged_paths)}"
            ),
            new_value="cleared",
        )
    )
    await session.commit()

    return SurveyResetResult(
        alumni_id=alumni_id,
        name=name,
        sends_deleted=sends_deleted,
        responses_deleted=responses_deleted,
        staged_photos_deleted=len(staged_paths),
    )
