"""Audit log listing route."""

from typing import Annotated

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies.auth import RequireViewAccess
from app.core.database import get_session
from app.models.audit import AuditLog
from app.models.user import User

router = APIRouter(prefix="/audit", tags=["audit"])

SessionDep = Annotated[AsyncSession, Depends(get_session)]


@router.get("")
async def list_audit(
    _: RequireViewAccess,
    session: SessionDep,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> list[dict]:
    """Most recent audit events, newest first."""
    rows = (
        await session.execute(
            select(AuditLog, User.email)
            .outerjoin(User, AuditLog.user_id == User.user_id)
            .order_by(AuditLog.created_at.desc())
            .limit(limit)
        )
    ).all()
    return [
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
    ]
