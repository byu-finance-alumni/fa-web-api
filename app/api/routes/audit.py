"""Audit log listing route."""

import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies.auth import RequireSuperAdmin
from app.core.database import get_session
from app.models.audit import AuditLog
from app.repositories.audit import build_audit_query

router = APIRouter(prefix="/audit", tags=["audit"])

SessionDep = Annotated[AsyncSession, Depends(get_session)]


@router.get("")
async def list_audit(
    _: RequireSuperAdmin,
    session: SessionDep,
    action_type: Annotated[
        str | None, Query(description="Exact action type, e.g. 'update'.")
    ] = None,
    entity_type: Annotated[
        str | None, Query(description="Exact entity type, e.g. 'alumni'.")
    ] = None,
    user: Annotated[
        str | None,
        Query(description="Acting user's email (case-insensitive substring)."),
    ] = None,
    date_from: Annotated[
        datetime.date | None,
        Query(description="Only events on/after this date (inclusive)."),
    ] = None,
    date_to: Annotated[
        datetime.date | None,
        Query(description="Only events on/before this date (inclusive)."),
    ] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> dict:
    """Most recent audit events, newest first, with optional server-side
    filtering by action type, entity type, acting-user email, and date range.
    Paginated (offset + total) so forensic review can reach past the first
    page — without it, events older than one page were invisible and could be
    buried by flooding the log. All filtering happens in PostgreSQL."""
    # A bare date covers the whole day: bound the end at the last instant of
    # that day so same-day events are included regardless of their time.
    end_bound = (
        datetime.datetime.combine(date_to, datetime.time.max, tzinfo=datetime.UTC)
        if date_to is not None
        else None
    )
    start_bound = (
        datetime.datetime.combine(date_from, datetime.time.min, tzinfo=datetime.UTC)
        if date_from is not None
        else None
    )
    base = build_audit_query(
        action_type=action_type,
        entity_type=entity_type,
        user=user,
        date_from=start_bound,
        date_to=end_bound,
    )
    total = await session.scalar(
        select(func.count()).select_from(base.subquery())
    )
    rows = (await session.execute(base.limit(limit).offset(offset))).all()
    return {
        "items": [
            {
                "audit_log_id": a.audit_log_id,
                "created_at": a.created_at.isoformat() if a.created_at else None,
                "user": email,
                "action_type": a.action_type,
                "entity_type": a.entity_type,
                "entity_id": a.entity_id,
                "field_name": a.field_name,
                "old_value": a.old_value,
                "new_value": a.new_value,
            }
            for a, email in rows
        ],
        "total": int(total or 0),
        "limit": limit,
        "offset": offset,
    }


@router.get("/options")
async def audit_options(_: RequireSuperAdmin, session: SessionDep) -> dict:
    """Distinct, sorted, non-null action and entity types for the filter menu
    (super admin only — the audit trail's old/new values can contain alumni
    PII). Two small queries against ``audit_logs`` — the backend still accepts
    any value, so the toolbar always offers an "Any" default too."""
    action_rows = (
        await session.execute(
            select(AuditLog.action_type)
            .where(AuditLog.action_type.isnot(None))
            .distinct()
            .order_by(AuditLog.action_type)
        )
    ).all()
    entity_rows = (
        await session.execute(
            select(AuditLog.entity_type)
            .where(AuditLog.entity_type.isnot(None))
            .distinct()
            .order_by(AuditLog.entity_type)
        )
    ).all()
    return {
        "action_types": [r[0] for r in action_rows if r[0]],
        "entity_types": [r[0] for r in entity_rows if r[0]],
    }
