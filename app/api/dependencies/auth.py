"""Authentication and authorization dependencies.

`get_current_user` verifies the bearer token and returns the token identity.
`get_current_db_user` resolves that identity to a database user and loads their
roles. Authorization (`require_full_access` / `require_view_only`) is decided
from those database roles — never from the token's claims.
"""

import logging
import uuid
from collections.abc import Awaitable, Callable
from typing import Annotated

from fastapi import Depends
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.capabilities import Capability, effective_capabilities
from app.core.database import get_session
from app.core.security import (
    AuthError,
    AuthorizationError,
    DeactivatedAccountError,
    MustChangePasswordError,
    verify_supabase_jwt,
)
from app.models.login_attempt import LoginAttempt
from app.repositories.permissions import load_grants
from app.repositories.user import get_user_with_roles_by_auth_id
from app.schemas.auth import AuthenticatedUser, UserContext

logger = logging.getLogger(__name__)

# auto_error=False so a missing/!Bearer header routes through our AuthError
# handler (401 + error envelope) instead of FastAPI's default 403.
_bearer = HTTPBearer(auto_error=False)


async def get_current_user(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
) -> AuthenticatedUser:
    if credentials is None or not credentials.credentials:
        raise AuthError("Missing bearer token.")

    claims = verify_supabase_jwt(credentials.credentials)

    subject = claims.get("sub")
    if not subject:
        raise AuthError("Token is missing a subject.")

    return AuthenticatedUser(
        auth_user_id=subject,
        email=claims.get("email"),
        token_role=claims.get("role"),
    )


# Convenience alias for route signatures.
CurrentUser = Annotated[AuthenticatedUser, Depends(get_current_user)]


