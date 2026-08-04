"""Security-event logging (Tier 1 — stdout only, no database).

Emits one structured WARNING/ERROR line per authentication / authorization
failure (and unhandled error) so intrusion attempts surface in the platform
logs. On Vercel, stdout becomes the function's runtime logs — searchable, and
forwardable to a SIEM via a Log Drain — with no table or migration.

Redaction-safe by design (see CLAUDE.md logging rules): we log request
*metadata* only — never tokens, request bodies, query strings, or alumni PII.
The URL path is logged WITHOUT its query string, because search params (e.g.
``?q=<name>``) can contain personal data.

Each line is prefixed ``security_event`` and carries a JSON payload, so it's both
greppable (``grep security_event``) and machine-parseable by a log drain.
"""

import json
import logging

from fastapi import Request

_logger = logging.getLogger("security")


def client_ip(request: Request) -> str | None:
    """Best-effort real client IP. Vercel/proxies set X-Forwarded-For as
    ``client, proxy1, proxy2`` — the first hop is the originating client."""
    fwd = request.headers.get("x-forwarded-for")
    if fwd:
        return fwd.split(",")[0].strip()
    real = request.headers.get("x-real-ip")
    if real:
        return real.strip()
    return request.client.host if request.client else None


def log_security_event(
    request: Request,
    event_type: str,
    *,
    status_code: int,
    detail: str | None = None,
    level: int = logging.WARNING,
) -> None:
    """Emit one structured security-event log line. Best-effort: never raises,
    so a logging failure can't break (or leak details into) the response."""
    try:
        payload = {
            "security_event": event_type,
            "status": status_code,
            "method": request.method,
            "path": request.url.path,  # path only — query string may carry PII
            "ip": client_ip(request),
            "ua": request.headers.get("user-agent"),
            "detail": detail,
        }
        _logger.log(level, "security_event %s", json.dumps(payload, default=str))
    except Exception:  # logging must never break the request path
        _logger.warning("security_event %s (payload unavailable)", event_type)
