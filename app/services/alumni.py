"""Alumni business logic.

Owns the rules that aren't just data access:
  * soft-delete — ``archived`` is flipped, rows are never hard-deleted
  * manual-edit provenance — any client edit stamps ``manually_edited_at`` so
    later imports won't clobber it (manual edits win)
"""

import datetime

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import NotFoundError
from app.models.alumni import Alumni
from app.repositories import alumni as repo
from app.schemas.alumni import AlumniCreate, AlumniUpdate


def _now() -> datetime.datetime:
    return datetime.datetime.now(datetime.UTC)


async def get_alumni(session: AsyncSession, alumni_id: int) -> Alumni:
    alumnus = await repo.get(session, alumni_id)
    if alumnus is None:
        raise NotFoundError(f"Alumni {alumni_id} not found.")
    return alumnus


async def list_alumni(
    session: AsyncSession,
    *,
    limit: int,
    offset: int,
    include_archived: bool = False,
) -> tuple[list[Alumni], int]:
    return await repo.list_page(
        session, limit=limit, offset=offset, include_archived=include_archived
    )


async def create_alumni(session: AsyncSession, payload: AlumniCreate) -> Alumni:
    alumnus = Alumni(**payload.model_dump(exclude_unset=True))
    session.add(alumnus)
    await session.commit()
    await session.refresh(alumnus)
    return alumnus


async def update_alumni(
    session: AsyncSession, alumni_id: int, payload: AlumniUpdate
) -> Alumni:
    alumnus = await get_alumni(session, alumni_id)
    changes = payload.model_dump(exclude_unset=True)
    for field, value in changes.items():
        setattr(alumnus, field, value)
    if changes:
        alumnus.manually_edited_at = _now()
        await session.commit()
        await session.refresh(alumnus)
    return alumnus


async def archive_alumni(session: AsyncSession, alumni_id: int) -> Alumni:
    """Soft-delete: flag the record archived. Idempotent."""
    alumnus = await get_alumni(session, alumni_id)
    if not alumnus.archived:
        alumnus.archived = True
        alumnus.manually_edited_at = _now()
        await session.commit()
        await session.refresh(alumnus)
    return alumnus


async def restore_alumni(session: AsyncSession, alumni_id: int) -> Alumni:
    """Reverse a soft-delete. Idempotent."""
    alumnus = await get_alumni(session, alumni_id)
    if alumnus.archived:
        alumnus.archived = False
        alumnus.manually_edited_at = _now()
        await session.commit()
        await session.refresh(alumnus)
    return alumnus
