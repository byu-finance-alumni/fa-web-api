"""Unified-notes routes.

Notes attach to one of three levels — an alumni profile, an interaction, or an
event — addressed as ``(entity_type, entity_id)``. Reads require any view-access
role (notes are visible to everyone, including ``view_only`` / Professor).

Writes take one of two capabilities (2026-11-03):

* ``notes.manage`` (#379) — add, edit, and delete ANY note at any level. Seeded
  to super_admin + full_access (the roles that held ``alumni.full``).
* ``notes.create`` — add a note on an ALUMNI PROFILE, and edit or delete only
  the notes the caller wrote. Seeded to student (Finance Society leaders, so one
  club can see another already reached out) and to every ``notes.manage`` role.

The route guard admits either; the finer rule (whose note, which level) is
decided per request from the LIVE permission config and enforced in the service
— 403 on someone else's note, after the 404 existence check. ``view_only`` holds
neither and stays read-only. All enforcement is server-side; the UI gate is not
relied on. Every list / write response carries ``can_edit`` so the UI shows
edit / delete only where the server would allow it.
"""

from typing import Annotated

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies.auth import PermissionConfig, RequireNotesWrite
from app.api.params import IdPath
from app.core.capabilities import Capability, effective_capabilities
from app.core.database import get_session
from app.core.rate_limit import BrowseReadRateLimit
from app.schemas.auth import UserContext
from app.schemas.note import NoteCreate, NoteEntityType, NoteRead, NoteUpdate
from app.services import notes as service

router = APIRouter(prefix="/notes", tags=["notes"])

SessionDep = Annotated[AsyncSession, Depends(get_session)]


def _note_rights(config: dict[str, frozenset[str]], user: UserContext) -> service.NoteRights:
    """What this caller may do with notes, from the LIVE permission config (so an
    engineer's edits in the permission editor take effect), not a role list.
    ``notes.manage`` implies everything ``notes.create`` allows."""
    held = effective_capabilities(config, user.roles)
    can_manage = Capability.NOTES_MANAGE in held
    return service.NoteRights(
        actor_user_id=user.user_id,
        can_manage=can_manage,
        can_create=can_manage or Capability.NOTES_CREATE in held,
        include_archived=user.sees_archived,
    )


@router.get("", response_model=list[NoteRead])
async def list_notes(
    user: BrowseReadRateLimit,
    config: PermissionConfig,
    session: SessionDep,
    entity_type: Annotated[
        NoteEntityType,
        Query(description="Which level the notes are attached to."),
    ],
    entity_id: Annotated[int, Query(gt=0, description="Id of the alumni / interaction / event.")],
) -> list[NoteRead]:
    """List the notes on one entity, newest first (any view-access role). 404 if
    the parent entity doesn't exist. The disclosure is audit-logged. A view_only
    caller sees note authors by first name only; editors see full names.

    Notes on an ARCHIVED alumnus (or one of their interactions) 404 below
    full_access, like the profile itself (#591)."""
    return await service.list_notes(
        session,
        entity_type,
        entity_id,
        actor_user_id=user.user_id,
        full_author_name=user.can_edit_alumni,
        include_archived=user.sees_archived,
        rights=_note_rights(config, user),
    )


@router.post("", response_model=NoteRead, status_code=status.HTTP_201_CREATED)
async def create_note(
    payload: NoteCreate,
    user: RequireNotesWrite,
    config: PermissionConfig,
    session: SessionDep,
) -> NoteRead:
    """Create a note. ``notes.manage`` may attach it to an alumni, interaction,
    or event; ``notes.create`` alone may attach it to an alumni profile only
    (403 otherwise). 404 if the target doesn't exist — or is an archived alumnus
    the caller cannot see."""
    rights = _note_rights(config, user)
    return await service.create_note(
        session, payload, actor_user_id=user.user_id, rights=rights
    )


@router.patch("/{note_id}", response_model=NoteRead)
async def update_note(
    note_id: IdPath,
    payload: NoteUpdate,
    user: RequireNotesWrite,
    config: PermissionConfig,
    session: SessionDep,
) -> NoteRead:
    """Edit a note's body. ``notes.manage`` may edit any note; ``notes.create``
    alone may edit only a note the caller wrote (403 otherwise). 404 if the note
    doesn't exist."""
    rights = _note_rights(config, user)
    return await service.update_note(
        session, note_id, payload, actor_user_id=user.user_id, rights=rights
    )


@router.delete("/{note_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_note(
    note_id: IdPath,
    user: RequireNotesWrite,
    config: PermissionConfig,
    session: SessionDep,
) -> None:
    """Delete a note. ``notes.manage`` may delete any note; ``notes.create``
    alone may delete only a note the caller wrote (403 otherwise). 404 if the
    note doesn't exist. The body is snapshotted to the audit trail before
    removal."""
    rights = _note_rights(config, user)
    await service.delete_note(session, note_id, actor_user_id=user.user_id, rights=rights)
