"""The one outbound email transport: Resend.

There is exactly ONE mail integration in this app and this module is it. The
survey sender (``app/services/survey_email.py``) and the API failure alerter
(``app/services/failure_alert.py``) both post through here, so there is one API
key (``RESEND_API_KEY``), one sending identity, one timeout policy and one place
to look when mail stops leaving the building.

This module deliberately does NOTHING but transport. It does not decide what a
failure means, does not retry, and does not translate status codes into domain
exceptions — the two callers need opposite behaviour there:

* the survey sender must distinguish "Resend said no" (release the claim, the
  recipients go out later) from "we never learned the outcome" (keep the claim,
  never risk emailing an alum twice);
* the alerter must never raise at all, because it runs on the failure path of a
  request that is already broken and a raising alerter would turn one broken
  request into two.

So each caller keeps its own error policy and shares only the wire call.

The API key lives ONLY in backend config and is never exposed to the frontend.
"""

from __future__ import annotations

import httpx

RESEND_API_BASE = "https://api.resend.com"
# Batch endpoint (up to 100 messages per call) — the survey campaign.
RESEND_BATCH_URL = f"{RESEND_API_BASE}/emails/batch"
# Single-message endpoint — operational mail (one alert to one engineer).
RESEND_SEND_URL = f"{RESEND_API_BASE}/emails"


def from_field(email: str, name: str | None = None) -> str:
    """Render a Resend ``From`` value: ``Name <addr>``, or the bare address."""
    return f"{name} <{email}>" if name else email


async def post_json(
    url: str,
    *,
    api_key: str,
    payload: object,
    timeout: float,
) -> httpx.Response:
    """POST ``payload`` to a Resend endpoint and return the raw response.

    Raises only what httpx raises (``httpx.HTTPError`` and subclasses) for a
    transport-level failure; a non-2xx RESPONSE is returned as-is for the caller
    to interpret. The client is created per call and closed on exit — this runs
    on serverless, where a module-level pooled client would outlive the frozen
    invocation that created it (the same reasoning as ``NullPool`` in
    ``app/core/database.py``).
    """
    async with httpx.AsyncClient(timeout=timeout) as client:
        return await client.post(
            url,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json=payload,
        )
