"""User administration routes (super_admin only)."""

from typing import Annotated

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.api.dependencies.auth import RequireSuperAdmin
from app.core.database import get_session
from app.models.user import User

router = APIRouter(prefix="/admin", tags=["admin"])

SessionDep = Annotated[AsyncSession, Depends(get_session)]


@router.get("/users")
async def list_users(_: RequireSuperAdmin, session: SessionDep) -> list[dict]:
    """List all users with their assigned roles."""
    rows = await session.scalars(
        select(User).options(selectinload(User.roles)).order_by(User.email)
    )
    return [
        {
            "user_id": u.user_id,
            "email": u.email,
            "first_name": u.first_name,
            "last_name": u.last_name,
            "active": u.active,
            "roles": [r.role_name for r in u.roles],
        }
        for u in rows.all()
    ]
