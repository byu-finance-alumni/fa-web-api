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

from app.core.database import get_session
from app.core.roles import RoleName
from app.core.security import (
    AuthError,
    AuthorizationError,
    DeactivatedAccountError,
    MustChangePasswordError,
    verify_supabase_jwt,
)
from app.models.login_attempt import LoginAttempt
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


def require_roles(
    *allowed: RoleName,
) -> Callable[[UserContext], Awaitable[UserContext]]:
    """Build a dependency that requires the user to hold one of `allowed`.

    Returns the UserContext on success so routes can use it directly; raises
    AuthorizationError (403) otherwise.
    """
    allowed_values = {role.value for role in allowed}

    async def _guard(user: CurrentDBUser) -> UserContext:
        if allowed_values.isdisjoint(user.roles):
            raise AuthorizationError()
        return user

    return _guard


# Role hierarchy: engineer ⊇ super_admin ⊇ full_access ⊇ student ⊇ view_only.
# `engineer` is the top role and therefore appears in EVERY allow-set (it
# satisfies every guard, including super_admin's). `super_admin` satisfies
# every guard except, by design, nothing above it. Guards are allow-lists
# (set-disjoint), not ranked comparisons, so each new role must be added
# explicitly to each guard it should satisfy.
#
# `student` is a narrow writer: it may EDIT existing alumni records (and their
# nested data) but may NOT create new alumni, archive/restore, import, or touch
# user administration. It is therefore added to `require_view_only` (read) and
# to `require_alumni_edit` (edit existing) ONLY — never to `require_full_access`.
require_super_admin = require_roles(RoleName.ENGINEER, RoleName.SUPER_ADMIN)
require_full_access = require_roles(
    RoleName.ENGINEER, RoleName.SUPER_ADMIN, RoleName.FULL_ACCESS
)
# Edit an EXISTING alumnus / their nested records. Adds `student` on top of the
# full_access set. Does NOT permit create, archive, restore, or import.
require_alumni_edit = require_roles(
    RoleName.ENGINEER,
    RoleName.SUPER_ADMIN,
    RoleName.FULL_ACCESS,
    RoleName.STUDENT,
)
require_view_only = require_roles(
    RoleName.ENGINEER,
    RoleName.SUPER_ADMIN,
    RoleName.FULL_ACCESS,
    RoleName.STUDENT,
    RoleName.VIEW_ONLY,
)
# Database / controlled-vocabulary administration (editable dropdowns, #82).
# Engineer-only: per the roles model, the engineer is the controlled-vocabulary
# (and DB) admin. super_admin is intentionally NOT permitted here.
require_vocab_admin = require_roles(RoleName.ENGINEER)
# Engineer-only: the top role exclusively (e.g. managing the support contacts
# shown on the in-app error screen).
require_engineer = require_roles(RoleName.ENGINEER)

RequireSuperAdmin = Annotated[UserContext, Depends(require_super_admin)]
RequireFullAccess = Annotated[UserContext, Depends(require_full_access)]
RequireAlumniEdit = Annotated[UserContext, Depends(require_alumni_edit)]
RequireViewAccess = Annotated[UserContext, Depends(require_view_only)]
RequireVocabAdmin = Annotated[UserContext, Depends(require_vocab_admin)]
RequireEngineer = Annotated[UserContext, Depends(require_engineer)]
