"""Best-effort, in-process per-actor rate limiting for destructive admin routes.

This is a simple fixed-window counter keyed by ``(bucket, actor_id)`` held in a
module-level dict. It is intentionally lightweight and dependency-free.

SERVERLESS CAVEAT: the counter lives in process memory, so on a serverless /
multi-instance deployment (e.g. Vercel) each instance keeps its OWN window and a
determined caller spread across instances could exceed the nominal limit. It is
therefore a best-effort brake against accidental floods and casual abuse from a
single warm instance — NOT a hard security boundary. The real guard rails are
the super_admin authz gate, the audit log, and the platform WAF rate limiting.
A shared store (Redis/Postgres) would be required for a strict global limit.

Each limiter is exposed as a FastAPI dependency factory (``rate_limiter(...)``)
that is added directly to a route signature; it resolves the acting user via the
same ``require_super_admin`` guard the routes already use, so it never trusts a
client-supplied identity. Exceeding the limit raises HTTP 429.
"""

import time
from collections import defaultdict
from typing import Annotated

from fastapi import Depends, HTTPException, status

from app.api.dependencies.auth import (
    get_current_db_user_allow_must_change,
    require_alumni_edit,
    require_super_admin,
    require_view_only,
)
from app.schemas.auth import UserContext

# Module-level state: {bucket_name: {actor_id: [timestamp, timestamp, ...]}}.
# Timestamps are monotonic seconds; aged-out entries are pruned lazily on each
# check so the dict can't grow without bound for a steady caller.
_WINDOWS: dict[str, dict[int, list[float]]] = defaultdict(lambda: defaultdict(list))

_TOO_MANY_REQUESTS = {
    "error": {
        "code": "rate_limited",
        "message": "Too many requests; please slow down and retry later.",
    }
}


def _check(bucket: str, actor_id: int, *, limit: int, window_seconds: float) -> None:
    """Record one hit for ``actor_id`` in ``bucket`` and raise 429 if over ``limit``.

    Fixed-window: count the actor's hits inside the trailing ``window_seconds``;
    if that count (including this one) would exceed ``limit``, raise 429 WITHOUT
    recording the hit (so a blocked caller can't push their own window forward).
    """
    now = time.monotonic()
    cutoff = now - window_seconds
    hits = _WINDOWS[bucket][actor_id]
    # Prune timestamps that have aged out of the window.
    hits[:] = [t for t in hits if t > cutoff]
    if len(hits) >= limit:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=_TOO_MANY_REQUESTS,
            headers={"Retry-After": str(int(window_seconds))},
        )
    hits.append(now)


def reset() -> None:
    """Clear all rate-limit state. For tests only."""
    _WINDOWS.clear()


def rate_limiter(
    bucket: str, *, limit: int, window_seconds: float, actor_guard=require_super_admin
):
    """Build a FastAPI dependency enforcing ``limit`` hits per ``window_seconds``
    per actor for the named ``bucket``.

    The dependency resolves the actor through ``actor_guard`` (default
    ``require_super_admin``, so the route stays gated and the identity is
    server-trusted), records the hit, and raises HTTP 429 when the actor is over
    budget. Pass a different guard (e.g. ``get_current_db_user_allow_must_change``)
    to throttle a route open to any authenticated user — the actor is still
    resolved server-side, never from client input.
    """

    async def _dependency(
        actor: Annotated[UserContext, Depends(actor_guard)],
    ) -> UserContext:
        _check(bucket, actor.user_id, limit=limit, window_seconds=window_seconds)
        return actor

    return _dependency


# Destructive-admin limits. Reset-password is the most sensitive (it mints a
# usable credential), so it gets the tightest budget; create_user and
# assign_role are throttled a little more loosely.
RESET_PASSWORD_LIMITER = rate_limiter(
    "admin:reset_password", limit=5, window_seconds=600
)
CREATE_USER_LIMITER = rate_limiter("admin:create_user", limit=10, window_seconds=600)
ASSIGN_ROLE_LIMITER = rate_limiter("admin:assign_role", limit=30, window_seconds=600)
# Permanent user deletion is destructive and irreversible. A generous cap (bulk
# cleanup may be legitimate) that still brakes a runaway loop / compromised
# session from wiping the directory in one burst.
DELETE_USER_LIMITER = rate_limiter("admin:delete_user", limit=20, window_seconds=600)
# Login recording is open to ANY authenticated user (they can only record their
# OWN login), so it's gated by the force-change-exempt resolver rather than an
# admin guard. No human logs in 10 times in 10 minutes; this just brakes a
# compromised/looping session from flooding login_events.
RECORD_LOGIN_LIMITER = rate_limiter(
    "auth:record_login",
    limit=10,
    window_seconds=600,
    actor_guard=get_current_db_user_allow_must_change,
)

ResetPasswordRateLimit = Annotated[UserContext, Depends(RESET_PASSWORD_LIMITER)]
CreateUserRateLimit = Annotated[UserContext, Depends(CREATE_USER_LIMITER)]
AssignRoleRateLimit = Annotated[UserContext, Depends(ASSIGN_ROLE_LIMITER)]
DeleteUserRateLimit = Annotated[UserContext, Depends(DELETE_USER_LIMITER)]
RecordLoginRateLimit = Annotated[UserContext, Depends(RECORD_LOGIN_LIMITER)]

# --- Alumni mutation routes (#112a) ------------------------------------------
#
# Per-endpoint brakes on the alumni write routes (interactions / tasks /
# employment create+edit+delete). Without these, only the platform WAF cap
# applied, so a write-capable role could script bulk edits/deletes. The actor is
# resolved through the SAME guard the route already uses (so authorization runs
# once and the identity stays server-trusted): interactions are open to every
# authenticated role (``require_view_only`` — a professor may log their own), and
# tasks/employment are edit-tier (``require_alumni_edit``).
#
# Limits are tuned for normal human editing: 30 writes / minute is far above a
# person clicking through a profile, but brakes a runaway loop / compromised
# session. The same window covers create, edit, and delete on each resource so a
# burst of mixed mutations is throttled as one stream.
_MUTATION_LIMIT = 30
_MUTATION_WINDOW = 60.0

INTERACTION_WRITE_LIMITER = rate_limiter(
    "alumni:interaction_write",
    limit=_MUTATION_LIMIT,
    window_seconds=_MUTATION_WINDOW,
    actor_guard=require_view_only,
)
TASK_WRITE_LIMITER = rate_limiter(
    "alumni:task_write",
    limit=_MUTATION_LIMIT,
    window_seconds=_MUTATION_WINDOW,
    actor_guard=require_alumni_edit,
)
EMPLOYMENT_WRITE_LIMITER = rate_limiter(
    "alumni:employment_write",
    limit=_MUTATION_LIMIT,
    window_seconds=_MUTATION_WINDOW,
    actor_guard=require_alumni_edit,
)

InteractionWriteRateLimit = Annotated[
    UserContext, Depends(INTERACTION_WRITE_LIMITER)
]
TaskWriteRateLimit = Annotated[UserContext, Depends(TASK_WRITE_LIMITER)]
EmploymentWriteRateLimit = Annotated[
    UserContext, Depends(EMPLOYMENT_WRITE_LIMITER)
]
