"""Signature verification for inbound provider webhooks (fa-web-app #858).

Resend delivers its webhooks through Svix, which signs every delivery:

* headers ``svix-id``, ``svix-timestamp`` (unix seconds) and ``svix-signature``;
* the signed content is ``f"{svix_id}.{svix_timestamp}.{raw_body}"`` -- the RAW
  bytes as received, never a re-serialised parse of them;
* the key is the base64-decoded part of the endpoint secret after its
  ``whsec_`` prefix;
* the signature is base64(HMAC-SHA256(key, content)), and the header can carry
  several space-separated ``v1,<sig>`` entries (one per active secret during a
  rotation) -- a delivery is genuine if ANY of them matches.

A delivery whose timestamp is more than :data:`TOLERANCE_SECONDS` away from now
(either direction) is refused, so a captured request cannot be replayed later.

Comparison is ``hmac.compare_digest`` on BYTES, for the same reason as
``app/core/cron.py``: header values arrive as latin-1 ``str`` and comparing
non-ASCII ``str`` values raises ``TypeError`` -- a malformed header must be a
clean rejection, not a 500.

``verify_resend_webhook`` is the ONE entry point the route calls, and
``scripts/security_scan.py`` (``check_webhook_auth``) checks that every
``/webhooks/`` route reaches it.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import time

from app.core.config import get_settings

TOLERANCE_SECONDS = 5 * 60

_SECRET_PREFIX = "whsec_"


class WebhookNotConfigured(Exception):
    """The signing secret is unset or unusable -- fail CLOSED (503)."""


def _secret_key(secret: str) -> bytes:
    raw = secret.strip()
    if raw.startswith(_SECRET_PREFIX):
        raw = raw[len(_SECRET_PREFIX) :]
    try:
        key = base64.b64decode(raw, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise WebhookNotConfigured("Webhook secret is not valid base64.") from exc
    if not key:
        raise WebhookNotConfigured("Webhook secret is empty.")
    return key


def verify_svix_signature(
    *,
    secret: str,
    svix_id: str | None,
    svix_timestamp: str | None,
    svix_signature: str | None,
    body: bytes,
    now: float | None = None,
) -> bool:
    """True iff the delivery is genuinely signed with ``secret`` and fresh.

    Raises :class:`WebhookNotConfigured` only for an unusable SECRET (our
    misconfiguration); every problem with the REQUEST returns False."""
    key = _secret_key(secret)
    if not svix_id or not svix_timestamp or not svix_signature:
        return False
    try:
        ts = int(svix_timestamp.strip())
    except ValueError:
        return False
    current = time.time() if now is None else now
    if abs(current - ts) > TOLERANCE_SECONDS:
        return False
    # The timestamp is signed as SENT, not as re-formatted; use the header text.
    signed = (
        svix_id.encode("utf-8", "replace")
        + b"."
        + svix_timestamp.strip().encode("utf-8", "replace")
        + b"."
        + body
    )
    expected = base64.b64encode(hmac.new(key, signed, hashlib.sha256).digest())
    matched = False
    for entry in svix_signature.split():
        version, _, sig = entry.partition(",")
        if version != "v1" or not sig:
            continue
        # No early exit: check every entry so timing does not reveal which one.
        if hmac.compare_digest(sig.encode("utf-8", "replace"), expected):
            matched = True
    return matched


def ensure_resend_webhook_configured() -> None:
    """Raise :class:`WebhookNotConfigured` unless ``RESEND_WEBHOOK_SECRET`` is a
    usable signing secret. The route calls this before it reads the body."""
    secret = get_settings().resend_webhook_secret
    if not secret or not secret.strip():
        raise WebhookNotConfigured("RESEND_WEBHOOK_SECRET is not set.")
    _secret_key(secret)


def verify_resend_webhook(
    *,
    svix_id: str | None,
    svix_timestamp: str | None,
    svix_signature: str | None,
    body: bytes,
) -> bool:
    """Verify a Resend delivery against ``RESEND_WEBHOOK_SECRET``.

    Raises :class:`WebhookNotConfigured` when the secret is unset or unusable,
    so the route fails CLOSED and processes nothing."""
    ensure_resend_webhook_configured()
    return verify_svix_signature(
        secret=get_settings().resend_webhook_secret or "",
        svix_id=svix_id,
        svix_timestamp=svix_timestamp,
        svix_signature=svix_signature,
        body=body,
    )
