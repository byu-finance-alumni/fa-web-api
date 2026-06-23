"""Unified-notes routes.

Notes attach to one of three levels — an alumni profile, an interaction, or an
event — addressed as ``(entity_type, entity_id)``. Reads require any view-access
role (notes are visible to everyone, including ``view_only`` / Professor).
Writes (create / edit / delete) require ``full_access`` and up — ``student`` is
deliberately excluded, matching the unified-notes spec (write = super_admin +
full_access only). All enforcement is server-side; the UI gate is not relied on.
"""

from typing import Annotated

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies.auth import RequireFullAccess, RequireViewAccess
from app.core.database import get_session
from app.schemas.note import NoteCreate, NoteEntityType, NoteRead, NoteUpdate
from app.services import notes as service

router = APIRouter(prefix="/notes", tags=["notes"])

SessionDep = Annotated[AsyncSession, Depends(get_session)]


@router.get("", response_model=list[NoteRead])
async def list_notes(
    user: RequireViewAccess,
    session: SessionDep,
    entity_type: Annotated[
        NoteEntityType,
        Query(description="Which level the notes are attached to."),
    ],
    entity_id: Annotated[int, Query(gt=0, description="Id of the alumni / interaction / event.")],
) -> list[NoteRead]:
    """List the notes on one entity, newest first (any view-access role). 404 if
    the parent entity doesn't exist. The disclosure is audit-logged."""
    return await service.list_notes(session, entity_type, entity_id, actor_user_id=user.user_id)


@router.post("", response_model=NoteRead, status_code=status.HTTP_201_CREATED)
async def create_note(
    payload: NoteCreate,
    user: RequireFullAccess,
    session: SessionDep,
) -> NoteRead:
    """Create a note on an alumni / interaction / event (full_access). 404 if the
    target entity doesn't exist."""
    return await service.create_note(session, payload, actor_user_id=user.user_id)


@router.patch("/{note_id}", response_model=NoteRead)
async def update_note(
    note_id: int,
    payload: NoteUpdate,
    user: RequireFullAccess,
    session: SessionDep,
) -> NoteRead:
    """Edit a note's body (full_access). 404 if the note doesn't exist."""
    return await service.update_note(session, note_id, payload, actor_user_id=user.user_id)


@router.delete("/{note_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_note(
    note_id: int,
    user: RequireFullAccess,
    session: SessionDep,
) -> None:
    """Delete a note (full_access). 404 if the note doesn't exist. The body is
    snapshotted to the audit trail before removal."""
    await service.delete_note(session, note_id, actor_user_id=user.user_id)
