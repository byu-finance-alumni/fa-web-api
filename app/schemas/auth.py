"""Authentication-related schemas."""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

from pydantic import BaseModel

from app.core.roles import RoleName

if TYPE_CHECKING:
    from app.models.user import User


class AuthenticatedUser(BaseModel):
    """Verified identity extracted from a Supabase access token.

    `token_role` is the raw role claim from the JWT. It is informational only
    and must NOT drive authorization — roles are resolved from the database.
    """

    auth_user_id: str
    email: str | None = None
    token_role: str | None = None


class UserContext(BaseModel):
    """An authenticated user resolved against the database, with roles.

    This is the object authorization decisions are made from — `roles` comes
    from the `user_roles` table, never from the token.
    """

    user_id: int
    auth_user_id: uuid.UUID
    email: str | None = None
    first_name: str | None = None
    last_name: str | None = None
    roles: list[str] = []

    @classmethod
    def from_orm_user(cls, user: User) -> UserContext:
        return cls(
            user_id=user.user_id,
            auth_user_id=user.auth_user_id,
            email=user.email,
            first_name=user.first_name,
            last_name=user.last_name,
            roles=[role.role_name for role in user.roles],
        )

    @property
    def is_full_access(self) -> bool:
        return RoleName.FULL_ACCESS.value in self.roles

    @property
    def is_view_only(self) -> bool:
        return RoleName.VIEW_ONLY.value in self.roles
