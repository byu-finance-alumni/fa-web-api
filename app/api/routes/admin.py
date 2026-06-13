"""User administration routes (super_admin only).

Scope today: list provisioned users and manage their roles on EXISTING accounts.
Creating brand-new auth users with a temporary one-time password (and forced
first-login reset) is a separate, security-sensitive flow over the Supabase
Admin API — see docs/PRE-LAUNCH.md — and is intentionally not implemented here.
"""

import secrets
from typing import Annotated

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.api.dependencies.auth import RequireSuperAdmin
from app.core.database import get_session
from app.core.errors import ConflictError, NotFoundError
from app.core.roles import RoleName
from app.models.audit import AuditLog
from app.models.login_attempt import LoginAttempt
from app.models.user import Role, User, UserRole
from app.services.supabase_admin import set_user_password

router = APIRouter(prefix="/admin", tags=["admin"])

SessionDep = Annotated[AsyncSession, Depends(get_session)]

# Temp-password generation: 20 chars from a mixed alphabet (no ambiguous 0/O/1/l/I)
# via the CSPRNG ``secrets``. ~120 bits of entropy — far beyond brute force for a
# one-time, immediately-rotated credential.
_TEMP_PW_ALPHABET = (
    "ABCDEFGHJKLMNPQRSTUVWXYZ" "abcdefghijkmnopqrstuvwxyz" "23456789" "!@#$%^&*?-_"
)
_TEMP_PW_LENGTH = 20


def _generate_temp_password() -> str:
    """Return a strong, single-use temporary password from the CSPRNG."""
    return "".join(secrets.choice(_TEMP_PW_ALPHABET) for _ in range(_TEMP_PW_LENGTH))


class ResetPasswordResponse(BaseModel):
    """The one-time temporary password, shown to the super_admin exactly once."""

    temp_password: str


class RoleAssign(BaseModel):
    """Assign a canonical role to a user. ``role_name`` is validated against the
    RoleName enum, so an unknown role is a 422 before any query runs."""

    model_config = ConfigDict(extra="forbid")

    role_name: RoleName


class UserActiveUpdate(BaseModel):
    """Activate or deactivate an existing user account.

    The project never hard-deletes users; deactivation flips ``users.active`` to
    false, which the auth dependency layer enforces — a deactivated user is
    blocked (403) on every authenticated route.
    """

    model_config = ConfigDict(extra="forbid")

    active: bool


def _serialize(u: User) -> dict:
    return {
        "user_id": u.user_id,
        "email": u.email,
        "first_name": u.first_name,
        "last_name": u.last_name,
        "active": u.active,
        # Lock state so the Admin -> Users page can show a "Locked" badge. The
        # boolean is derived from locked_at so the UI doesn't need to interpret
        # the timestamp; locked_at is exposed for display/sorting.
        "locked": u.locked_at is not None,
        "locked_at": u.locked_at,
        "roles": [r.role_name for r in u.roles],
    }


async def _load_user(session: AsyncSession, user_id: int) -> User:
    user = await session.scalar(
        select(User).options(selectinload(User.roles)).where(User.user_id == user_id)
    )
    if user is None:
        raise NotFoundError(f"User {user_id} not found.")
    return user


@router.get("/users")
async def list_users(_: RequireSuperAdmin, session: SessionDep) -> list[dict]:
    """List all users with their assigned roles."""
    rows = await session.scalars(
        select(User).options(selectinload(User.roles)).order_by(User.email)
    )
    return [_serialize(u) for u in rows.all()]


@router.patch("/users/{user_id}")
async def set_user_active(
    user_id: int,
    payload: UserActiveUpdate,
    actor: RequireSuperAdmin,
    session: SessionDep,
) -> dict:
    """Deactivate or reactivate an existing user. super_admin only.

    Deactivation is the project's stand-in for removing access (users are never
    hard-deleted): once ``active`` is false the auth dependency rejects every
    authenticated request from that user. A super_admin cannot deactivate their
    own account — that could lock administration out of the system. Every change
    is audited; a no-op (already in the requested state) is idempotent and not
    re-audited.
    """
    if payload.active is False and user_id == actor.user_id:
        raise ConflictError("You cannot deactivate your own account.")

    user = await _load_user(session, user_id)

    if user.active != payload.active:
        old_active = user.active
        user.active = payload.active
        session.add(
            AuditLog(
                user_id=actor.user_id,
                action_type="activate_user" if payload.active else "deactivate_user",
                entity_type="user",
                entity_id=user_id,
                field_name="active",
                old_value=str(old_active),
                new_value=str(payload.active),
            )
        )
        await session.commit()
    return _serialize(await _load_user(session, user_id))


