"""Inbound provider webhooks (fa-web-app #858).

``POST /webhooks/resend`` receives Resend's delivery events so the survey console
can list whose email bounced. It is UNAUTHENTICATED in the session sense -- Resend
has no user -- and the Svix signature is the whole credential:

* ``RESEND_WEBHOOK_SECRET`` unset (or unusable) -> 503 and NOTHING is processed.
  Fail closed: an unconfigured deploy never accepts unsigned events.
* body larger than :data:`MAX_BODY_BYTES` -> 413, before any parsing.
* signature missing, wrong, or timestamp more than five minutes off -> 401.
* a verified ``email.bounced`` / ``email.complained`` is stored, idempotent on
  ``svix-id``; any other verified event type is acknowledged (200) and dropped.

Kept out of the OpenAPI schema: it is not part of the frontend contract.
Rate-limited per client IP (``RESEND_WEBHOOK_LIMITER``). Never logs an email
address or the raw body. ``scripts/security_scan.py`` (``check_webhook_auth``)
proves every ``/webhooks/`` route calls ``verify_resend_webhook``.
"""

from __future__ import annotations

import json
import logging
from typing import Annotated

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_session
from app.core.rate_limit import RESEND_WEBHOOK_LIMITER
from app.core.webhooks import (
    WebhookNotConfigured,
    ensure_resend_webhook_configured,
    verify_resend_webhook,
)
from app.services import survey_bounces

log = logging.getLogger(__name__)

SessionDep = Annotated[AsyncSession, Depends(get_session)]

router = APIRouter(prefix="/webhooks", tags=["webhooks"])

#: Resend's event payloads are a few hundred bytes to a couple of KiB. 64 KiB is
#: far above any real one and bounds what an unsigned caller can make us buffer
#: and HMAC.
MAX_BODY_BYTES = 64 * 1024


def _error(status: int, code: str, message: str) -> JSONResponse:
    return JSONResponse(
        status_code=status, content={"error": {"code": code, "message": message}}
    )


async def _read_capped(request: Request) -> bytes | None:
    """The raw body, or None if it is larger than :data:`MAX_BODY_BYTES`.

    Content-Length is checked first, then the stream is counted as it is read,
    so a caller that omits or understates the header still cannot push more
    than the cap into memory."""
    declared = request.headers.get("content-length")
    if declared is not None:
        try:
            if int(declared) > MAX_BODY_BYTES:
                return None
        except ValueError:
            return None
    buf = bytearray()
    async for chunk in request.stream():
        buf.extend(chunk)
        if len(buf) > MAX_BODY_BYTES:
            return None
    return bytes(buf)


@router.post(
    "/resend",
    include_in_schema=False,
    dependencies=[Depends(RESEND_WEBHOOK_LIMITER)],
)
async def resend_webhook(request: Request, session: SessionDep) -> JSONResponse:
    """Receive one Resend delivery event. See the module docstring."""
    try:
        # Probe the secret BEFORE reading the body: an unconfigured deploy must
        # not even buffer the request.
        ensure_resend_webhook_configured()
    except WebhookNotConfigured:
        # Deliberate, operator-fixable 503 -- not an outage to page about.
        request.state.alert_ignore = True
        log.error("Resend webhook received but RESEND_WEBHOOK_SECRET is not usable")
        return _error(503, "webhook_not_configured", "Webhook is not configured.")

    body = await _read_capped(request)
    if body is None:
        return _error(413, "payload_too_large", "Request body is too large.")

    svix_id = request.headers.get("svix-id")
    if not verify_resend_webhook(
        svix_id=svix_id,
        svix_timestamp=request.headers.get("svix-timestamp"),
        svix_signature=request.headers.get("svix-signature"),
        body=body,
    ):
        return _error(401, "invalid_signature", "Invalid webhook signature.")

    try:
        payload = json.loads(body)
    except (ValueError, UnicodeDecodeError):
        return _error(400, "invalid_payload", "Webhook body is not valid JSON.")

    outcome = await survey_bounces.record_webhook_event(
        session, svix_id=svix_id or "", payload=payload
    )
    return JSONResponse(status_code=200, content={"status": outcome})
