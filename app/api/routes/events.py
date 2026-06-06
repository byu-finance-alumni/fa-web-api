"""Event listing routes."""

import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies.auth import RequireFullAccess, RequireViewAccess
from app.core.database import get_session
from app.core.errors import NotFoundError
from app.models.alumni import Alumni
from app.models.audit import AuditLog
from app.models.event import Event, EventAttendance
from app.schemas.event import EventCreate

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
async def list_events(
    _: RequireViewAccess,
    session: SessionDep,
    q: Annotated[
        str | None,
        Query(description="Substring match on event name or location (case-insensitive)."),
    ] = None,
    event_type: Annotated[
        str | None,
        Query(description="Event type (case-insensitive exact match)."),
    ] = None,
    date_from: Annotated[
        datetime.date | None,
        Query(description="Only events on or after this date (inclusive)."),
    ] = None,
    date_to: Annotated[
        datetime.date | None,
        Query(description="Only events on or before this date (inclusive)."),
    ] = None,
) -> list[dict]:
    count_sq = (
        select(
            EventAttendance.event_id,
            func.count().label("att"),
        )
        .group_by(EventAttendance.event_id)
        .subquery()
    )
    stmt = (
        select(Event, func.coalesce(count_sq.c.att, 0))
        .outerjoin(count_sq, Event.event_id == count_sq.c.event_id)
    )
    if q:
        term = q.strip()
        if term:
            pattern = f"%{term}%"
            stmt = stmt.where(
                Event.event_name.ilike(pattern)
                | Event.event_location.ilike(pattern)
            )
    if event_type:
        stmt = stmt.where(func.lower(Event.event_type) == event_type.strip().lower())
    if date_from is not None:
        stmt = stmt.where(Event.event_date >= date_from)
    if date_to is not None:
        stmt = stmt.where(Event.event_date <= date_to)
    stmt = stmt.order_by(Event.event_date.desc().nullslast())
    rows = (await session.execute(stmt)).all()
    return [_serialize(e, int(att)) for e, att in rows]


@router.get("/options")
async def event_options(_: RequireViewAccess, session: SessionDep) -> dict:
    """Distinct, sorted, non-null event types for the filter menu (view access)."""
    rows = (
        await session.execute(
            select(Event.event_type)
            .where(Event.event_type.isnot(None))
            .distinct()
            .order_by(Event.event_type)
        )
    ).all()
    return {"types": [r[0] for r in rows if r[0]]}


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_event(
    payload: EventCreate, user: RequireFullAccess, session: SessionDep
) -> dict:
    """Create an event (full_access). Stamps the acting user and audits the
    write (entity_type "event", action "create")."""
    event = Event(
        event_name=payload.event_name,
        event_type=payload.event_type,
        event_date=payload.event_date,
        event_location=payload.event_location,
        event_notes=payload.event_notes,
        logged_by_user_id=user.user_id,
    )
    session.add(event)
    await session.flush()
    session.add(
        AuditLog(
            user_id=user.user_id,
            action_type="create",
            entity_type="event",
            entity_id=event.event_id,
        )
    )
    await session.commit()
    await session.refresh(event)
    return _serialize(event, 0)


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


def _attendee_name(a: Alumni) -> str:
    name = " ".join(
        p for p in (a.preferred_first_name or a.first_name, a.last_name) if p
    ).strip()
    return name or f"Alumni #{a.alumni_id}"


@router.get("/{event_id}/attendees")
async def list_event_attendees(
    event_id: int, _: RequireViewAccess, session: SessionDep
) -> list[dict]:
    """Alumni who attended an event (view-access read). 404 if the event is
    unknown so callers can distinguish "no attendees" from "no such event"."""
    event = await session.get(Event, event_id)
    if event is None:
        raise NotFoundError(f"Event {event_id} not found.")
    rows = (
        await session.execute(
            select(Alumni, EventAttendance.attendance_status)
            .join(EventAttendance, EventAttendance.alumni_id == Alumni.alumni_id)
            .where(EventAttendance.event_id == event_id)
            .order_by(Alumni.last_name, Alumni.first_name)
        )
    ).all()
    return [
        {
            "alumni_id": a.alumni_id,
            "name": _attendee_name(a),
            "graduation_year": a.graduation_year,
            "attendance_status": status,
        }
        for a, status in rows
    ]