@router.post("/users/{user_id}/roles")
async def assign_role(
    user_id: int,
    payload: RoleAssign,
    actor: RequireSuperAdmin,
    session: SessionDep,
) -> dict:
    """Grant a role to an existing user (idempotent). super_admin only."""
    user = await _load_user(session, user_id)
    role = await session.scalar(
        select(Role).where(Role.role_name == payload.role_name.value)
    )
    if role is None:
        raise NotFoundError(f"Role {payload.role_name.value} is not seeded.")

    if role.role_name not in {r.role_name for r in user.roles}:
        session.add(UserRole(user_id=user_id, role_id=role.role_id))
        session.add(
            AuditLog(
                user_id=actor.user_id,
                action_type="assign_role",
                entity_type="user",
                entity_id=user_id,
                field_name="role",
                new_value=role.role_name,
            )
        )
        await session.commit()
    return _serialize(await _load_user(session, user_id))


@router.delete("/users/{user_id}/roles/{role_name}")
async def remove_role(
    user_id: int,
    role_name: RoleName,
    actor: RequireSuperAdmin,
    session: SessionDep,
) -> dict:
    """Revoke a role from an existing user (idempotent). super_admin only.

    Guards against a super_admin removing their own last super_admin role, which
    would lock user administration out of the system.
    """
    await _load_user(session, user_id)  # 404 if the user doesn't exist
    role = await session.scalar(
        select(Role).where(Role.role_name == role_name.value)
    )
    if role is None:
        raise NotFoundError(f"Role {role_name.value} is not seeded.")

    link = await session.scalar(
        select(UserRole).where(
            UserRole.user_id == user_id, UserRole.role_id == role.role_id
        )
    )
    if link is not None:
        if (
            role.role_name == RoleName.SUPER_ADMIN.value
            and user_id == actor.user_id
        ):
            raise ConflictError("You cannot remove your own super_admin role.")
        await session.delete(link)
        session.add(
            AuditLog(
                user_id=actor.user_id,
                action_type="remove_role",
                entity_type="user",
                entity_id=user_id,
                field_name="role",
                old_value=role.role_name,
            )
        )
        await session.commit()
    return _serialize(await _load_user(session, user_id))


@router.post("/users/{user_id}/reset-password", response_model=ResetPasswordResponse)
async def reset_password(
    user_id: int,
    actor: RequireSuperAdmin,
    session: SessionDep,
) -> ResetPasswordResponse:
    """Set a strong one-time temporary password on a user. super_admin only.

    Flow:
      1. Load the target user and resolve its Supabase auth identity
         (``users.auth_user_id``).
      2. Generate a CSPRNG temp password and set it on the Supabase auth user via
         the Admin API (server-side, service-role key). A non-2xx / transport
         failure raises ServiceError (502) WITHOUT leaking the upstream response.
      3. On success, clear any hard lock (``locked_at`` / ``locked_reason``) and
         delete the rolling ``login_attempts`` row for that email, so the user can
         log in again immediately.
      4. Audit the action (``reset_password``; actor = the super_admin, entity =
         target user). The password is NEVER logged, audited, or returned in any
         channel other than this one-time response body.

    The temp password is returned ONCE in the response for the super_admin to
    hand to the user; the user should change it on next login.
    """
    user = await _load_user(session, user_id)

    temp_password = _generate_temp_password()

    # Set the password on the auth provider FIRST. If this fails we raise before
    # touching our DB, so we never clear a lock for a reset that didn't happen.
    await set_user_password(user.auth_user_id, temp_password)

    was_locked = user.locked_at is not None
    user.locked_at = None
    user.locked_reason = None

    # Drop the rolling failed-login counter for this email so a prior cooldown
    # doesn't immediately re-block the freshly-reset user. Match the throttle's
    # case-insensitive keying (lowercased email).
    await session.execute(
        delete(LoginAttempt).where(LoginAttempt.email_lc == user.email.lower())
    )

    session.add(
        AuditLog(
            user_id=actor.user_id,
            action_type="reset_password",
            entity_type="user",
            entity_id=user_id,
            field_name="locked_at" if was_locked else None,
            old_value="locked" if was_locked else None,
            new_value="unlocked" if was_locked else None,
        )
    )
    await session.commit()

    return ResetPasswordResponse(temp_password=temp_password)
