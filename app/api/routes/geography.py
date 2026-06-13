"""Alumni Geography dashboard endpoints.

Read-only, view-access gated (no public access). Location-based aggregation for
the geography dashboard: state choropleth, state/city drill-down, rankings, and
summary analytics. All counts are computed in PostgreSQL.
"""

from typing import Annotated

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies.auth import RequireViewAccess
from app.core.database import get_session
from app.services import geography as svc

router = APIRouter(prefix="/geography", tags=["geography"])

SessionDep = Annotated[AsyncSession, Depends(get_session)]


def filter_params(
    employer: str | None = None,
    industry: str | None = None,
    year: int | None = None,
    region: str | None = None,
    tag: str | None = None,
) -> dict:
    """Shared geography filters (employer / industry / grad year / region / tag)."""
    return {
        "employer": employer,
        "industry": industry,
        "year": year,
        "region": region,
        "tag": tag,
    }


FiltersDep = Annotated[dict, Depends(filter_params)]


@router.get("/summary")
async def summary(
    _: RequireViewAccess, session: SessionDep, filters: FiltersDep
) -> dict:
    """Analytics cards + the available filter options."""
    return await svc.get_summary(session, filters)


@router.get("/states")
async def states(
    _: RequireViewAccess, session: SessionDep, filters: FiltersDep
) -> list[dict]:
    """Per-state alumni counts for the choropleth map and Top States ranking."""
    return await svc.get_states(session, filters)


@router.get("/breakdown")
async def breakdown(
    _: RequireViewAccess,
    session: SessionDep,
    filters: FiltersDep,
    dimension: Annotated[
        str, Query(pattern="^(states|cities|employers|industries)$")
    ],
) -> dict:
    """Full ranked list for a dimension (the 'View all' breakdown table)."""
    return await svc.get_breakdown(session, dimension, filters)


@router.get("/states/{state}")
async def state_detail(
    state: str, _: RequireViewAccess, session: SessionDep, filters: FiltersDep
) -> dict:
    """Count + top cities / employers / industries for one state."""
    return await svc.get_state_detail(session, state, filters)


@router.get("/states/{state}/alumni")
async def state_alumni(
    state: str,
    _: RequireViewAccess,
    session: SessionDep,
    filters: FiltersDep,
    sort: Annotated[str, Query(pattern="^(name|year|city)$")] = "name",
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> dict:
    """Paginated, sortable alumni list for a state."""
    return await svc.get_state_alumni(
        session, state, filters, limit=limit, offset=offset, sort=sort
    )


@router.get("/cities")
async def city_detail(
    _: RequireViewAccess,
    session: SessionDep,
    filters: FiltersDep,
    state: Annotated[str, Query(min_length=1)],
    city: Annotated[str, Query(min_length=1)],
) -> dict:
    """City drill-down: count + employer / industry / grad-year distribution."""
    return await svc.get_city_detail(session, state, city, filters)
