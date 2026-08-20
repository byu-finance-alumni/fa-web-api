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

The unauthenticated pre-login routes have neither an actor nor a token, so they
use ``client_ip_rate_limiter(...)`` — client IP only. See the "#423" block.
"""

import hashlib
import time
from collections import OrderedDict, defaultdict
from typing import Annotated

from fastapi import Depends, HTTPException, Request, status

from app.api.dependencies.auth import (
    get_current_db_user_allow_must_change,
    require_alumni_edit,
    require_alumni_photos,
    require_engineer,
    require_interactions_create,
    require_super_admin,
)
from app.schemas.auth import UserContext

# Module-level state: {bucket_name: {actor_key: [timestamp, timestamp, ...]}}.
# The actor key is a user id for the authenticated limiters and an opaque string
# (client IP / hashed token) for the public ones.
# Timestamps are monotonic seconds; aged-out entries are pruned lazily on each
# check.
#
# Each bucket is an LRU (OrderedDict, most-recently-touched last) with a HARD
# CEILING on how many distinct actors it will remember — see
# :data:`_MAX_ACTORS_PER_BUCKET`.
_WINDOWS: dict[str, "OrderedDict[int | str, list[float]]"] = defaultdict(OrderedDict)

# The most distinct actors any one bucket will hold before the least recently
# seen are dropped.
#
# This ceiling exists because of the PUBLIC limiters below. While every limiter
# keyed on an authenticated ``user.user_id``, the key space was the staff account
# list — a couple of dozen entries that could never grow. ``public_token_rate_limiter``
# is the first one keyed on ATTACKER-CHOSEN input: the limiter runs as a route
# dependency, i.e. BEFORE the token is verified, so `GET /survey/respond/<random>`
# in a loop mints a brand-new key on every request without needing a valid
# credential. Unbounded, that is an anonymous memory-exhaustion DoS — and
# ``_WINDOWS`` is shared by every limiter in the app, so it would take the whole
# instance down with it, not just the survey routes.
#
# Evicting the least-recently-touched entry is safe for the thing that matters:
# a flood's per-IP key is touched on every single request, so it is always the
# freshest entry in its bucket and can never be the one evicted. What gets
# dropped is exactly the cold garbage the flood created.
_MAX_ACTORS_PER_BUCKET = 10_000

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

    Touching an actor moves it to the front of its bucket's LRU, and the bucket
    is trimmed to :data:`_MAX_ACTORS_PER_BUCKET` afterwards, so no caller can
    grow this dict without bound by presenting endless distinct keys.
    """
    now = time.monotonic()
    cutoff = now - window_seconds
    per_bucket = _WINDOWS[bucket]
    # Prune timestamps that have aged out of the window.
    hits = [t for t in per_bucket.get(actor_id, ()) if t > cutoff]
    over_budget = len(hits) >= limit
    if not over_budget:
        hits.append(now)
    # Re-seat the actor at the FRESH end either way: a blocked caller is the most
    # active one there is, so its window must not be evicted out from under it
    # (that would hand it a clean budget).
    per_bucket[actor_id] = hits
    per_bucket.move_to_end(actor_id)
    while len(per_bucket) > _MAX_ACTORS_PER_BUCKET:
        per_bucket.popitem(last=False)
    if over_budget:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=_TOO_MANY_REQUESTS_MESSAGE,
            headers={"Retry-After": str(int(window_seconds))},
        )


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
# Permanent user deletion is destructive and irreversible, so it gets the same
# tight budget as reset-password (#425). This used to be 20 on the theory that
# "bulk cleanup may be legitimate" — it isn't: the user directory is a couple of
# dozen staff accounts, so nobody ever deletes more than a handful in a sitting,
# and 20 was simply the loosest destructive budget in the file for the most
# irreversible call in it. Five still covers any real correction pass while
# narrowing what a runaway loop or a compromised session can wipe in one burst.
# Same in-process caveat as every limiter here (see the module docstring): this
# narrows the blast radius of one warm instance, it is not a global ceiling.
DELETE_USER_LIMITER = rate_limiter("admin:delete_user", limit=5, window_seconds=600)
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
# Turning maintenance mode ON is the most destructive single call in the app: it
# invalidates every non-engineer session at once and closes the site. A generous
# budget (an incident may legitimately involve a few flips) that still brakes a
# runaway loop or a compromised engineer token from thrashing the switch.
#
# THE *DISABLE* ENDPOINT IS DELIBERATELY NOT LIMITED. Turning maintenance OFF is
# the recovery path, and a limiter on it is itself a lockout: burn the budget —
# by accident, by a retry loop, or on purpose — and the site stays down for the
# length of the window with no way to bring it back. Throttling only the
# destructive direction keeps the brake where the damage is.
ENABLE_MAINTENANCE_LIMITER = rate_limiter(
    "maintenance:enable",
    limit=20,
    window_seconds=600,
    actor_guard=require_engineer,
)
# Revoking a live session ends someone's access and deletes their Supabase
# session row, so it gets a brake like the other destructive engineer actions.
# Budget sized for a real incident rather than a single correction: the user
# directory is a couple of dozen staff accounts, and an engineer working through
# "sign everyone out" during a credential-guessing scare legitimately fires this
# once per account in a few minutes. 30/10min covers that with room to spare
# while still braking a runaway loop or a compromised engineer token.
#
# ONLY the revoke is limited; GET /admin/sessions is not. Throttling the read
# would brake the screen an engineer uses to DECIDE what to revoke, which is the
# same lockout-shaped mistake as limiting the maintenance-mode *disable* route.
REVOKE_SESSION_LIMITER = rate_limiter(
    "admin:revoke_session",
    limit=30,
    window_seconds=600,
    actor_guard=require_engineer,
)

