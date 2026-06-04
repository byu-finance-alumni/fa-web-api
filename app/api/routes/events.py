"""Event listing routes."""

from typing import Annotated

from fastapi import APIRouter, Depends
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies.auth import RequireViewAccess
from app.core.database import get_session
from app.core.errors import NotFoundError
from app.models.event import Event, EventAttendance

router = APIRouter(prefix="/events", tags=["events"])

SessionDep = Annotated[AsyncSession, Depends(get_session)]


def _serialize(e: Event, attendance: int) -> dict:
    return {
        "event_id": e.event_id,
        "event_name": e.event_name,
        "event_type": e.event_type,
        "event_date": e.event_date.isoformat() if e.event_date else None,
        "event_location": e.event_location,
        "event_notes": e.event_notes,
        "attendance_count": attendance,
    }


@router.get("")
async def list_events(_: RequireViewAccess, session: SessionDep) -> list[dict]:
    count_sq = (
        select(
            EventAttendance.event_id,
            func.count().label("att"),
        )
        .group_by(EventAttendance.event_id)
        .subquery()
    )
    rows = (
        await session.execute(
            select(Event, func.coalesce(count_sq.c.att, 0))
            .outerjoin(count_sq, Event.event_id == count_sq.c.event_id)
            .order_by(Event.event_date.desc().nullslast())
        )
    ).all()
    return [_serialize(e, int(att)) for e, att in rows]


@router.get("/{event_id}")
async def get_event(
    event_id: int, _: RequireViewAccess, session: SessionDep
) -> dict:
    event = await session.get(Event, event_id)
    if event is None:
        raise NotFoundError(f"Event {event_id} not found.")
    att = await session.scalar(
        select(func.count())
        .select_from(EventAttendance)
        .where(EventAttendance.event_id == event_id)
    )
    return _serialize(event, int(att or 0))
