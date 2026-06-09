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

from app.core.errors import ConflictError, NotFoundError
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
    EmploymentHistoryCreate,
    EmploymentHistoryRead,
    EngagementNoteRead,
    EventAttendanceCreate,
    EventAttendedRead,
    InteractionCreate,
    InteractionRead,
    LeadershipRead,
    ProfileRead,
    ProgramEngagementRead,
    StatusLabelCreate,
    SurveyRead,
    TagCreate,
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


async def add_employment(
    session: AsyncSession,
    alumni_id: int,
    payload: EmploymentHistoryCreate,
    actor_user_id: int | None,
) -> EmploymentHistoryRead:
    """Insert a prior role into an alumnus's employment history (full_access)."""
    await _require_alumni(session, alumni_id)
    row = EmploymentHistory(
        alumni_id=alumni_id,
        employer_name=payload.employer_name,
        employment_title=payload.employment_title,
        employment_industry=payload.employment_industry,
        city=payload.city,
        state=payload.state,
        start_year=payload.start_year,
        end_year=payload.end_year,
        is_current=payload.is_current,
    )
    session.add(row)
    _audit_alumni(session, actor_user_id, "add_employment", alumni_id)
    await session.commit()
    await session.refresh(row)
    return EmploymentHistoryRead.model_validate(row)


async def _tag_names(session: AsyncSession, alumni_id: int) -> list[str]:
    return list(
        (
            await session.scalars(
                select(Tag.tag_name)
                .join(AlumniTag, AlumniTag.tag_id == Tag.tag_id)
                .where(AlumniTag.alumni_id == alumni_id)
                .order_by(Tag.tag_name)
            )
        ).all()
    )


async def _get_or_create_tag(session: AsyncSession, name: str) -> Tag:
    tag = await session.scalar(select(Tag).where(Tag.tag_name == name))
    if tag is None:
        tag = Tag(tag_name=name)
        session.add(tag)
        await session.flush()
    return tag


async def add_tag(
    session: AsyncSession,
    alumni_id: int,
    payload: TagCreate,
    actor_user_id: int | None,
) -> list[str]:
    """Attach a canonical tag to an alumnus; return the resulting tag list.

    Idempotent: re-adding an existing tag is a no-op (existence-checked against
    the unique constraint), never a 500."""
    await _require_alumni(session, alumni_id)
    tag = await _get_or_create_tag(session, payload.tag)
    existing = await session.scalar(
        select(AlumniTag.alumni_tag_id).where(
            AlumniTag.alumni_id == alumni_id, AlumniTag.tag_id == tag.tag_id
        )
    )
    if existing is None:
        session.add(AlumniTag(alumni_id=alumni_id, tag_id=tag.tag_id))
        _audit_alumni(session, actor_user_id, "add_tag", alumni_id)
        await session.commit()
    return await _tag_names(session, alumni_id)


async def remove_tag(
    session: AsyncSession,
    alumni_id: int,
    tag_name: str,
    actor_user_id: int | None,
) -> list[str]:
    """Detach a tag from an alumnus; return the resulting tag list. 404 if the
    alumnus doesn't have that tag."""
    await _require_alumni(session, alumni_id)
    assoc = await session.scalar(
        select(AlumniTag)
        .join(Tag, Tag.tag_id == AlumniTag.tag_id)
        .where(AlumniTag.alumni_id == alumni_id, Tag.tag_name == tag_name)
    )
    if assoc is None:
        raise NotFoundError(f"Tag '{tag_name}' is not set on alumni {alumni_id}.")
    await session.delete(assoc)
    _audit_alumni(session, actor_user_id, "remove_tag", alumni_id)
    await session.commit()
    return await _tag_names(session, alumni_id)


async def _status_label_names(session: AsyncSession, alumni_id: int) -> list[str]:
    return list(
        (
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
    )


async def _get_or_create_status_label(
    session: AsyncSession, name: str
) -> StatusLabel:
    label = await session.scalar(
        select(StatusLabel).where(StatusLabel.status_label_name == name)
    )
    if label is None:
        label = StatusLabel(status_label_name=name)
        session.add(label)
        await session.flush()
    return label


async def add_status_label(
    session: AsyncSession,
    alumni_id: int,
    payload: StatusLabelCreate,
    actor_user_id: int | None,
) -> list[str]:
    """Attach a canonical status label to an alumnus; return the resulting list.
    Idempotent (existence-checked)."""
    await _require_alumni(session, alumni_id)
    label = await _get_or_create_status_label(session, payload.label)
    existing = await session.scalar(
        select(AlumniStatusLabel.alumni_status_label_id).where(
            AlumniStatusLabel.alumni_id == alumni_id,
            AlumniStatusLabel.status_label_id == label.status_label_id,
        )
    )
    if existing is None:
        session.add(
            AlumniStatusLabel(
                alumni_id=alumni_id, status_label_id=label.status_label_id
            )
        )
        _audit_alumni(session, actor_user_id, "add_status_label", alumni_id)
        await session.commit()
    return await _status_label_names(session, alumni_id)


async def remove_status_label(
    session: AsyncSession,
    alumni_id: int,
    label_name: str,
    actor_user_id: int | None,
) -> list[str]:
    """Detach a status label from an alumnus; return the resulting list. 404 if
    the alumnus doesn't have that label."""
    await _require_alumni(session, alumni_id)
    assoc = await session.scalar(
        select(AlumniStatusLabel)
        .join(
            StatusLabel,
            StatusLabel.status_label_id == AlumniStatusLabel.status_label_id,
        )
        .where(
            AlumniStatusLabel.alumni_id == alumni_id,
            StatusLabel.status_label_name == label_name,
        )
    )
    if assoc is None:
        raise NotFoundError(
            f"Status label '{label_name}' is not set on alumni {alumni_id}."
        )
    await session.delete(assoc)
    _audit_alumni(session, actor_user_id, "remove_status_label", alumni_id)
    await session.commit()
    return await _status_label_names(session, alumni_id)


async def add_event_attendance(
    session: AsyncSession,
    alumni_id: int,
    payload: EventAttendanceCreate,
    actor_user_id: int | None,
) -> EventAttendedRead:
    """Mark an alumnus as an attendee of an existing event (full_access).

    404 if the event or alumnus is unknown. Respects the unique
    (event_id, alumni_id) constraint: a duplicate raises ConflictError (409),
    never a 500."""
    await _require_alumni(session, alumni_id)
    event = await session.get(Event, payload.event_id)
    if event is None:
        raise NotFoundError(f"Event {payload.event_id} not found.")

    existing = await session.scalar(
        select(EventAttendance).where(
            EventAttendance.event_id == payload.event_id,
            EventAttendance.alumni_id == alumni_id,
        )
    )
    if existing is not None:
        raise ConflictError(
            f"Alumni {alumni_id} is already an attendee of event "
            f"{payload.event_id}."
        )

    session.add(
        EventAttendance(
            event_id=payload.event_id,
            alumni_id=alumni_id,
            attendance_status=payload.attendance_status,
        )
    )
    _audit_alumni(session, actor_user_id, "add_event_attendance", alumni_id)
    await session.commit()
    return EventAttendedRead.model_validate(event).model_copy(
        update={"attendance_status": payload.attendance_status}
    )
