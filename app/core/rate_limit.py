"""Best-effort, in-process rate limiting for destructive and public routes.

This is a simple fixed-window counter keyed by ``(bucket, actor_key)`` held in a
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

The public survey-respond routes have no logged-in actor to key on — the signed
token in the path is the whole credential — so they use
``public_token_rate_limiter(...)`` instead, which budgets by hashed token AND by
client IP. See the "#360" block at the bottom of this module.
"""

import hashlib
import time
from collections import defaultdict
from typing import Annotated

from fastapi import Depends, HTTPException, Request, status

from app.api.dependencies.auth import (
    get_current_db_user_allow_must_change,
    require_alumni_edit,
    require_full_access,
    require_super_admin,
    require_view_only,
)
from app.core.security_log import client_ip
from app.schemas.auth import UserContext

# Module-level state: {bucket_name: {actor_key: [timestamp, timestamp, ...]}}.
# The actor key is a user id for the authenticated limiters and an opaque string
# (client IP / hashed token) for the public ones.
# Timestamps are monotonic seconds; aged-out entries are pruned lazily on each
# check so the dict can't grow without bound for a steady caller.
_WINDOWS: dict[str, dict[int | str, list[float]]] = defaultdict(
    lambda: defaultdict(list)
)

# A plain, client-safe message. It is intentionally NOT pre-wrapped in the
# ``{"error": {...}}`` envelope: the app's ``StarletteHTTPException`` handler
# (see ``app/main.py``) turns any raised ``HTTPException`` into the standard
# envelope, deriving ``error.code`` from the 429 status. Passing a dict as
# ``detail`` here would double-nest it as ``{"detail": {"error": {...}}}``.
_TOO_MANY_REQUESTS_MESSAGE = "Too many requests; please slow down and retry later."


def _check(
    bucket: str, actor_id: int | str, *, limit: int, window_seconds: float
) -> None:
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
            detail=_TOO_MANY_REQUESTS_MESSAGE,
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
# Headshot direct-upload routes (mint signed URL + confirm). Each writes a DB
# audit row and makes an outbound Supabase call, so brake them like the other
# alumni mutations; full_access is the managing tier.
HEADSHOT_WRITE_LIMITER = rate_limiter(
    "alumni:headshot_write",
    limit=_MUTATION_LIMIT,
    window_seconds=_MUTATION_WINDOW,
    actor_guard=require_full_access,
)
# Bulk headshot import (#595) is chunked: image bytes go browser -> Supabase
# directly, and the client makes TWO small metadata calls (mint upload URLs, then
# confirm) per chunk of up to _HEADSHOT_BULK_MAX_PER_REQUEST (100) photos. Both
# calls share this bucket, so 100 requests buys 50 chunks = 5,000 images per
# window — half the old route's 10 x 1000 ceiling, and still five full
# 1,000-photo imports back to back, which is well past any real batch. Its own
# bucket, separate from the per-upload one: enough for legitimate re-runs, a hard
# brake on a loop / compromised session trying to churn the whole directory.
BULK_HEADSHOT_LIMITER = rate_limiter(
    "alumni:headshot_bulk",
    limit=100,
    window_seconds=600,
    actor_guard=require_full_access,
)

InteractionWriteRateLimit = Annotated[
    UserContext, Depends(INTERACTION_WRITE_LIMITER)
]
TaskWriteRateLimit = Annotated[UserContext, Depends(TASK_WRITE_LIMITER)]
EmploymentWriteRateLimit = Annotated[
    UserContext, Depends(EMPLOYMENT_WRITE_LIMITER)
]
HeadshotWriteRateLimit = Annotated[
    UserContext, Depends(HEADSHOT_WRITE_LIMITER)
]
BulkHeadshotRateLimit = Annotated[
    UserContext, Depends(BULK_HEADSHOT_LIMITER)
]

# --- Public, token-gated survey routes (#360) --------------------------------
#
# `/survey/respond/{token}` is the one surface here with NO login: the signed
# token IS the credential, so there is no `UserContext` to key on and
# `rate_limiter` above does not apply. These three routes were the only
# unauthenticated write path in the app and carried no brake at all — the WAF
# was the entire defence. Each submit mints a new survey_response row and
# unlocks another 20 MiB photo upload into the headshots bucket, so an
# un-braked token was a storage-fill and review-queue-flood primitive.
#
# Two independent budgets per request, both required:
#
# * per TOKEN — the precise one. A token addresses exactly one alum's record, so
#   this is what stops one leaked/forwarded link being replayed into a flood,
#   whatever address it is replayed from.
# * per CLIENT IP — the broad one. Catches a single host working several tokens
#   at once. Deliberately loose, because alumni share egress addresses (one
#   employer's network, one campus, mobile CGNAT) and blocking a real alum is a
#   worse outcome than admitting a slow prober, who still has to hold a valid
#   HMAC to reach anything.
#
# Same in-process caveat as every limiter in this module (see the module
# docstring): per-instance, best-effort, not a hard boundary. The IP is read
# from X-Forwarded-For, which only Vercel's edge can be trusted to set — a
# spoofed header evades the IP budget but NOT the per-token one.

_SURVEY_WINDOW = 600.0


def _token_key(token: str) -> str:
    """An opaque, stable key for a survey token.

    Hashed, not raw: this dict outlives the request, and a survey token is a live
    credential for one alum's PII — it does not belong sitting in process memory
    (or in a repr / traceback) in usable form. Truncated because collisions here
    would only merge two callers' budgets, not grant access."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()[:32]


def public_token_rate_limiter(
    bucket: str, *, token_limit: int, ip_limit: int, window_seconds: float
):
    """Build a FastAPI dependency throttling an unauthenticated, token-gated
    route by BOTH the path token and the client IP.

    Used as a route-level ``dependencies=[...]`` entry rather than an injected
    ``Annotated`` parameter (the shape the authenticated limiters use): there is
    no actor to hand back to the endpoint, so there is nothing to inject.
    """

    async def _dependency(request: Request, token: str) -> None:
        # Token budget first: it is the one an attacker cannot dodge, so it is
        # the one that should decide the outcome when both are near their cap.
        _check(
            f"{bucket}:token",
            _token_key(token),
            limit=token_limit,
            window_seconds=window_seconds,
        )
        _check(
            f"{bucket}:ip",
            client_ip(request) or "unknown",
            limit=ip_limit,
            window_seconds=window_seconds,
        )

    return _dependency


# Reading the confirm page. The loosest of the three: it is a read, and a real
# alum reloads, re-opens the link from the email, or comes back later. Still far
# below what enumeration would need — and enumeration needs a valid HMAC anyway.
SURVEY_RESPOND_READ_LIMITER = public_token_rate_limiter(
    "survey:respond_read", token_limit=30, ip_limit=300, window_seconds=_SURVEY_WINDOW
)
# Submitting. Each call stages a row in the staff review queue, so this is the
# self-suppression / queue-flood surface. Ten per token per ten minutes leaves
# plenty of room for an alum who submits, spots a typo and resubmits.
SURVEY_SUBMIT_LIMITER = public_token_rate_limiter(
    "survey:respond_submit", token_limit=10, ip_limit=60, window_seconds=_SURVEY_WINDOW
)
# Photo upload. The tightest, because it is the only one that moves real bytes
# (up to _HEADSHOT_MAX_BYTES each) into storage. A phone upload that fails and is
# retried a few times still fits.
SURVEY_PHOTO_LIMITER = public_token_rate_limiter(
    "survey:respond_photo", token_limit=5, ip_limit=30, window_seconds=_SURVEY_WINDOW
)
