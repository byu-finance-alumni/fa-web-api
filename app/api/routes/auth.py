"""Authentication routes."""

from fastapi import APIRouter

from app.api.dependencies.auth import CurrentDBUser, CurrentUser
from app.schemas.auth import AuthenticatedUser, UserContext

router = APIRouter(prefix="/auth", tags=["auth"])


@router.get("/me", response_model=AuthenticatedUser)
async def me(current_user: CurrentUser) -> AuthenticatedUser:
    """Return the authenticated user's verified identity.

    Requires a valid Supabase access token in the `Authorization: Bearer`
    header.
    """
    return current_user


@router.get("/context", response_model=UserContext)
async def context(user: CurrentDBUser) -> UserContext:
    """Return the signed-in user resolved against the database, with roles.

    Used by the frontend for role-aware UI. Returns 403 if the authenticated
    user isn't provisioned (no active `users` row).
    """
    return user
