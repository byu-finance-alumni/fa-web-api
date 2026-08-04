"""Maintenance-mode routes — the engineer's site-wide pause switch.

Four endpoints, one of them public:

  GET  /maintenance/status   PUBLIC. ``{enabled, message}`` and nothing else, so
                             the frontend can render the maintenance page to
                             logged-out visitors.
  GET  /maintenance          RequireEngineer. Console view (adds who/when).
  POST /maintenance/enable   RequireEngineer. Pause + force-logout.
  POST /maintenance/disable  RequireEngineer. Resume.

The disable endpoint is reachable WHILE maintenance is on because engineers are
exempt from the pause — see ``app/services/maintenance`` for why the exempt set
and the "can disable" set are the same set by construction.
"""

from typing import Annotated

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies.auth import RequireEngineer
from app.core.database import get_session
from app.schemas.maintenance import (
    MaintenanceEnableRequest,
    MaintenanceEnableResult,
    MaintenanceState,
    MaintenanceStatus,
)
from app.services import maintenance

router = APIRouter(prefix="/maintenance", tags=["maintenance"])

SessionDep = Annotated[AsyncSession, Depends(get_session)]


@router.get("/status", response_model=MaintenanceStatus)
async def maintenance_status(session: SessionDep) -> MaintenanceStatus:
    """PUBLIC (no auth): is the site in maintenance mode, and what should the
    maintenance page say?

    Intentionally unauthenticated — a logged-out visitor has to be able to learn
    that the site is closed. The response is capped to ``{enabled, message}``
    (see ``MaintenanceStatus``): no actor, no timestamps, no version, no
    account-shaped data of any kind, so it cannot be used to enumerate anything.
    Both fields are single site-wide values that every visitor sees identically.

    Served from the same short-lived process cache the request gate uses, so
    hammering this endpoint does not translate into database load.
    """
    return await maintenance.read_status(session)


@router.get("", response_model=MaintenanceState)
async def get_maintenance_state(
    user: RequireEngineer, session: SessionDep
) -> MaintenanceState:
    """Engineer console view: the public status plus who turned it on and when.

    Uncached — the console must show the true current value, not a value up to
    a few seconds stale.
    """
    return await maintenance.get_state(session)


@router.post("/enable", response_model=MaintenanceEnableResult)
async def enable_maintenance(
    user: RequireEngineer,
    session: SessionDep,
    body: MaintenanceEnableRequest | None = None,
) -> MaintenanceEnableResult:
    """Turn maintenance mode ON.

    Pauses logins and every authenticated request for non-engineers, and ends
    the live session of every signed-in non-engineer account. Engineers — the
    caller included — keep their session and their access, so this same console
    can turn it back off without signing in again.
    """
    return await maintenance.enable(
        session,
        actor_user_id=user.user_id,
        message=body.message if body else None,
    )


@router.post("/disable", response_model=MaintenanceState)
async def disable_maintenance(
    user: RequireEngineer, session: SessionDep
) -> MaintenanceState:
    """Turn maintenance mode OFF and restore normal logins.

    Reachable while maintenance is ON: ``RequireEngineer`` resolves through the
    strict user dependency, whose maintenance gate exempts engineers.
    """
    return await maintenance.disable(session, actor_user_id=user.user_id)
