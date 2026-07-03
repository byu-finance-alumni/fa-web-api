"""Supabase JWT verification.

Access tokens are issued by Supabase Auth. We verify them server-side and
NEVER trust client-provided role/identity claims for authorization decisions —
roles are resolved from the database (see app/api/dependencies/auth.py).

Both signing schemes Supabase can use are supported, selected from the token's
`alg` header:
  * HS256        — shared JWT secret (set JWT_SECRET)
  * RS256/ES256  — asymmetric keys, verified via the project's JWKS endpoint
"""

from functools import lru_cache
from typing import Any

import jwt
from jwt import PyJWKClient
from jwt.exceptions import PyJWTError

from app.core.config import get_settings

_SUPABASE_AUDIENCE = "authenticated"
_SUPPORTED_ASYMMETRIC = {"RS256", "ES256"}


class AuthError(Exception):
    """Raised when a token is missing, malformed, expired, or untrusted.

    The message is safe to surface to clients (no internal details). Maps to a
    401 response.
    """

    def __init__(self, message: str = "Could not validate credentials.") -> None:
        self.message = message
        super().__init__(message)


class AuthorizationError(Exception):
    """Raised when an authenticated user lacks permission for an action.

    Distinct from AuthError: the caller proved *who* they are but isn't allowed
    to do *this*. Maps to a 403 response. The message is safe to surface.
    """

    def __init__(
        self, message: str = "You do not have permission to perform this action."
    ) -> None:
        self.message = message
        super().__init__(message)


class DeactivatedAccountError(AuthorizationError):
    """Raised when a valid token belongs to a *deactivated* user account.

    A subclass of AuthorizationError so it still maps to 403, but distinct so the
    block is recorded as its own ``account_deactivated`` security event — a
    deactivated user attempting an authenticated request is high signal.
    """

    def __init__(
        self, message: str = "Your account has been deactivated."
    ) -> None:
        super().__init__(message)


class MustChangePasswordError(AuthorizationError):
    """Raised when a valid token belongs to a user who must change their
    (admin-issued temp) password before doing anything else.

    A subclass of AuthorizationError so it still maps to 403, but distinct so it
    surfaces with the machine code ``password_change_required`` and is recorded
    as its own ``password_change_required`` security event. Enforced on EVERY
    authenticated route except the two needed to complete the change itself, so a
    user holding a valid session can't bypass the forced change by calling the
    backend directly.
    """

    def __init__(
        self,
        message: str = (
            "You must change your password before continuing."
        ),
    ) -> None:
        super().__init__(message)


class SessionSupersededError(AuthError):
    """Raised when a valid token belongs to a session that has been SUPERSEDED —
    the account signed in again on another device, so this (older) session is no
    longer the account's single active session (#147).

    A subclass of AuthError so it still maps to 401, but distinct so it surfaces
    with the machine code ``session_superseded`` (the frontend signs the device
    out and explains why) and is recorded as its own security event. Enforced on
    the data routes (the source of truth); the claiming call itself
    (POST /auth/login) uses the exempt resolver so a new sign-in is never blocked.
    """

    def __init__(
        self,
        message: str = (
            "You were signed out because this account signed in on another "
            "device."
        ),
    ) -> None:
        super().__init__(message)


@lru_cache
def _jwk_client(jwks_url: str) -> PyJWKClient:
    return PyJWKClient(jwks_url)


def _resolve_key_and_alg(token: str) -> tuple[Any, str]:
    settings = get_settings()
    try:
        header = jwt.get_unverified_header(token)
    except PyJWTError as exc:
        raise AuthError("Malformed authentication token.") from exc

    alg = header.get("alg")
    if alg == "HS256":
        if not settings.jwt_secret:
            raise AuthError("Server is not configured to verify HS256 tokens.")
        return settings.jwt_secret, "HS256"

    if alg in _SUPPORTED_ASYMMETRIC:
        if not settings.supabase_url:
            raise AuthError("Server is not configured to verify tokens.")
        jwks_url = f"{settings.supabase_url.rstrip('/')}/auth/v1/.well-known/jwks.json"
        try:
            key = _jwk_client(jwks_url).get_signing_key_from_jwt(token).key
        except Exception as exc:  # JWKS fetch / parse errors
            raise AuthError("Unable to retrieve token signing key.") from exc
        return key, alg

    raise AuthError("Unsupported token signing algorithm.")


def verify_supabase_jwt(token: str) -> dict[str, Any]:
    """Verify a Supabase access token and return its decoded claims.

    Raises AuthError on any failure; never leaks underlying error details.
    """
    settings = get_settings()
    key, alg = _resolve_key_and_alg(token)

    # Issuer validation: when supabase_url is configured we PIN the issuer so a
    # correctly-signed token minted by a DIFFERENT Supabase project is rejected.
    # The asymmetric (RS256/ES256) path already hard-requires supabase_url (it is
    # needed to fetch the JWKS, see _resolve_key_and_alg), so an asymmetric token
    # can never reach here with issuer=None. The HS256 path is the documented
    # offline mode (shared secret, no project URL) and intentionally still
    # verifies signature + audience + exp without an issuer pin.
    if alg in _SUPPORTED_ASYMMETRIC and not settings.supabase_url:
        raise AuthError("Server is not configured to verify tokens.")
    issuer = (
        f"{settings.supabase_url.rstrip('/')}/auth/v1"
        if settings.supabase_url
        else None
    )

    try:
        claims: dict[str, Any] = jwt.decode(
            token,
            key,
            algorithms=[alg],
            audience=_SUPABASE_AUDIENCE,
            issuer=issuer,
            options={"require": ["exp", "sub"]},
        )
    except PyJWTError as exc:
        raise AuthError("Invalid or expired authentication token.") from exc

    return claims
