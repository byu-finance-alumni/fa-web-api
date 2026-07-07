"""Request-scoped audit suppression for engineer actors (#199).

The engineer is a super-user / maintenance role. Its actions must NOT clutter
the FERPA audit trail, so no ``audit_logs`` row is written while an engineer is
the acting user. This module carries that decision for the current request in a
contextvar: the auth layer records it once per authenticated request (see
``app/api/dependencies/auth.py``), and a ``before_flush`` hook on the AuditLog
model reads it and drops any pending audit inserts (see ``app/models/audit.py``).

Using a contextvar -- rather than threading the actor's roles through every audit
callsite -- keeps the guard CENTRAL: it covers every existing and future write to
``audit_logs`` without touching the individual callsites. The value is re-set on
each authenticated request, and asyncio copies the context per request task, so
one request's value never leaks into another. It defaults to "not suppressed", so
if it is ever unset the audit trail is preserved (fail toward keeping the record).
"""

import contextvars
from collections.abc import Iterable

ENGINEER_ROLE = "engineer"

_actor_is_engineer: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "audit_actor_is_engineer", default=False
)


def set_audit_actor(roles: Iterable[str] | None) -> None:
    """Record whether the current request's actor is an engineer.

    Called once per authenticated request from the auth layer with the actor's
    role names, so the engineer-detection rule lives in exactly one place.
    """
    _actor_is_engineer.set(ENGINEER_ROLE in set(roles or ()))


def audit_suppressed() -> bool:
    """True when the current actor's ``audit_logs`` writes must be dropped (#199)."""
    return _actor_is_engineer.get()


def reset_audit_actor() -> None:
    """Reset to the default (not suppressed). Used by tests to isolate state."""
    _actor_is_engineer.set(False)
