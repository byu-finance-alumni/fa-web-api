"""Dashboard summary metrics computed from the alumni core table."""

from typing import Annotated

from fastapi import APIRouter, Depends
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies.auth import RequireViewAccess
from app.core.database import get_session
from app.models.alumni import Alumni

router = APIRouter(prefix="/dashboard", tags=["dashboard"])

SessionDep = Annotated[AsyncSession, Depends(get_session)]


@router.get("/summary")
async def summary(_: RequireViewAccess, session: SessionDep) -> dict:
    """Counts + graduation-cohort breakdown for the dashboard."""
    active = Alumni.archived.is_(False)
    total = await session.scalar(
        select(func.count()).select_from(Alumni).where(active)
    )
    archived = await session.scalar(
        select(func.count()).select_from(Alumni).where(Alumni.archived.is_(True))
    )
    deceased = await session.scalar(
        select(func.count())
        .select_from(Alumni)
        .where(active, Alumni.deceased.is_(True))
    )
    rows = (
        await session.execute(
            select(Alumni.graduation_year, func.count())
            .where(active, Alumni.graduation_year.is_not(None))
            .group_by(Alumni.graduation_year)
            .order_by(Alumni.graduation_year)
        )
    ).all()
    return {
        "total_alumni": int(total or 0),
        "archived": int(archived or 0),
        "deceased": int(deceased or 0),
        "by_graduation_year": [
            {"year": r[0], "count": int(r[1])} for r in rows
        ],
    }
