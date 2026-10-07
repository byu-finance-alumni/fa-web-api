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

The bulk-READ surfaces (alumni list/search, profile, notes, headshot URLs, the
named-alumni geography lists, and every CSV/export route) use
``read_rate_limiter(...)``: per authenticated user, several windows at once,
and a SECURITY alert the first time a user trips it. The export bucket is also
counted across instances from the audit trail. See the "read throttle" block.
"""

import asyncio
import datetime
import hashlib
import json
import logging
import time
from collections import OrderedDict, defaultdict
from typing import Annotated

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies.auth import (
    get_current_db_user_allow_must_change,
    require_alumni_edit,
    require_alumni_export,
    require_alumni_photos,
    require_engineer,
    require_interactions_create,
    require_reports_advanced,
    require_super_admin,
    require_view_only,
)
from app.core.config import get_settings
from app.core.database import get_session
from app.core.failure_monitor import route_template
from app.models.audit import AuditLog
from app.models.engineer_action import EngineerActionLog
from app.schemas.auth import UserContext
from app.services import failure_alert

log = logging.getLogger(__name__)
_security_log = logging.getLogger("security")

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
    _READ_ALERTED_AT.clear()


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
# The forced password change (#592) sets a real Supabase password server-side,
# so it is braked like the other credential-minting calls: 5 per user per 10
# minutes. Unlike the dependency-style limiters above it is checked INSIDE the
# route, AFTER the cheap refusals (no change pending -> 409, too short / the
# email -> 422), so a typo doesn't burn the budget. It is checked BEFORE the
# temp-password reuse check, though: that check answers "is this the current
# password?", which is exactly the guess a brake exists to ration.
def check_change_password_budget(user_id: int) -> None:
    """Spend one ``auth:change_password`` hit for *user_id*; 429 when over."""
    _check("auth:change_password", user_id, limit=5, window_seconds=600)


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

# --- Test alert (#457 follow-up) ---------------------------------------------
#
# Engineer-only, and it posts to a third party, so it is braked harder than the
# routes above: this is the one endpoint in the app whose entire job is to send a
# message somewhere else. Six an hour is plenty for "did the webhook I just
# rotated work" and nowhere near enough to be a way of spamming a channel.
TEST_ALERT_LIMITER = rate_limiter(
    "admin:test_alert",
    limit=6,
    window_seconds=3600,
    actor_guard=require_engineer,
)
TestAlertRateLimit = Annotated[UserContext, Depends(TEST_ALERT_LIMITER)]

# --- Alert message templates (2026-08-20) ------------------------------------
#
# Saving the wording of a Slack alert. Engineer-only through the same
# ``actor_guard`` the test-alert limiter uses, so the gate and the brake are one
# dependency and the identity stays server-trusted.
#
# LOOSER THAN TEST_ALERT_LIMITER, and the difference is the point: a test alert
# POSTS TO A THIRD PARTY on every call, so six an hour is the right ceiling on a
# channel-spamming primitive. Saving a template writes one short row in this
# database and sends nothing at all; the only thing it needs braking is a runaway
# loop or a compromised engineer token. 30 per ten minutes is far above someone
# iterating on a sentence and well below anything that could matter.
#
# ⚠️ ONLY THE SAVE IS LIMITED. Resetting a template to its built-in default is
# deliberately NOT, for the same reason the maintenance-mode *disable* route is
# not (see the block above it): reset is the recovery path from wording that
# broke the message, and a limiter on the way back is itself the failure. Brake
# the direction that does the damage.
ALERT_TEMPLATE_LIMITER = rate_limiter(
    "admin:alert_template",
    limit=30,
    window_seconds=600,
    actor_guard=require_engineer,
)
AlertTemplateRateLimit = Annotated[UserContext, Depends(ALERT_TEMPLATE_LIMITER)]

# --- Delete a login-abuse campaign (#457 follow-up) --------------------------
#
# Engineer-only, and the most destructive read-path cleanup in the console: one
# call deletes a source's whole trail across three tables — the per-attempt
# failures, the detector's incident row, and the block row, which un-blocks that
# source. There is no undo and nothing to restore from.
#
# Budgeted like the other irreversible engineer actions rather than like a read.
# Ten per ten minutes is sized for a real cleanup pass — the 2026-08-19 incident
# had THREE sources, and a test run leaves one — with headroom for a mistyped
# address, while hard-braking a runaway loop or a compromised engineer token
# trying to wipe the login telemetry wholesale. Same in-process caveat as every
# limiter here (see the module docstring): it narrows the blast radius of one
# warm instance, it is not a global ceiling. The forensic trail is the real
# control: every call writes an ``engineer_action_log`` row the engineer cannot
# delete, whether it removed anything or not.
DELETE_LOGIN_CAMPAIGN_LIMITER = rate_limiter(
    "admin:delete_login_campaign",
    limit=10,
    window_seconds=600,
    actor_guard=require_engineer,
)
DeleteLoginCampaignRateLimit = Annotated[
    UserContext, Depends(DELETE_LOGIN_CAMPAIGN_LIMITER)
]

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

# The PUBLIC, un-tokened contact lookup for the demo survey. There is no token
# to key on, so it is IP-only. Read-only and it returns one row of two fields,
# so it is generous -- but bounded, because unlike its token-gated sibling
# anyone can call it.
SURVEY_CONTACT_LIMITER = client_ip_rate_limiter(
    "survey:contact", limit=300, window_seconds=_SURVEY_WINDOW
)

LOGIN_PRECHECK_LIMITER = client_ip_rate_limiter(
    "auth:login_precheck", limit=LOGIN_PRECHECK_LIMIT, window_seconds=_LOGIN_WINDOW
)
LOGIN_RECORD_LIMITER = client_ip_rate_limiter(
    "auth:login_record", limit=LOGIN_RECORD_LIMIT, window_seconds=_LOGIN_WINDOW
)


# --- Bulk-read throttle (2026-10-02 breach review) ---------------------------
#
# Every limiter above brakes a WRITE or an unauthenticated route. The reads had
# nothing: one stolen view_only/student token could walk
# `/alumni/{id}/profile` 1..N, page the list, or loop `POST /alumni/export`
# (10,000 rows a call) for as long as the token lived. Every read is audited,
# but the audit log is something a person reads AFTER the fact — nothing
# stopped the loop and nothing told anyone it was happening.
#
# KEYED ON THE AUTHENTICATED USER ID — NEVER ON IP. The whole staff sits behind
# one campus NAT, so a per-IP budget is one shared budget for the department,
# and a per-IP limit has already locked staff out once (api#43, closed). The id
# comes from the same server-side guard the route already used, never from the
# request.
#
# TWO BUCKETS, so browsing a profile never spends export allowance and vice
# versa: `read:browse` (list/search, profile, notes, headshot URLs, the
# geography lists of named alumni) and `read:export` (every CSV / export
# route), the latter much tighter because a single call returns a whole
# population.
#
# TWO WINDOWS PER BUCKET. The short one stops a script within a minute of it
# starting (and raises the alert while it is still small); the long one stops a
# slow, patient walk that stays under the short one. A hit is only recorded
# when EVERY window has room, so a blocked call never pushes any window forward.
#
# ⚠️ HOW THE NUMBERS WERE CHOSEN — read before tightening them. The goal is that
# no human ever sees this; a 429 on a profile page reads as the app breaking.
# Counted from fa-web-app (no React Query — pages are server components that
# fetch on navigation, and `<Link>` prefetch of a dynamic route stops at its
# `loading.tsx`, so hovering a roster never fetches a profile):
#
#   * a profile view is 3 browse hits (profile + notes + headshot URL; the
#     headshot is Next-cached for 10 min, so often only 2);
#   * a roster page is 2 (list + ONE batched headshot call, also cached);
#   * the typeahead searches (topbar, quick-log, donation picker, map) are
#     debounced 250-400 ms, so a burst of typing is a handful of calls, not one
#     per keystroke;
#   * 429 is never auto-retried (`apiGetWithRetry` retries only 0/5xx).
#
# Clicking as fast as pages render (~1.5 s each) is ~40 navigations = ~120 hits
# a minute — a physical ceiling, not a working pace. 240/min is double that.
# 2,400/hour is a profile view every ~4.5 s for a full hour without a break,
# several times what a real outreach session does. Exports: a heavy session is
# a handful of list exports with different columns, a cohort template per class
# year, and an event roster or two; 20 per ten minutes (one every 30 s for ten
# minutes straight) and 60 an hour clear that comfortably, while a 10,000-row
# export loop is stopped at its 21st call.
#
# ENGINEER IS NOT EXEMPT, following precedent: no limiter in this module
# exempts a role (the engineer-only routes above are throttled too). The limits
# are set where no person reaches them, so an exemption would buy nothing but a
# role whose stolen token is unthrottled.
#
# Same in-process caveat as every limiter here (see the module docstring): the
# windows live in ONE warm instance's memory, so N instances allow up to N times
# the nominal budget and a cold start begins at zero. On Fluid Compute a single
# caller's sequential requests mostly reuse a warm instance, which is what makes
# this a real brake on a naive loop — but it is NOT a global ceiling for BROWSE.
#
# The EXPORT bucket IS global (2026-10-06): on top of the in-memory windows it
# counts the caller's own completed exports in the audit trail — every export
# route writes one ``export_*`` row per call (:data:`EXPORT_AUDIT_ACTIONS`) — so
# the same 10-minute / 1-hour budgets hold across every instance and survive a
# cold start. One COUNT per export call; see :func:`_recent_export_counts`.
# Browse gets no such check: it runs on every page view and its reads are not
# all audited, so a query per call there is cost without a reliable count.

_BROWSE_WINDOWS: tuple[tuple[int, float], ...] = ((240, 60.0), (2400, 3600.0))
_EXPORT_WINDOWS: tuple[tuple[int, float], ...] = ((20, 600.0), (60, 3600.0))

# One alert per (bucket, user) per this many seconds, per instance. Without it a
# runaway client that ignores 429 would page the channel once per request. An
# hour matches the longest window: if the same user is still tripping it an hour
# later, that IS news. Per-instance like everything here, so N warm instances can
# each send one — bounded, and the subject names the user so they group by eye.
_READ_ALERT_COOLDOWN_SECONDS = 3600.0
# {(bucket, user_id): monotonic time of the last alert}. Keyed on the
# authenticated user id, so it is bounded by the staff directory — no caller
# can mint keys here without a valid login.
_READ_ALERTED_AT: dict[tuple[str, int], float] = {}
# Bounded like the other alert deliveries (login_abuse uses 8 s): the blocked
# request waits at most this long, once per cooldown, and is answered 429 anyway.
_READ_ALERT_TIMEOUT_SECONDS = 8.0


def _check_windows(
    bucket: str, actor_id: int, windows: tuple[tuple[int, float], ...]
) -> None:
    """Record one hit in every window of ``bucket``, or raise 429 recording none.

    Each window is its own ``_WINDOWS`` bucket (``<bucket>:<seconds>s``) so the
    existing LRU / pruning in :func:`_check` applies unchanged. All windows are
    inspected BEFORE any is written: otherwise a call refused by the hour window
    would still spend the minute window's budget.
    """
    now = time.monotonic()
    for limit, window_seconds in windows:
        name = f"{bucket}:{int(window_seconds)}s"
        cutoff = now - window_seconds
        live = sum(1 for t in _WINDOWS[name].get(actor_id, ()) if t > cutoff)
        if live >= limit:
            # Raises 429 (and re-seats the actor in this window's LRU) without
            # recording the hit.
            _check(name, actor_id, limit=limit, window_seconds=window_seconds)
    for limit, window_seconds in windows:
        _check(
            f"{bucket}:{int(window_seconds)}s",
            actor_id,
            limit=limit,
            window_seconds=window_seconds,
        )


# The audit ``action_type`` every export route writes, one row per completed
# call. A NEW export route must add its action here, or it spends only the
# per-instance budget (tests/test_read_throttle.py scans the code for
# ``export_*`` actions and fails until it is listed).
EXPORT_AUDIT_ACTIONS: frozenset[str] = frozenset(
    {
        "export_alumni",  # POST /alumni/export + the cohort template
        "export_profile",  # GET /alumni/{id}/export
        "export_event_attendees",
        "export_survey_no_reply",  # both no-reply CSVs
        "export_opportunity_links",
    }
)


async def _recent_export_counts(
    session: AsyncSession, actor: UserContext, windows: tuple[tuple[int, float], ...]
) -> list[int]:
    """The caller's completed exports inside each of ``windows``, ACROSS ALL
    INSTANCES — one COUNT with a FILTER per window, over the longest window.

    Counted from the audit trail the export routes already write, so it needs no
    new table or migration. An engineer's audit rows are rerouted into
    ``engineer_action_log`` (#199), so theirs are counted there instead.

    ⚠️ INDEXES: neither table has a (user, time) composite index. ``audit_logs``
    is scanned through ``idx_audit_logs_created_at`` (the last hour of rows, all
    actors) and filtered on user/action; ``engineer_action_log`` has separate
    ``occurred_at`` and ``actor_user_id`` indexes. Fine at this staff size, but a
    bulk import's per-field audit rows land in that hour too — if this COUNT ever
    shows up as slow, an ``(user_id, created_at)`` index is the fix.
    """
    if actor.is_engineer:
        table, user_col, time_col = (
            EngineerActionLog,
            EngineerActionLog.actor_user_id,
            EngineerActionLog.occurred_at,
        )
    else:
        table, user_col, time_col = AuditLog, AuditLog.user_id, AuditLog.created_at
    now = datetime.datetime.now(datetime.UTC)
    longest = max(window_seconds for _limit, window_seconds in windows)
    stmt = select(
        *(
            func.count().filter(
                time_col >= now - datetime.timedelta(seconds=window_seconds)
            )
            for _limit, window_seconds in windows
        )
    ).where(
        user_col == actor.user_id,
        table.action_type.in_(sorted(EXPORT_AUDIT_ACTIONS)),
        time_col >= now - datetime.timedelta(seconds=longest),
    )
    row = (await session.execute(stmt)).one()
    return [int(n or 0) for n in row]


async def _global_export_trip(
    session: AsyncSession, actor: UserContext, windows: tuple[tuple[int, float], ...]
) -> tuple[int, float] | None:
    """The (limit, window) the caller's completed exports already fill, or None.

    FAILS OPEN on a database error: the per-instance windows still apply, and an
    export that cannot read the audit table cannot run its own query either, so
    refusing here would only change which error the caller sees.
    """
    try:
        counts = await _recent_export_counts(session, actor, windows)
    except Exception:  # noqa: BLE001 - fail open, see docstring
        log.warning("rate_limit: global export count failed; per-instance only")
        try:
            await session.rollback()
        except Exception:  # noqa: BLE001
            pass
        return None
    for (limit, window_seconds), count in zip(windows, counts, strict=True):
        if count >= limit:
            return limit, window_seconds
    return None


def _tripped_window(
    bucket: str, actor_id: int, windows: tuple[tuple[int, float], ...]
) -> tuple[int, float]:
    """The (limit, window) that refused this call — for the alert text only."""
    now = time.monotonic()
    for limit, window_seconds in windows:
        hits = _WINDOWS[f"{bucket}:{int(window_seconds)}s"].get(actor_id, ())
        if sum(1 for t in hits if t > now - window_seconds) >= limit:
            return limit, window_seconds
    return windows[0]


async def _alert_read_throttle(
    request: Request,
    bucket: str,
    actor_id: int,
    windows: tuple[tuple[int, float], ...],
    tripped: tuple[int, float] | None = None,
) -> None:
    """Log and alert, at most once per (bucket, user) per cooldown. Never raises.

    ⚠️ WHAT THE MESSAGE MAY CONTAIN: the user id, the bucket, the route TEMPLATE
    (``/alumni/{alumni_id}/profile`` — never the raw path, whose id points at a
    person), the limit and the window. No alumni data, no query string, no token.
    That is enough to act on: find the user in the console and revoke the
    session; the audit log has which records were read.

    Awaited, not fired-and-forgotten, for the reason recorded in
    ``opportunity_link_alert``: Vercel freezes the function once the response is
    written, so a detached task often never runs. The cost is one bounded wait on
    one already-refused request per cooldown.

    ``tripped`` is the (limit, window) when the caller already knows it (the
    global export count); otherwise it is read off the in-memory windows.

    A delivery that FAILS — raises, times out, is cancelled, or lands nowhere —
    releases the cooldown claim, so the next 429 retries the alert instead of
    the hour passing in silence. A cancellation is re-raised after the release.
    """
    now = time.monotonic()
    key = (bucket, actor_id)

    def _release_claim() -> None:
        # Only OUR claim: a newer one belongs to a send still in flight.
        if _READ_ALERTED_AT.get(key) == now:
            _READ_ALERTED_AT.pop(key, None)

    try:
        last = _READ_ALERTED_AT.get(key)
        if last is not None and now - last < _READ_ALERT_COOLDOWN_SECONDS:
            return
        # Claim before sending, same rule as failure_alert: a slow send must not
        # let the next 429 in behind it and send a second copy.
        _READ_ALERTED_AT[key] = now
        limit, window_seconds = tripped or _tripped_window(bucket, actor_id, windows)
        route = route_template(request)
        # The stdout security_event line (same shape as app/core/security_log.py,
        # but with the route TEMPLATE rather than the raw path). Once per
        # cooldown too, so a runaway client cannot flood the runtime logs.
        _security_log.warning(
            "security_event %s",
            json.dumps(
                {
                    "security_event": "read_throttled",
                    "status": 429,
                    "method": request.method,
                    "path": route,
                    "user_id": actor_id,
                    "bucket": bucket,
                    "limit": limit,
                    "window_seconds": int(window_seconds),
                }
            ),
        )
        if not failure_alert.alerting_enabled():
            return
        env = get_settings().environment
        subject = (
            f"[fa-web-api {env}] Read throttle: user {actor_id} hit the "
            f"{bucket} limit"
        )
        rows = [
            ("Environment", str(env)),
            ("User id", str(actor_id)),
            ("Bucket", bucket),
            ("Route", route),
            ("Limit", f"{limit} requests per {int(window_seconds)} s"),
            ("Action taken", "Further requests refused (429) until the window drains"),
        ]
        summary = (
            f"User {actor_id} hit the {bucket} read limit ({limit} per "
            f"{int(window_seconds)} s) on {route}. Further requests are refused. "
            "If this was not a person, revoke their session in the engineer console."
        )
        landed = await asyncio.wait_for(
            failure_alert.deliver_alert(
                subject,
                "One account read far more than any person browsing could. This "
                "is how a stolen token or a scraping script looks. Check who it "
                "is and whether they expect it; the audit log shows what was read.",
                rows,
                purpose=failure_alert.SECURITY,
                slack_summary=summary,
            ),
            timeout=_READ_ALERT_TIMEOUT_SECONDS,
        )
        if not landed:
            _release_claim()
            log.error("rate_limit: the read-throttle alert for %s landed nowhere", bucket)
    except asyncio.CancelledError:
        _release_claim()
        raise
    except Exception:  # noqa: BLE001 - the alert must never change the 429
        _release_claim()
        log.error("rate_limit: could not deliver the read-throttle alert for %s", bucket)


def read_rate_limiter(
    bucket: str,
    *,
    windows: tuple[tuple[int, float], ...],
    actor_guard=require_view_only,
    global_count: bool = False,
):
    """Build a FastAPI dependency throttling a bulk-READ route per user.

    Like :func:`rate_limiter` the actor is resolved through ``actor_guard`` (the
    route's own authorization), so gating and braking are one dependency and
    the key is server-trusted. Unlike it: several windows at once, and the
    first refusal per cooldown raises a SECURITY alert.

    Two factories built with the SAME ``bucket`` share one budget even with
    different guards — that is how the opportunity-link export (view access)
    and the alumni exports (``alumni.export``) count against one export limit.

    ``global_count`` (the export bucket only) adds the cross-instance check:
    after the in-memory windows pass, the caller's completed exports in the
    audit trail must also be under every window's limit
    (:func:`_global_export_trip`). Same 429, same alert.
    """

    async def _dependency(
        request: Request,
        actor: Annotated[UserContext, Depends(actor_guard)],
    ) -> UserContext:
        try:
            _check_windows(bucket, actor.user_id, windows)
        except HTTPException:
            await _alert_read_throttle(request, bucket, actor.user_id, windows)
            raise
        return actor

    if not global_count:
        return _dependency

    # A separate dependency so ONLY the export routes resolve a DB session here
    # (FastAPI caches it per request, so it is the route's own session).
    async def _global_dependency(
        request: Request,
        actor: Annotated[UserContext, Depends(_dependency)],
        session: Annotated[AsyncSession, Depends(get_session)],
    ) -> UserContext:
        tripped = await _global_export_trip(session, actor, windows)
        if tripped is not None:
            await _alert_read_throttle(
                request, bucket, actor.user_id, windows, tripped=tripped
            )
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail=_TOO_MANY_REQUESTS_MESSAGE,
                headers={"Retry-After": str(int(tripped[1]))},
            )
        return actor

    return _global_dependency


BROWSE_READ_LIMITER = read_rate_limiter("read:browse", windows=_BROWSE_WINDOWS)
# The geography drill-downs that list NAMED alumni (state / country / radius /
# city) are gated by ``reports.advanced``, so they resolve through that guard —
# but spend the SAME browse budget: paging a map drill-down is the same kind of
# walk as paging the directory.
GEO_BROWSE_READ_LIMITER = read_rate_limiter(
    "read:browse", windows=_BROWSE_WINDOWS, actor_guard=require_reports_advanced
)
EXPORT_READ_LIMITER = read_rate_limiter(
    "read:export",
    windows=_EXPORT_WINDOWS,
    actor_guard=require_alumni_export,
    global_count=True,
)
# The opportunity-link export is open to view access (it is the Links tab's own
# download), so it resolves through that guard — but spends the SAME export
# budget, because it is the same kind of call.
VIEW_EXPORT_READ_LIMITER = read_rate_limiter(
    "read:export",
    windows=_EXPORT_WINDOWS,
    actor_guard=require_view_only,
    global_count=True,
)

BrowseReadRateLimit = Annotated[UserContext, Depends(BROWSE_READ_LIMITER)]
GeoBrowseReadRateLimit = Annotated[UserContext, Depends(GEO_BROWSE_READ_LIMITER)]
ExportReadRateLimit = Annotated[UserContext, Depends(EXPORT_READ_LIMITER)]
ViewExportReadRateLimit = Annotated[UserContext, Depends(VIEW_EXPORT_READ_LIMITER)]
