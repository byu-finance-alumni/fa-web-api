"""Alumni profile aggregation.

Assembles the read-only ``ProfileRead`` payload for one alumni: the core record
plus every related collection the profile tabs render. One call per related
table; for a single alumni the row counts are tiny, so this stays well within
the dashboard/profile performance budget.
"""

from __future__ import annotations

import datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import NotFoundError
from app.models.alumni import Alumni
from app.models.audit import AuditLog
from app.models.contact import AlumniContactInfo
from app.models.crm import Attachment, FollowUpTask, Interaction, Survey
from app.models.employment import (
    CurrentEmployment,
    EducationHistory,
    EmploymentHistory,
)
from app.models.engagement import (
    AlumniEngagement,
    AlumniProgramEngagement,
    FinanceSocietyLeadership,
)
from app.models.event import Event, EventAttendance
from app.models.tags import AlumniStatusLabel, AlumniTag, StatusLabel, Tag
from app.models.user import User
from app.schemas.alumni import AlumniRead
from app.schemas.profile import (
    AttachmentRead,
    AuditEntryRead,
    ContactRead,
    CurrentCareerRead,
    EducationRead,
    EmploymentHistoryRead,
    EngagementNoteRead,
    EventAttendedRead,
    InteractionCreate,
    InteractionRead,
    LeadershipRead,
    ProfileRead,
    ProgramEngagementRead,
    SurveyRead,
    TaskCreate,
    TaskRead,
)


def _now() -> datetime.datetime:
    return datetime.datetime.now(datetime.UTC)


def _full_name(first: str | None, last: str | None, email: str | None) -> str | None:
    name = " ".join(p for p in (first, last) if p).strip()
    return name or email


async def _require_alumni(session: AsyncSession, alumni_id: int) -> Alumni:
    alumnus = await session.get(Alumni, alumni_id)
    if alumnus is None:
        raise NotFoundError(f"Alumni {alumni_id} not found.")
    return alumnus


async def _actor_name(session: AsyncSession, user_id: int | None) -> str | None:
    if user_id is None:
        return None
    user = await session.get(User, user_id)
    return _full_name(user.first_name, user.last_name, user.email) if user else None


async def get_profile(session: AsyncSession, alumni_id: int) -> ProfileRead:
    alumnus = await session.get(Alumni, alumni_id)
    if alumnus is None:
        raise NotFoundError(f"Alumni {alumni_id} not found.")

    contact = await session.scalar(
        select(AlumniContactInfo)
        .where(AlumniContactInfo.alumni_id == alumni_id)
        .order_by(AlumniContactInfo.contact_info_id)
        .limit(1)
    )
    career = await session.scalar(
        select(CurrentEmployment)
        .where(CurrentEmployment.alumni_id == alumni_id)
        .order_by(CurrentEmployment.current_employment_id.desc())
        .limit(1)
    )
    employment = (
        await session.scalars(
            select(EmploymentHistory)
            .where(EmploymentHistory.alumni_id == alumni_id)
            .order_by(
                EmploymentHistory.is_current.desc(),
                EmploymentHistory.end_year.desc().nullsfirst(),
                EmploymentHistory.start_year.desc().nullslast(),
            )
        )
    ).all()
    education = (
        await session.scalars(
            select(EducationHistory)
            .where(EducationHistory.alumni_id == alumni_id)
            .order_by(EducationHistory.degree_year.desc().nullslast())
        )
    ).all()
    leadership = (
        await session.scalars(
            select(FinanceSocietyLeadership)
            .where(FinanceSocietyLeadership.alumni_id == alumni_id)
            .order_by(FinanceSocietyLeadership.role_year.desc().nullslast())
        )
    ).all()
    program = await session.scalar(
        select(AlumniProgramEngagement).where(
            AlumniProgramEngagement.alumni_id == alumni_id
        )
    )
    engagement_notes = (
        await session.scalars(
            select(AlumniEngagement)
            .where(AlumniEngagement.alumni_id == alumni_id)
            .order_by(AlumniEngagement.engagement_id)
        )
    ).all()
    tags = (
        await session.scalars(
            select(Tag.tag_name)
            .join(AlumniTag, AlumniTag.tag_id == Tag.tag_id)
            .where(AlumniTag.alumni_id == alumni_id)
            .order_by(Tag.tag_name)
        )
    ).all()
    status_labels = (
        await session.scalars(
            select(StatusLabel.status_label_name)
            .join(
                AlumniStatusLabel,
                AlumniStatusLabel.status_label_id == StatusLabel.status_label_id,
            )
            .where(AlumniStatusLabel.alumni_id == alumni_id)
            .order_by(StatusLabel.status_label_name)
        )
    ).all()
    surveys = (
        await session.scalars(
            select(Survey)
            .where(Survey.alumni_id == alumni_id)
            .order_by(Survey.survey_year.desc().nullslast())
        )
    ).all()
    # Cap the returned list to the 50 most recent; expose the true total
    # separately so the UI can show an accurate count without a huge payload.
    interaction_count = await session.scalar(
        select(func.count())
        .select_from(Interaction)
        .where(Interaction.alumni_id == alumni_id)
    )
    interactions = (
        await session.scalars(
            select(Interaction)
            .where(Interaction.alumni_id == alumni_id)
            .order_by(Interaction.interaction_date_time.desc().nullslast())
            .limit(50)
        )
    ).all()
    tasks = (
        await session.scalars(
            select(FollowUpTask)
            .where(FollowUpTask.alumni_id == alumni_id)
            .order_by(
                FollowUpTask.completed.asc(),
                FollowUpTask.due_date.asc().nullslast(),
            )
        )
    ).all()
    attachments = (
        await session.scalars(
            select(Attachment)
            .where(Attachment.alumni_id == alumni_id)
            .order_by(Attachment.uploaded_at.desc())
        )
    ).all()
    event_rows = (
        await session.execute(
            select(Event, EventAttendance.attendance_status)
            .join(EventAttendance, EventAttendance.event_id == Event.event_id)
            .where(EventAttendance.alumni_id == alumni_id)
            .order_by(Event.event_date.desc().nullslast())
        )
    ).all()
    audit = (
        await session.scalars(
            select(AuditLog)
            .where(AuditLog.entity_type == "alumni", AuditLog.entity_id == alumni_id)
            .order_by(AuditLog.created_at.desc())
            .limit(50)
        )
    ).all()

    # Resolve user display names for interactions/tasks in one lookup.
    user_ids = {i.user_id for i in interactions if i.user_id} | {
        t.assigned_to_user_id for t in tasks if t.assigned_to_user_id
    }
    names: dict[int, str | None] = {}
    if user_ids:
        for u in (
            await session.scalars(select(User).where(User.user_id.in_(user_ids)))
        ).all():
            names[u.user_id] = _full_name(u.first_name, u.last_name, u.email)

    return ProfileRead(
        alumni=AlumniRead.model_validate(alumnus),
        contact=ContactRead.model_validate(contact) if contact else None,
        current_career=CurrentCareerRead.model_validate(career) if career else None,
        employment_history=[EmploymentHistoryRead.model_validate(e) for e in employment],
        education=[EducationRead.model_validate(e) for e in education],
        leadership=[LeadershipRead.model_validate(le) for le in leadership],
        program_engagement=(
            ProgramEngagementRead.model_validate(program) if program else None
        ),
        engagement_notes=[
            EngagementNoteRead.model_validate(en) for en in engagement_notes
        ],
        tags=list(tags),
        status_labels=list(status_labels),
        surveys=[SurveyRead.model_validate(s) for s in surveys],
        interactions=[
            InteractionRead.model_validate(i).model_copy(
                update={"logged_by": names.get(i.user_id) if i.user_id else None}
            )
            for i in interactions
        ],
        interaction_count=int(interaction_count or 0),
        tasks=[
            TaskRead.model_validate(t).model_copy(
                update={
                    "assigned_to": (
                        names.get(t.assigned_to_user_id)
                        if t.assigned_to_user_id
                        else None
                    )
                }
            )
            for t in tasks
        ],
        attachments=[AttachmentRead.model_validate(a) for a in attachments],
        events=[
            EventAttendedRead.model_validate(ev).model_copy(
                update={"attendance_status": status}
            )
            for ev, status in event_rows
        ],
        audit=[AuditEntryRead.model_validate(a) for a in audit],
    )