async def get_current_db_user_allow_must_change(
    current: CurrentUser,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> UserContext:
    """Resolve the verified token identity to a provisioned, active DB user,
    WITHOUT enforcing the force-password-change gate.

    Identical to ``get_current_db_user`` (valid token, provisioned + active user
    required) EXCEPT it does NOT raise ``MustChangePasswordError`` when the user
    still holds the flag. This is the EXEMPT variant used only by the handful of
    routes a flagged user must still reach to complete the change itself
    (``GET /auth/context`` and ``POST /auth/password/complete``). Every other
    authenticated route uses ``get_current_db_user``, which enforces the gate.

    Raises AuthError (401) if the token subject isn't a valid id,
    AuthorizationError (403) if there's no matching user (a valid token for
    someone who was never granted access), and DeactivatedAccountError (403) if
    the user exists but has been deactivated — the latter is enforced here so a
    deactivated account is blocked on EVERY authenticated route, not just at the
    point of deactivation, and surfaces as its own security event.
    """
    try:
        auth_uuid = uuid.UUID(current.auth_user_id)
    except ValueError as exc:
        raise AuthError("Token subject is not a valid identifier.") from exc

    user = await get_user_with_roles_by_auth_id(session, auth_uuid)
    if user is None:
        raise AuthorizationError("Your account is not provisioned for access.")
    if not user.active:
        raise DeactivatedAccountError()

    await _clear_login_attempts(session, user.email)

    return UserContext.from_orm_user(user)


async def get_current_db_user(
    current: CurrentUser,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> UserContext:
    """Resolve the verified token identity to a provisioned, active DB user and
    ENFORCE the force-password-change gate.

    Performs the same resolution as ``get_current_db_user_allow_must_change``
    (delegated, so the lookup / deactivation logic lives in one place) and then
    layers the ``must_change_password`` check on top: raises
    MustChangePasswordError (403 / ``password_change_required``) when the user is
    on an admin-issued temp password. This is enforced server-side on EVERY
    authenticated route — the frontend gate is NOT sufficient, since a valid
    session token could otherwise call data endpoints directly and bypass the
    forced change. Only ``GET /auth/context`` and ``POST /auth/password/complete``
    depend on the exempt variant above so the user can read and clear the flag.
    """
    user = await get_current_db_user_allow_must_change(current, session)
    if user.must_change_password:
        raise MustChangePasswordError()
    return user


async def _clear_login_attempts(session: AsyncSession, email: str) -> None:
    """Best-effort clear of the rolling failed-login counter for ``email``.

    A genuinely successful login is the only way to reach an authenticated
    route, so resolving the DB user here is the trustworthy signal to drop the
    rolling ``login_attempts`` row — this replaces the old (abusable)
    unauthenticated ``/auth/login/record {success:true}`` clear. Keyed on the
    lowercased email to match the throttle's case-insensitive keying.

    Deliberately defensive: a failure here must never break the authenticated
    request, so any error is swallowed (the counter just isn't cleared this
    time; it self-expires via the rolling window anyway).
    """
    try:
        await session.execute(
            delete(LoginAttempt).where(LoginAttempt.email_lc == email.lower())
        )
        await session.commit()
    except Exception:  # noqa: BLE001 - best-effort; never fail the request
        logger.warning("Failed to clear login_attempts on auth", exc_info=True)
        try:
            await session.rollback()
        except Exception:  # noqa: BLE001
            pass


CurrentDBUser = Annotated[UserContext, Depends(get_current_db_user)]
# EXEMPT variant: same resolution (valid token, provisioned + active) but does
# NOT enforce the force-password-change gate. Use ONLY on the routes a flagged
# user must reach to complete the change (GET /auth/context, POST
# /auth/password/complete).
CurrentDBUserAllowMustChange = Annotated[
    UserContext, Depends(get_current_db_user_allow_must_change)
]


async def get_permission_config(
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict[str, frozenset[str]]:
    """Load the editable role→capabilities grant map for this request.

    FastAPI caches sub-dependencies within a single request, so this runs once
    per request even when several capability guards depend on it — no stale
    in-process cache to invalidate, and a revoke takes effect on the very next
    request. Falls back to the historical defaults if the table is empty (see
    ``app/repositories/permissions.load_grants``).
    """
    return await load_grants(session)


PermissionConfig = Annotated[dict[str, frozenset[str]], Depends(get_permission_config)]


def require_capability(
    capability: str,
) -> Callable[[UserContext, dict[str, frozenset[str]]], Awaitable[UserContext]]:
    """Build a dependency that requires the user to hold ``capability``.

    The capability is resolved against the LIVE permission config (editable by
    the engineer), not a frozen role set. The engineer always holds every
    capability (hard override in ``effective_capabilities``), so the engineer
    can never lock themselves out. Returns the UserContext on success; raises
    AuthorizationError (403) otherwise.
    """

    async def _guard(
        user: CurrentDBUser, config: PermissionConfig
    ) -> UserContext:
        if capability not in effective_capabilities(config, user.roles):
            raise AuthorizationError()
        return user

    return _guard


# The historical guards, now expressed as capability checks. The capability →
# default-role mapping in ``app/core/capabilities.DEFAULT_GRANTS`` reproduces the
# old hardcoded allow-lists exactly, so behaviour is unchanged until an engineer
# edits the config. Names and type aliases are preserved so every existing route
# import keeps working.
#
# `engineer` ⊇ `super_admin` ⊇ `full_access` ⊇ `student` ⊇ `view_only`. The
# engineer holds every capability unconditionally; the other roles hold whatever
# the config grants them.
require_super_admin = require_capability(Capability.USER_ADMIN)
require_full_access = require_capability(Capability.ALUMNI_FULL)
# Edit an EXISTING alumnus / their nested records — not create/archive/import.
require_alumni_edit = require_capability(Capability.ALUMNI_EDIT)
require_view_only = require_capability(Capability.VIEW)
# Controlled-vocabulary administration (editable dropdowns, #82).
require_vocab_admin = require_capability(Capability.VOCAB_ADMIN)
# Engineer console + permission editor. The `engineer` capability is not
# assignable to any other role, so this stays engineer-exclusive even though it
# now routes through the config like the others.
require_engineer = require_capability(Capability.ENGINEER)

RequireSuperAdmin = Annotated[UserContext, Depends(require_super_admin)]
RequireFullAccess = Annotated[UserContext, Depends(require_full_access)]
RequireAlumniEdit = Annotated[UserContext, Depends(require_alumni_edit)]
RequireViewAccess = Annotated[UserContext, Depends(require_view_only)]
RequireVocabAdmin = Annotated[UserContext, Depends(require_vocab_admin)]
RequireEngineer = Annotated[UserContext, Depends(require_engineer)]
