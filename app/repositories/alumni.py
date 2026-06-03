"""Data access for alumni core records.

Thin query layer — business rules (soft-delete, manual-edit stamping) live in
the service. ``list_page`` excludes archived rows by default.
"""

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.alumni import Alumni


async def get(session: AsyncSession, alumni_id: int) -> Alumni | None:
    return await session.get(Alumni, alumni_id)


async def list_page(
    session: AsyncSession,
    *,
    limit: int,
    offset: int,
    include_archived: bool = False,
) -> tuple[list[Alumni], int]:
    """Return a page of alumni and the total count matching the same filter."""
    stmt = select(Alumni)
    if not include_archived:
        stmt = stmt.where(Alumni.archived.is_(False))

    total = await session.scalar(
        select(func.count()).select_from(stmt.subquery())
    )
    rows = await session.scalars(
        stmt.order_by(Alumni.last_name, Alumni.alumni_id).limit(limit).offset(offset)
    )
    return list(rows.all()), int(total or 0)