def _audit_alumni(
    session: AsyncSession, actor_user_id: int | None, action: str, alumni_id: int
) -> None:
    """Record a profile-activity audit event against the alumni entity, so it
    surfaces in the profile Audit tab. No-op when the actor is unknown."""
    if actor_user_id is not None:
        session.add(
            AuditLog(
                user_id=actor_user_id,
                action_type=action,
                entity_type="alumni",
                entity_id=alumni_id,
            )
        )


async def add_interaction(
    session: AsyncSession,
    alumni_id: int,
    payload: InteractionCreate,
    actor_user_id: int | None,
) -> InteractionRead:
    await _require_alumni(session, alumni_id)
    interaction = Interaction(
        alumni_id=alumni_id,
        user_id=actor_user_id,
        interaction_type=payload.interaction_type,
        interaction_date_time=payload.interaction_date_time or _now(),
        interaction_notes=payload.interaction_notes,
    )
    session.add(interaction)
    _audit_alumni(session, actor_user_id, "add_interaction", alumni_id)
    await session.commit()
    await session.refresh(interaction)
    return InteractionRead.model_validate(interaction).model_copy(
        update={"logged_by": await _actor_name(session, interaction.user_id)}
    )


async def add_task(
    session: AsyncSession,
    alumni_id: int,
    payload: TaskCreate,
    actor_user_id: int | None,
) -> TaskRead:
    await _require_alumni(session, alumni_id)
    task = FollowUpTask(
        alumni_id=alumni_id,
        assigned_to_user_id=actor_user_id,
        task_title=payload.task_title,
        due_date=payload.due_date,
        task_notes=payload.task_notes,
    )
    session.add(task)
    _audit_alumni(session, actor_user_id, "add_task", alumni_id)
    await session.commit()
    await session.refresh(task)
    return TaskRead.model_validate(task).model_copy(
        update={"assigned_to": await _actor_name(session, task.assigned_to_user_id)}
    )


async def set_task_completed(
    session: AsyncSession,
    alumni_id: int,
    task_id: int,
    completed: bool,
    actor_user_id: int | None,
) -> TaskRead:
    task = await session.get(FollowUpTask, task_id)
    if task is None or task.alumni_id != alumni_id:
        raise NotFoundError(f"Task {task_id} not found.")
    if task.completed != completed:
        task.completed = completed
        task.completed_at = _now() if completed else None
        _audit_alumni(
            session,
            actor_user_id,
            "complete_task" if completed else "reopen_task",
            alumni_id,
        )
        await session.commit()
        await session.refresh(task)
    return TaskRead.model_validate(task).model_copy(
        update={"assigned_to": await _actor_name(session, task.assigned_to_user_id)}
    )
