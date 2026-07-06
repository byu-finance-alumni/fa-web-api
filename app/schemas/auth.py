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
    # Supabase session identifier (``session_id`` claim) — identifies THIS
    # sign-in/device, so the single-active-session guard (#147) can tell one of
    # the account's sessions from another. None if the token predates the claim.
    session_id: str | None = None
    # Token issued-at (``iat`` claim, epoch seconds). Used by the single-session
    # guard to decide which of two sessions is genuinely NEWER (#188), so a newer
    # login supersedes an older one even if its best-effort claim call was lost.
    session_issued_at: int | None = None


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
    # The user's EFFECTIVE capability codes under the live permission config
    # (#164). Populated by GET /auth/context for the frontend's capability-aware
    # UI; empty on the internal UserContext that the authorization guards resolve
    # (guards re-derive capabilities from the config per request — they never
    # trust this field). See app/core/capabilities.
    capabilities: list[str] = []
    # True while the user is on an admin-issued temp password and must set their
    # own (the frontend gates them into a set-password screen). Reflects the
    # CURRENT authenticated user's flag; cleared via POST /auth/password/complete.
    must_change_password: bool = False
    # Single-active-session (#147). ``session_id`` is THIS request's token
    # session (from the JWT); ``active_session_id`` is the account's current
    # active session from the DB. When both are set and differ, this session has
    # been superseded by a newer login. Populated by the auth resolver.
    # ``session_issued_at`` is the token ``iat`` used to break ties by recency
    # (#188). Populated by the auth resolver.
    session_id: str | None = None
    session_issued_at: int | None = None
    active_session_id: str | None = None

    @classmethod
    def from_orm_user(cls, user: User) -> UserContext:
        return cls(
            user_id=user.user_id,
            auth_user_id=user.auth_user_id,
            email=user.email,
            first_name=user.first_name,
            last_name=user.last_name,
            roles=[role.role_name for role in user.roles],
            must_change_password=user.must_change_password,
            active_session_id=user.active_session_id,
        )

    @property
    def is_engineer(self) -> bool:
        return RoleName.ENGINEER.value in self.roles

    @property
    def is_super_admin(self) -> bool:
        return RoleName.SUPER_ADMIN.value in self.roles

    @property
    def is_full_access(self) -> bool:
        return RoleName.FULL_ACCESS.value in self.roles

    @property
    def is_student(self) -> bool:
        return RoleName.STUDENT.value in self.roles

    @property
    def is_view_only(self) -> bool:
        return RoleName.VIEW_ONLY.value in self.roles

    @property
    def can_edit_alumni(self) -> bool:
        """True for any role permitted to edit an existing alumnus and their
        nested records: engineer, super_admin, full_access, or student. Mirrors
        the ``require_alumni_edit`` guard. Does NOT imply create/archive/import
        rights (those are ``full_access`` and up)."""
        return bool(
            {
                RoleName.ENGINEER.value,
                RoleName.SUPER_ADMIN.value,
                RoleName.FULL_ACCESS.value,
                RoleName.STUDENT.value,
            }.intersection(self.roles)
        )
