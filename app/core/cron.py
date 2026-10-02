"""Shared authorization for Vercel Cron endpoints.

The survey run, the opportunity-link digest and the headshot sweep are all
triggered by Vercel Cron, not by a logged-in user, so they authorize on a shared
secret instead of a capability: the request must carry
``Authorization: Bearer <CRON_SECRET>``, which Vercel Cron sends automatically
when ``CRON_SECRET`` is set as a project env var. When ``CRON_SECRET`` is unset
every call is rejected, so these endpoints are never open by default.

One helper so the three sites can't drift. It compares BYTES, not ``str``:
Starlette decodes header values as latin-1, and ``hmac.compare_digest`` raises
``TypeError`` when asked to compare ``str`` values containing non-ASCII
characters. Feeding it the raw ``str`` therefore turned a malformed (non-ASCII)
``Authorization`` header into an unhandled 500 — which the failure monitor counts
and can escalate into a false "outage" alert — instead of a clean 401. Encoding
both operands first keeps the comparison constant-time and total.
"""

from __future__ import annotations

import hmac

from fastapi import HTTPException, Request

from app.core.config import get_settings


def verify_cron_secret(request: Request) -> None:
    """Authorize a Vercel Cron call; raise 401 on any other or absent credential.

    Rejects everything when ``CRON_SECRET`` is unset (default-closed).
    """
    expected = get_settings().cron_secret
    provided = request.headers.get("Authorization", "")
    if not expected or not hmac.compare_digest(
        provided.encode("utf-8"), f"Bearer {expected}".encode()
    ):
        raise HTTPException(status_code=401, detail="Invalid cron credentials.")
