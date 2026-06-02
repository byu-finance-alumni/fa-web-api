"""Authentication routes."""

from fastapi import APIRouter

from app.api.dependencies.auth import CurrentUser
from app.schemas.auth import AuthenticatedUser

router = APIRouter(prefix="/auth", tags=["auth"])


@router.get("/me", response_model=AuthenticatedUser)
async def me(current_user: CurrentUser) -> AuthenticatedUser:
    """Return the authenticated user's verified identity.

    Requires a valid Supabase access token in the `Authorization: Bearer`
    header.
    """
    return current_user