ResetPasswordRateLimit = Annotated[UserContext, Depends(RESET_PASSWORD_LIMITER)]
CreateUserRateLimit = Annotated[UserContext, Depends(CREATE_USER_LIMITER)]
AssignRoleRateLimit = Annotated[UserContext, Depends(ASSIGN_ROLE_LIMITER)]
DeleteUserRateLimit = Annotated[UserContext, Depends(DELETE_USER_LIMITER)]
RecordLoginRateLimit = Annotated[UserContext, Depends(RECORD_LOGIN_LIMITER)]
EnableMaintenanceRateLimit = Annotated[
    UserContext, Depends(ENABLE_MAINTENANCE_LIMITER)
]
RevokeSessionRateLimit = Annotated[UserContext, Depends(REVOKE_SESSION_LIMITER)]

# --- Alumni mutation routes (#112a) ------------------------------------------
#
# Per-endpoint brakes on the alumni write routes (interactions / tasks /
# employment create+edit+delete). Without these, only the platform WAF cap
# applied, so a write-capable role could script bulk edits/deletes. The actor is
# resolved through the SAME guard the route already uses (so authorization runs
# once and the identity stays server-trusted): interactions are gated on
# ``require_interactions_create`` (#379 — its own capability, seeded to every
# role, so a professor may still log their own), and tasks/employment are
# edit-tier (``require_alumni_edit``).
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
    actor_guard=require_interactions_create,
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
# alumni mutations; the managing gate is the ``alumni.photos`` capability (#379).
HEADSHOT_WRITE_LIMITER = rate_limiter(
    "alumni:headshot_write",
    limit=_MUTATION_LIMIT,
    window_seconds=_MUTATION_WINDOW,
    actor_guard=require_alumni_photos,
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
    actor_guard=require_alumni_photos,
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
#   this is the budget that tracks the thing being abused rather than the host
#   abusing it, and unlike the IP key it is not spoofable: reaching it at all
#   costs a valid HMAC, so header games cannot move a caller onto a fresh key.
#   That makes it the better-aimed of the two — NOT a ceiling. It is the same
#   in-process counter as everything else here, so it is per warm instance and
#   starts over at zero on a cold start; a leaked link replayed slowly enough,
#   or across enough instances, still gets through. It brakes a naive replay
#   flood, it does not stop a patient one.
# * per CLIENT IP — the broad one. Catches a single host working several tokens
#   at once. Deliberately loose, because alumni share egress addresses (one
#   employer's network, one campus, mobile CGNAT) and blocking a real alum is a
#   worse outcome than admitting a slow prober, who still has to hold a valid
#   HMAC to reach anything.
#
# Same in-process caveat as every limiter in this module (see the module
# docstring): per-instance, best-effort, not a hard boundary. The IP comes from
# :func:`_client_key`, which reads the hop the edge added rather than the one the
# caller supplied.

_SURVEY_WINDOW = 600.0


def _client_key(request: Request) -> str:
    """The client-IP key for a public limiter, read so a caller cannot choose it.

    Deliberately NOT ``security_log.client_ip``, which takes the LEFTMOST
    ``X-Forwarded-For`` hop. Leftmost is the right answer for a human reading a
    log line (it names the originating client) but the wrong one for a budget: a
    proxy chain APPENDS hops, so the leftmost value is whatever the caller put
    there. That would let an attacker rotate a fresh fake IP per request to dodge
    this budget entirely, and — worse — pin a REAL alum's or a whole employer's
    egress address and burn their budget on purpose, locking them out.

    So: take the hop the trusted edge itself added, which is the RIGHTMOST one,
    preferring Vercel's own header since nothing upstream of the edge can set it.
    A spoofed ``X-Forwarded-For`` then only lengthens the chain we ignore.

    The per-token budget is the better-aimed control regardless — it needs a
    valid HMAC, so no header games reach it (though it is best-effort and
    per-instance like everything else here). This is the loose second layer.
    """
    for header in ("x-vercel-forwarded-for", "x-forwarded-for"):
        raw = request.headers.get(header)
        if raw:
            last = raw.split(",")[-1].strip()
            if last:
                return last
    real = request.headers.get("x-real-ip")
    if real and real.strip():
        return real.strip()
    return request.client.host if request.client else "unknown"


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
        # Token budget first: it keys on the credential rather than the address,
        # so it is the better-aimed of the two and should decide the outcome
        # when both are near their cap. (Better-aimed, not unavoidable — it is
        # the same best-effort per-instance counter as every other limiter here;
        # see the module docstring.)
        _check(
            f"{bucket}:token",
            _token_key(token),
            limit=token_limit,
            window_seconds=window_seconds,
        )
        _check(
            f"{bucket}:ip",
            _client_key(request),
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
# Submitting opportunity links (#441). The SAME budget as the field submit, and
# deliberately its own bucket rather than a share of that one: they are two
# independent calls the survey page makes, so sharing a budget would mean a
# submit-then-fix-a-typo cycle on one form silently eating the other form's
# allowance. Each call can create up to `MAX_LINKS_PER_SUBMIT` rows in the
# moderation queue, so this is a queue-flood surface exactly like the field
# submit — 10 per token per ten minutes is well past any real alum and a hard
# brake on a replayed link.
OPPORTUNITY_LINK_SUBMIT_LIMITER = public_token_rate_limiter(
    "survey:respond_links", token_limit=10, ip_limit=60, window_seconds=_SURVEY_WINDOW
)


# --- Unauthenticated pre-login routes (#423) ---------------------------------
#
# `/auth/login/precheck` and `/auth/login/record` are the other pair of routes
# with no login — they run BEFORE the user has a session, so there is neither a
# `UserContext` nor a signed token to key on and neither limiter above applies.
# They carried NO application-level brake at all: the code accepted that on the
# grounds that the platform WAF rate-limits them, which cannot be verified from
# this repo. `/auth/login/record` with `success:false` upserts a `login_attempts`
# row keyed on the CALLER'S OWN email string and inserts a permanent
# `login_failures` row, so un-braked it was an anonymous, unbounded row-creation
# primitive as well as a lockout-DoS amplifier.
#
# KEYED ON CLIENT IP ONLY — deliberately NOT on the email:
#
#   * A per-email budget would be a lockout amplifier, not a brake: burning it
#     for a victim's address is exactly the denial of service the attacker wants.
#   * It would also break anti-enumeration. These routes are contractually
#     identical whatever email you send (see app/services/login_lockout.py); a
#     429 that depends on the email is a side channel that separates addresses.
#     Keying on the IP alone keeps the response a pure function of the caller,
#     never of the account.
#
# The IP comes from :func:`_client_key`, i.e. the hop the trusted edge added
# (RIGHTMOST), never the spoofable leftmost `X-Forwarded-For` value. The `context.
# ip_address` field in the request BODY is likewise NOT used as a key: it is
# caller-supplied, so it could be rotated to dodge the budget or pinned to a real
# user's address to burn theirs.
#
# ⚠️ TOPOLOGY CAVEAT — READ BEFORE RETUNING THESE NUMBERS.
# Both routes are called from a Next.js SERVER ACTION (fa-web-app
# src/app/login/actions.ts), i.e. server->server, never from the browser. The
# address this limiter sees for legitimate traffic is therefore the FRONTEND
# function's egress IP, not the signing-in human's — so every real login in the
# organisation funnels onto a handful of shared keys, while an attacker hitting
# this API directly (the cheap way to abuse it) gets keyed on their own address.
# Consequences:
#
#   * The budgets are sized for the AGGREGATE legitimate funnel, not per person.
#     They are a coarse ceiling on anonymous row creation, not a per-user brake.
#     A genuinely per-end-user control has to live at the edge/WAF, which is the
#     only layer that still sees the real client.
#   * They are set far above any plausible real volume on purpose. A false 429
#     on `/auth/login/record` would stop failures being COUNTED, i.e. it would
#     suppress the lockout — the defence, not the attack (an attacker guessing
#     passwords talks to Supabase directly and never calls this API). Throttling
#     the counter too eagerly would therefore be a security regression, so the
#     limit is set to catch only floods that are unambiguously not real traffic.
#
# Same in-process caveat as every limiter here (see the module docstring):
# per-instance, best-effort, not a hard boundary.

_LOGIN_WINDOW = 600.0


def client_ip_rate_limiter(bucket: str, *, limit: int, window_seconds: float):
    """Build a FastAPI dependency throttling an unauthenticated route by client
    IP alone.

    Used as a route-level ``dependencies=[...]`` entry rather than an injected
    ``Annotated`` parameter: there is no actor to hand back to the endpoint.
    Because it is a route dependency it runs BEFORE the request body is
    validated, so it cannot see — and can never vary by — the submitted email.
    """

    async def _dependency(request: Request) -> None:
        _check(
            bucket, _client_key(request), limit=limit, window_seconds=window_seconds
        )

    return _dependency


# Reading the throttle state. Read-only and the frontend FAILS OPEN on any
# non-OK response, so a 429 here costs nothing but a skipped pre-check; the
# loosest of the two.
LOGIN_PRECHECK_LIMIT = 600
# Recording an attempt. The one that WRITES: a `login_attempts` upsert plus a
# permanent `login_failures` row per failure. 300 per ten minutes is ~0.5/s of
# anonymous row creation from one address — orders of magnitude above the real
# funnel (a few dozen sign-ins a day across the whole directory) and still a hard
# ceiling where there was none. See the topology caveat above for why this is
# deliberately not tighter.
LOGIN_RECORD_LIMIT = 300

LOGIN_PRECHECK_LIMITER = client_ip_rate_limiter(
    "auth:login_precheck", limit=LOGIN_PRECHECK_LIMIT, window_seconds=_LOGIN_WINDOW
)
LOGIN_RECORD_LIMITER = client_ip_rate_limiter(
    "auth:login_record", limit=LOGIN_RECORD_LIMIT, window_seconds=_LOGIN_WINDOW
)
