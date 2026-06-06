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
from app.models.audit import AuditLog
from app.repositories import alumni as repo
from app.schemas.alumni import AlumniCreate, AlumniUpdate


def _now() -> datetime.datetime:
    return datetime.datetime.now(datetime.UTC)


def _audit(
    session: AsyncSession,
    actor_user_id: int | None,
    action: str,
    alumni_id: int,
    *,
    field_name: str | None = None,
    old_value: object | None = None,
    new_value: object | None = None,
) -> None:
    """Record an alumni audit event when an acting user is known.

    For field-level changes (updates), ``field_name`` + old/new values are
    captured so the audit doubles as version history. Values are stringified to
    fit the ``text`` audit columns; ``None`` stays ``NULL``.
    """
    if actor_user_id is not None:
        session.add(
            AuditLog(
                user_id=actor_user_id,
                action_type=action,
                entity_type="alumni",
                entity_id=alumni_id,
                field_name=field_name,
                old_value=None if old_value is None else str(old_value),
                new_value=None if new_value is None else str(new_value),
            )
        )


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
    **filters: object,
) -> tuple[list[Alumni], int]:
    """List alumni with pagination. ``filters`` are forwarded to the repository
    (see ``build_alumni_query``: q, graduation_year, grad_year_min/max,
    deceased, include_archived)."""
    return await repo.list_page(session, limit=limit, offset=offset, **filters)


async def create_alumni(
    session: AsyncSession,
    payload: AlumniCreate,
    actor_user_id: int | None = None,
) -> Alumni:
    alumnus = Alumni(**payload.model_dump(exclude_unset=True))
    session.add(alumnus)
    if actor_user_id is not None:
        await session.flush()
        _audit(session, actor_user_id, "create", alumnus.alumni_id)
    await session.commit()
    await session.refresh(alumnus)
    return alumnus


async def update_alumni(
    session: AsyncSession,
    alumni_id: int,
    payload: AlumniUpdate,
    actor_user_id: int | None = None,
) -> Alumni:
    alumnus = await get_alumni(session, alumni_id)
    changes = payload.model_dump(exclude_unset=True)
    # Only audit fields that actually changed; capture before/after per field.
    applied: dict[str, tuple[object, object]] = {}
    for field, value in changes.items():
        old = getattr(alumnus, field)
        if old != value:
            applied[field] = (old, value)
            setattr(alumnus, field, value)
    if applied:
        alumnus.manually_edited_at = _now()
        for field, (old, new) in applied.items():
            _audit(
                session,
                actor_user_id,
                "update",
                alumni_id,
                field_name=field,
                old_value=old,
                new_value=new,
            )
        await session.commit()
        await session.refresh(alumnus)
    return alumnus


async def archive_alumni(
    session: AsyncSession, alumni_id: int, actor_user_id: int | None = None
) -> Alumni:
    """Soft-delete: flag the record archived. Idempotent."""
    alumnus = await get_alumni(session, alumni_id)
    if not alumnus.archived:
        alumnus.archived = True
        alumnus.manually_edited_at = _now()
        _audit(session, actor_user_id, "archive", alumni_id)
        await session.commit()
        await session.refresh(alumnus)
    return alumnus


async def restore_alumni(
    session: AsyncSession, alumni_id: int, actor_user_id: int | None = None
) -> Alumni:
    """Reverse a soft-delete (unarchive). Idempotent."""
    alumnus = await get_alumni(session, alumni_id)
    if alumnus.archived:
        alumnus.archived = False
        alumnus.manually_edited_at = _now()
        _audit(session, actor_user_id, "restore", alumni_id)
        await session.commit()
        await session.refresh(alumnus)
    return alumnus
