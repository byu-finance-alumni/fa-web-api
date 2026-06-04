"""Data access for alumni core records.

Thin query layer — business rules (soft-delete, manual-edit stamping) live in
the service. ``build_alumni_query`` is a pure function (no IO) so the filter
logic can be unit-tested by compiling the statement.

Search currently covers the alumni *core* table (names, external ids,
graduation year, deceased). Employer / industry / title / city / state / tags /
status-label search needs the related tables (modeled later); the filter list
here is structured so those conditions can be added as joins without reshaping
callers.
"""

from sqlalchemy import Select, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.alumni import Alumni


async def get(session: AsyncSession, alumni_id: int) -> Alumni | None:
    return await session.get(Alumni, alumni_id)


def build_alumni_query(
    *,
    q: str | None = None,
    graduation_year: int | None = None,
    grad_year_min: int | None = None,
    grad_year_max: int | None = None,
    deceased: bool | None = None,
    include_archived: bool = False,
) -> Select:
    """Build the filtered ``SELECT alumni`` statement (without limit/offset)."""
    conditions = []
    if not include_archived:
        conditions.append(Alumni.archived.is_(False))
    if q:
        like = f"%{q}%"
        conditions.append(
            or_(
                Alumni.first_name.ilike(like),
                Alumni.last_name.ilike(like),
                Alumni.preferred_first_name.ilike(like),
                Alumni.middle_name.ilike(like),
                Alumni.byu_id.ilike(like),
                Alumni.net_id.ilike(like),
            )
        )
    if graduation_year is not None:
        conditions.append(Alumni.graduation_year == graduation_year)
    if grad_year_min is not None:
        conditions.append(Alumni.graduation_year >= grad_year_min)
    if grad_year_max is not None:
        conditions.append(Alumni.graduation_year <= grad_year_max)
    if deceased is not None:
        conditions.append(Alumni.deceased.is_(deceased))

    stmt = select(Alumni)
    if conditions:
        stmt = stmt.where(*conditions)
    return stmt


async def list_page(
    session: AsyncSession,
    *,
    limit: int,
    offset: int,
    **filters,
) -> tuple[list[Alumni], int]:
    """Return a filtered page of alumni and the total count for that filter."""
    stmt = build_alumni_query(**filters)
    total = await session.scalar(select(func.count()).select_from(stmt.subquery()))
    rows = await session.scalars(
        stmt.order_by(Alumni.last_name, Alumni.alumni_id).limit(limit).offset(offset)
    )
    return list(rows.all()), int(total or 0)
