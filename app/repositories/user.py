"""Data access for users and their roles."""

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.user import User


async def get_user_with_roles_by_auth_id(
    session: AsyncSession, auth_user_id: uuid.UUID
) -> User | None:
    """Return the user matching a Supabase auth id, with roles eagerly loaded.

    ``roles`` is eager-loaded (``selectinload``) so authorization can read it
    after the session closes without triggering a lazy IO load.
    """
    stmt = (
        select(User)
        .where(User.auth_user_id == auth_user_id)
        .options(selectinload(User.roles))
    )
    result = await session.execute(stmt)
    return result.scalar_one_or_none()
