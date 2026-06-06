"""Authentication and authorization dependencies.

`get_current_user` verifies the bearer token and returns the token identity.
`get_current_db_user` resolves that identity to a database user and loads their
roles. Authorization (`require_full_access` / `require_view_only`) is decided
from those database roles — never from the token's claims.
"""

import uuid
from collections.abc import Awaitable, Callable
from typing import Annotated

from fastapi import Depends
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_session
from app.core.roles import RoleName
from app.core.security import AuthError, AuthorizationError, verify_supabase_jwt
from app.repositories.user import get_user_with_roles_by_auth_id
from app.schemas.auth import AuthenticatedUser, UserContext

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


async def get_current_db_user(
    current: CurrentUser,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> UserContext:
    """Resolve the verified token identity to a provisioned, active DB user.

    Raises AuthError (401) if the token subject isn't a valid id, and
    AuthorizationError (403) if there's no matching active user — a valid token
    for someone who hasn't been granted access.
    """
    try:
        auth_uuid = uuid.UUID(current.auth_user_id)
    except ValueError as exc:
        raise AuthError("Token subject is not a valid identifier.") from exc

    user = await get_user_with_roles_by_auth_id(session, auth_uuid)
    if user is None or not user.active:
        raise AuthorizationError("Your account is not provisioned for access.")

    return UserContext.from_orm_user(user)


CurrentDBUser = Annotated[UserContext, Depends(get_current_db_user)]


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


# Role hierarchy: super_admin ⊇ full_access ⊇ view_only. super_admin satisfies
# every guard; user/role administration requires super_admin specifically.
require_super_admin = require_roles(RoleName.SUPER_ADMIN)
require_full_access = require_roles(RoleName.SUPER_ADMIN, RoleName.FULL_ACCESS)
require_view_only = require_roles(
    RoleName.SUPER_ADMIN, RoleName.FULL_ACCESS, RoleName.VIEW_ONLY
)

RequireSuperAdmin = Annotated[UserContext, Depends(require_super_admin)]
RequireFullAccess = Annotated[UserContext, Depends(require_full_access)]
RequireViewAccess = Annotated[UserContext, Depends(require_view_only)]
