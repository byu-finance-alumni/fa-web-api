"""Alumni CRUD routes.

Reads require view access (either role); writes require full_access. ``DELETE``
is a soft-delete (archive), never a hard delete — audit history depends on
retained records.
"""

from typing import Annotated

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies.auth import RequireFullAccess, RequireViewAccess
from app.core.database import get_session
from app.schemas.alumni import (
    AlumniCreateFull,
    AlumniPage,
    AlumniRead,
    AlumniUpdateFull,
)
from app.schemas.profile import (
    EmploymentHistoryCreate,
    EmploymentHistoryRead,
    EventAttendanceCreate,
    EventAttendedRead,
    InteractionCreate,
    InteractionRead,
    ProfileRead,
    StatusLabelCreate,
    TagCreate,
    TaskCompleteUpdate,
    TaskCreate,
    TaskRead,
)
from app.services import alumni as service
from app.services import profile as profile_service

router = APIRouter(prefix="/alumni", tags=["alumni"])

SessionDep = Annotated[AsyncSession, Depends(get_session)]


@router.get("", response_model=AlumniPage)
async def list_alumni(
    _: RequireViewAccess,
    session: SessionDep,
    q: Annotated[
        str | None,
        Query(description="Search names and external ids (case-insensitive)."),
    ] = None,
    graduation_year: int | None = None,
    grad_year_min: int | None = None,
    grad_year_max: int | None = None,
    deceased: Annotated[
        bool | None, Query(description="Filter by deceased flag.")
    ] = None,
    employer: Annotated[
        str | None,
        Query(description="Current employer (case-insensitive exact match)."),
    ] = None,
    industry: Annotated[
        str | None,
        Query(
            description=(
                "Current industry / work area, primary or secondary "
                "(case-insensitive exact match)."
            )
        ),
    ] = None,
    attended_event: Annotated[
        bool, Query(description="Only alumni who attended at least one event.")
    ] = False,
    donor: Annotated[
        bool, Query(description="Only PIFF donors.")
    ] = False,
    mentor_willing: Annotated[
        bool, Query(description="Only alumni willing to mentor.")
    ] = False,
    guest_speaker_willing: Annotated[
        bool, Query(description="Only alumni willing to guest speak.")
    ] = False,
    missing_email: Annotated[
        bool,
        Query(description="Only alumni with no contact-info email on file."),
    ] = False,
    missing_employer: Annotated[
        bool,
        Query(description="Only alumni with no current employer on file."),
    ] = False,
    duplicate: Annotated[
        bool,
        Query(description="Only alumni flagged as duplicate candidates."),
    ] = False,
    include_archived: bool = False,
    sort: Annotated[
        str,
        Query(description="Sort order: name | grad_desc | grad_asc."),
    ] = "name",
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> AlumniPage:
    items, total = await service.list_alumni(
        session,
        limit=limit,
        offset=offset,
        q=q,
        graduation_year=graduation_year,
        grad_year_min=grad_year_min,
        grad_year_max=grad_year_max,
        deceased=deceased,
        employer=employer,
        industry=industry,
        attended_event=attended_event,
        donor=donor,
        mentor_willing=mentor_willing,
        guest_speaker_willing=guest_speaker_willing,
        missing_email=missing_email,
        missing_employer=missing_employer,
        duplicate=duplicate,
        include_archived=include_archived,
        sort=sort,
    )
    return AlumniPage(items=items, total=total, limit=limit, offset=offset)


@router.get("/{alumni_id}", response_model=AlumniRead)
async def get_alumni(
    alumni_id: int, _: RequireViewAccess, session: SessionDep
) -> AlumniRead:
    return await service.get_alumni(session, alumni_id)


@router.get("/{alumni_id}/profile", response_model=ProfileRead)
async def get_alumni_profile(
    alumni_id: int, _: RequireViewAccess, session: SessionDep
) -> ProfileRead:
    """Full profile aggregate (core + contact, career, employment, leadership,
    engagement, surveys, interactions, tasks, attachments, audit) for the tabs."""
    return await profile_service.get_profile(session, alumni_id)


@router.post(
    "/{alumni_id}/interactions",
    response_model=InteractionRead,
    status_code=status.HTTP_201_CREATED,
)
async def add_interaction(
    alumni_id: int,
    payload: InteractionCreate,
    user: RequireFullAccess,
    session: SessionDep,
) -> InteractionRead:
    """Log an interaction on an alumni's timeline (full_access)."""
    return await profile_service.add_interaction(
        session, alumni_id, payload, actor_user_id=user.user_id
    )


@router.post(
    "/{alumni_id}/tasks",
    response_model=TaskRead,
    status_code=status.HTTP_201_CREATED,
)
async def add_task(
    alumni_id: int,
    payload: TaskCreate,
    user: RequireFullAccess,
    session: SessionDep,
) -> TaskRead:
    """Create a follow-up task for an alumni (full_access)."""
    return await profile_service.add_task(
        session, alumni_id, payload, actor_user_id=user.user_id
    )


@router.patch("/{alumni_id}/tasks/{task_id}", response_model=TaskRead)
async def update_task_completion(
    alumni_id: int,
    task_id: int,
    payload: TaskCompleteUpdate,
    user: RequireFullAccess,
    session: SessionDep,
) -> TaskRead:
    """Toggle a follow-up task's completion state (full_access)."""
    return await profile_service.set_task_completed(
        session, alumni_id, task_id, payload.completed, actor_user_id=user.user_id
    )


@router.post(
    "/{alumni_id}/employment",
    response_model=EmploymentHistoryRead,
    status_code=status.HTTP_201_CREATED,
)
async def add_employment(
    alumni_id: int,
    payload: EmploymentHistoryCreate,
    user: RequireFullAccess,
    session: SessionDep,
) -> EmploymentHistoryRead:
    """Add a prior role to an alumni's employment history (full_access)."""
    return await profile_service.add_employment(
        session, alumni_id, payload, actor_user_id=user.user_id
    )


@router.post(
    "/{alumni_id}/tags",
    response_model=list[str],
    status_code=status.HTTP_201_CREATED,
)
async def add_tag(
    alumni_id: int,
    payload: TagCreate,
    user: RequireFullAccess,
    session: SessionDep,
) -> list[str]:
    """Attach a canonical engagement tag to an alumni (full_access). Returns the
    resulting tag list. Idempotent."""
    return await profile_service.add_tag(
        session, alumni_id, payload, actor_user_id=user.user_id
    )


@router.delete("/{alumni_id}/tags/{tag}", response_model=list[str])
async def remove_tag(
    alumni_id: int,
    tag: str,
    user: RequireFullAccess,
    session: SessionDep,
) -> list[str]:
    """Detach a tag from an alumni (full_access). Returns the resulting tag list.
    404 if the alumni doesn't have that tag."""
    return await profile_service.remove_tag(
        session, alumni_id, tag, actor_user_id=user.user_id
    )


@router.post(
    "/{alumni_id}/status-labels",
    response_model=list[str],
    status_code=status.HTTP_201_CREATED,
)
async def add_status_label(
    alumni_id: int,
    payload: StatusLabelCreate,
    user: RequireFullAccess,
    session: SessionDep,
) -> list[str]:
    """Attach a canonical status label to an alumni (full_access). Returns the
    resulting label list. Idempotent."""
    return await profile_service.add_status_label(
        session, alumni_id, payload, actor_user_id=user.user_id
    )


@router.delete("/{alumni_id}/status-labels/{label}", response_model=list[str])
async def remove_status_label(
    alumni_id: int,
    label: str,
    user: RequireFullAccess,
    session: SessionDep,
) -> list[str]:
    """Detach a status label from an alumni (full_access). Returns the resulting
    label list. 404 if the alumni doesn't have that label."""
    return await profile_service.remove_status_label(
        session, alumni_id, label, actor_user_id=user.user_id
    )


@router.post(
    "/{alumni_id}/events",
    response_model=EventAttendedRead,
    status_code=status.HTTP_201_CREATED,
)
async def add_event_attendance(
    alumni_id: int,
    payload: EventAttendanceCreate,
    user: RequireFullAccess,
    session: SessionDep,
) -> EventAttendedRead:
    """Mark an alumni as an attendee of an existing event (full_access). 404 if
    the event/alumni is unknown; 409 if attendance already exists."""
    return await profile_service.add_event_attendance(
        session, alumni_id, payload, actor_user_id=user.user_id
    )


@router.post("", response_model=AlumniRead, status_code=status.HTTP_201_CREATED)
async def create_alumni(
    payload: AlumniCreateFull, user: RequireFullAccess, session: SessionDep
) -> AlumniRead:
    return await service.create_alumni(session, payload, actor_user_id=user.user_id)


@router.patch("/{alumni_id}", response_model=AlumniRead)
async def update_alumni(
    alumni_id: int,
    payload: AlumniUpdateFull,
    user: RequireFullAccess,
    session: SessionDep,
) -> AlumniRead:
    return await service.update_alumni(
        session, alumni_id, payload, actor_user_id=user.user_id
    )


@router.delete("/{alumni_id}", response_model=AlumniRead)
async def archive_alumni(
    alumni_id: int, user: RequireFullAccess, session: SessionDep
) -> AlumniRead:
    """Soft-delete (archive) an alumni record."""
    return await service.archive_alumni(
        session, alumni_id, actor_user_id=user.user_id
    )


@router.post("/{alumni_id}/restore", response_model=AlumniRead)
async def restore_alumni(
    alumni_id: int, user: RequireFullAccess, session: SessionDep
) -> AlumniRead:
    """Restore (unarchive) a previously archived alumni record."""
    return await service.restore_alumni(
        session, alumni_id, actor_user_id=user.user_id
    )
