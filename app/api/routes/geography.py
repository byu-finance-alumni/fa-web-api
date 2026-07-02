"""Alumni Geography dashboard endpoints.

Read-only, view-access gated (no public access). Location-based aggregation for
the geography dashboard: state choropleth, state/city drill-down, rankings, and
summary analytics. All counts are computed in PostgreSQL.
"""

import logging
from typing import Annotated

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies.auth import RequireFullAccess, RequireViewAccess
from app.core.database import get_session
from app.models.audit import AuditLog
from app.schemas.auth import UserContext
from app.schemas.geography import (
    Breakdown,
    CityDetail,
    CountryCount,
    CountyCount,
    GeoAlumniPage,
    GeoSummary,
    RadiusPage,
    StateCount,
    StateDetail,
)
from app.services import geography as svc

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/geography", tags=["geography"])

SessionDep = Annotated[AsyncSession, Depends(get_session)]


async def _audit_view(
    session: AsyncSession, actor: UserContext, *, entity_type: str, field_name: str
) -> None:
    """Best-effort disclosure audit for a geography alumni drill-down.

    FERPA: drilling into the individual alumni behind a location is a disclosure,
    so the access is recorded (actor + which drill-down, never the returned PII).
    Defensive — if the audit write fails it must never break the read."""
    try:
        session.add(
            AuditLog(
                user_id=actor.user_id,
                action_type="view",
                entity_type=entity_type,
                field_name=field_name,
            )
        )
        await session.commit()
    except Exception:  # noqa: BLE001 - best-effort; never fail the read
        logger.warning("Failed to write geography view audit", exc_info=True)
        try:
            await session.rollback()
        except Exception:  # noqa: BLE001
            pass


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


@router.get("/summary", response_model=GeoSummary)
async def summary(
    _: RequireViewAccess, session: SessionDep, filters: FiltersDep
) -> GeoSummary:
    """Analytics cards + the available filter options."""
    return await svc.get_summary(session, filters)


@router.get("/states", response_model=list[StateCount])
async def states(
    _: RequireViewAccess, session: SessionDep, filters: FiltersDep
) -> list[StateCount]:
    """Per-state alumni counts for the choropleth map and Top States ranking."""
    return await svc.get_states(session, filters)


@router.get("/counties", response_model=list[CountyCount])
async def counties(
    _: RequireViewAccess, session: SessionDep, filters: FiltersDep
) -> list[CountyCount]:
    """Per-county alumni counts (5-digit FIPS) for the national county choropleth.

    Aggregate counts only (no PII), so view-accessible like ``/states``."""
    return await svc.get_counties(session, filters)


@router.get("/countries", response_model=list[CountryCount])
async def countries(
    _: RequireViewAccess, session: SessionDep, filters: FiltersDep
) -> list[CountryCount]:
    """Per-country alumni counts (international) for the world-map view.

    Aggregate counts only (no PII), so view-accessible like ``/states``."""
    return await svc.get_countries(session, filters)


@router.get("/breakdown", response_model=Breakdown)
async def breakdown(
    _: RequireViewAccess,
    session: SessionDep,
    filters: FiltersDep,
    dimension: Annotated[
        str, Query(pattern="^(states|cities|employers|industries)$")
    ],
) -> Breakdown:
    """Full ranked list for a dimension (the 'View all' breakdown table)."""
    return await svc.get_breakdown(session, dimension, filters)


@router.get("/states/{state}", response_model=StateDetail)
async def state_detail(
    state: str, _: RequireViewAccess, session: SessionDep, filters: FiltersDep
) -> StateDetail:
    """Count + top cities / employers / industries for one state."""
    return await svc.get_state_detail(session, state, filters)


@router.get("/states/{state}/alumni", response_model=GeoAlumniPage)
async def state_alumni(
    state: str,
    actor: RequireFullAccess,
    session: SessionDep,
    filters: FiltersDep,
    sort: Annotated[str, Query(pattern="^(name|year|city)$")] = "name",
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> GeoAlumniPage:
    """Paginated, sortable alumni list for a state.

    FERPA: this lists the individual alumni behind a state count, so it is gated
    to full_access (view_only gets 403) and the disclosure is audited; the
    aggregate state/city counts stay view-accessible."""
    result = await svc.get_state_alumni(
        session, state, filters, limit=limit, offset=offset, sort=sort
    )
    await _audit_view(
        session, actor, entity_type="geography:state_alumni", field_name=state
    )
    return result


@router.get("/radius", response_model=RadiusPage)
async def radius_alumni(
    actor: RequireFullAccess,
    session: SessionDep,
    filters: FiltersDep,
    lat: Annotated[float, Query(ge=-90, le=90)],
    lng: Annotated[float, Query(ge=-180, le=180)],
    miles: Annotated[float, Query(gt=0, le=500)],
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> RadiusPage:
    """Alumni within ``miles`` of (lat, lng), nearest first.

    Distance is city-level (an alumnus's location is their city's coordinates,
    via the city_geo crosswalk — the only geographic signal stored). FERPA: this
    lists the individual alumni behind a location, so it is gated to full_access
    (view_only gets 403) and the disclosure is audited."""
    result = await svc.get_radius_alumni(
        session, lat, lng, miles, filters, limit=limit, offset=offset
    )
    await _audit_view(
        session,
        actor,
        entity_type="geography:radius_search",
        field_name=f"{lat:.4f},{lng:.4f},{miles}mi",
    )
    return result


@router.get("/cities", response_model=CityDetail)
async def city_detail(
    actor: RequireFullAccess,
    session: SessionDep,
    filters: FiltersDep,
    state: Annotated[str, Query(min_length=1)],
    city: Annotated[str, Query(min_length=1)],
) -> CityDetail:
    """City drill-down: count + employer / industry / grad-year distribution
    AND the individual alumni in that city.

    FERPA: the response includes the named alumni in the city, so it is gated to
    full_access (view_only gets 403) and the disclosure is audited; the aggregate
    state/city counts stay view-accessible."""
    result = await svc.get_city_detail(session, state, city, filters)
    await _audit_view(
        session, actor, entity_type="geography:city_detail", field_name=city
    )
    return result
