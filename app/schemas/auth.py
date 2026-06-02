"""Authentication-related schemas."""

from pydantic import BaseModel


class AuthenticatedUser(BaseModel):
    """Verified identity extracted from a Supabase access token.

    `token_role` is the raw role claim from the JWT. It is informational only
    and must NOT drive authorization — roles are resolved from the database.
    """

    auth_user_id: str
    email: str | None = None
    token_role: str | None = None
