"""Thin client for the Supabase Auth Admin API.

Used by the super_admin password-reset flow to set a new password on a user's
Supabase *auth* identity (keyed by ``users.auth_user_id``). Authenticates with
the service-role key, which MUST never reach the browser — these calls only ever
run server-side from a super_admin-gated route.

The service-role key bypasses RLS and can administer any auth user, so the
calling route is responsible for authorization (super_admin only) BEFORE calling
here. This module performs no authorization itself.
"""

from __future__ import annotations

import uuid

import httpx

from app.core.config import get_settings
from app.core.errors import ServiceError

# Keep outbound calls snappy; this is a synchronous admin action behind a button.
_TIMEOUT_SECONDS = 10.0


async def set_user_password(auth_user_id: uuid.UUID, new_password: str) -> None:
    """Set a new password on a Supabase auth user via the Admin API.

    PUT {supabase_url}/auth/v1/admin/users/{auth_user_id} with the service-role
    key. Raises ``ServiceError`` (mapped to 502) on any misconfiguration,
    transport failure, or non-2xx response — and NEVER surfaces the upstream
    Supabase response body to the caller (it can contain internal detail). The
    password is never logged.
    """
    settings = get_settings()
    base_url = settings.supabase_url
    service_key = settings.supabase_service_role_key
    if not base_url or not service_key:
        # Misconfiguration is an internal/operational failure, not a client error.
        raise ServiceError("Password reset is unavailable: auth admin not configured.")

    url = f"{base_url.rstrip('/')}/auth/v1/admin/users/{auth_user_id}"
    headers = {
        "apikey": service_key,
        "Authorization": f"Bearer {service_key}",
        "Content-Type": "application/json",
    }
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT_SECONDS) as client:
            response = await client.put(
                url, headers=headers, json={"password": new_password}
            )
    except httpx.HTTPError as exc:
        # Transport-level failure (DNS, timeout, connection reset). Log the
        # exception TYPE only — never the URL with the key or the body.
        raise ServiceError(
            "Could not reach the authentication service to reset the password."
        ) from exc

    if not response.is_success:
        # Deliberately opaque: do not leak the Supabase status/body to the client.
        raise ServiceError("The authentication service rejected the password reset.")
