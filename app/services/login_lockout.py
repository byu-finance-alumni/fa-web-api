"""Pre-login throttling and lockout.

The frontend performs the actual Supabase password sign-in. Around it, it calls
this service to (a) refuse to even attempt a login that is currently throttled
(``check_login``) and (b) record the outcome of each attempt (``record_attempt``).
The authoritative throttle/lock state lives in the database (the
``login_attempts`` table and ``users.locked_at`` / ``users.locked_reason``); this
module is the only writer.

Two layered defenses, by failed-attempt count within a rolling window:

  * COOLDOWN (soft, time-boxed): at ``COOLDOWN_THRESHOLD`` failures we set a short
    ``cooldown_until``. Applies to ANY email — registered or not — and clears
    itself when the timer elapses. This is the first-line brake on online
    password guessing.

  * HARD LOCK (sticky): at ``LOCK_THRESHOLD`` failures, AND only when the email
    belongs to a REGISTERED user, we set ``users.locked_at``. This does not
    clear on its own — a super_admin must reset the password (which clears it).
    Unregistered emails are never hard-locked (there is no account to lock).

The rolling counter resets if the most recent failure is older than
``ATTEMPT_WINDOW_MINUTES`` — so sparse, occasional typos never accumulate into a
lock; only sustained bursts do.

Security tradeoffs (documented for the appsec review):

  * Account enumeration (hardened 2026-10-06): the two pre-login routes are
    UNAUTHENTICATED, so nothing they return may depend on whether the email is a
    real account or whether that account is hard-locked. Earlier versions
    returned a ``locked`` reason (registered accounts only, no retry timer) and
    relied on the frontend collapsing it into the cooldown message — but the raw
    JSON is visible to anyone who calls the API directly, so ``locked`` vs
    ``cooldown`` told an anonymous caller "this is a real staff account, and it
    is locked". Both public functions below now derive their answer ONLY from
    the ``login_attempts`` counter row, which is keyed on the caller-supplied
    email string and behaves identically for a registered and an unregistered
    address. ``check_login`` does not look the user up at all.

    The hard lock is still set exactly as before; it is ENFORCED after
    authentication instead (``get_current_db_user_allow_must_change`` refuses a
    locked account's token with 403 / ``account_locked``). That is the only
    place the lock state can be revealed safely — the caller has already proven
    the password — and it also closes the gap where a locked user who signed in
    directly against Supabase was never refused by the API at all.
    ``record_attempt`` still reports the lock transition as the internal
    ``"locked"`` boolean for server-side callers; routes must not echo it.

  * Lockout denial-of-service: because the hard lock keys on the (registered)
    email and not the attacker's IP, an attacker who knows a victim's email can
    deliberately burn failed attempts to lock that victim out until an admin
    resets it. This is an accepted, deliberate tradeoff (a sticky lock is the
    point); it is bounded by (1) the per-IP limiter and the #457 automatic
    source block on ``/auth/login/record``, and (2) super_admin self-service
    reset. Engineers are exempt from lock ENFORCEMENT (see the auth resolver):
    no other role may reset an engineer's password (#178 ceiling), so an
    enforced lock on the engineer would have no recovery path. The cooldown layer
    alone (which auto-clears) handles the common typo case without admin
    involvement.
"""

from __future__ import annotations

import datetime
import math

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.login_attempt import LoginAttempt
from app.models.user import User

# --- Thresholds (tunable policy constants) -----------------------------------
# Soft cooldown kicks in at this many failures within the window.
COOLDOWN_THRESHOLD = 10
# Length of the soft cooldown.
COOLDOWN_MINUTES = 5
# Hard lock (registered emails only) at this many failures within the window.
LOCK_THRESHOLD = 20
# The rolling counter resets if the last failure is older than this.
ATTEMPT_WINDOW_MINUTES = 60

LOCK_REASON_TOO_MANY_FAILED = "too_many_failed_logins"


def _now() -> datetime.datetime:
    return datetime.datetime.now(datetime.UTC)


def _normalize(email: str) -> str:
    """Lowercase + strip so the throttle key is canonical and case-insensitive."""
    return email.strip().lower()


async def _get_user_by_email_lc(session: AsyncSession, email_lc: str) -> User | None:
    """Look up a registered user by lowercased email (case-insensitive)."""
    return await session.scalar(
        select(User).where(func.lower(User.email) == email_lc)
    )


async def _get_attempt(session: AsyncSession, email_lc: str) -> LoginAttempt | None:
    return await session.get(LoginAttempt, email_lc)


def _status(
    reason: str, *, allowed: bool, retry_after_seconds: int | None
) -> dict:
    return {
        "allowed": allowed,
        "reason": reason,
        "retry_after_seconds": retry_after_seconds,
    }


async def check_login(session: AsyncSession, email: str) -> dict:
    """Decide whether a login attempt for ``email`` may proceed right now.

    Returns ``{"allowed": bool, "reason": "ok"|"cooldown",
    "retry_after_seconds": int|None}``. Read-only: this never mutates state.

    Answers from the ``login_attempts`` counter ONLY and never looks the user
    up, so the result — and the query pattern behind it — is the same for an
    unknown address, a real account, and a hard-locked one (see the
    anti-enumeration note at the top of this module). A hard-locked account is
    refused after authentication, not here.
    """
    email_lc = _normalize(email)
    now = _now()

    attempt = await _get_attempt(session, email_lc)
    if (
        attempt is not None
        and attempt.cooldown_until is not None
        and attempt.cooldown_until > now
    ):
        retry_after = math.ceil((attempt.cooldown_until - now).total_seconds())
        return _status("cooldown", allowed=False, retry_after_seconds=retry_after)

    return _status("ok", allowed=True, retry_after_seconds=None)


async def record_attempt(
    session: AsyncSession, email: str, success: bool
) -> dict:
    """Record the outcome of a login attempt and return the resulting status.

    On success the rolling counter is cleared (a successful login can only have
    happened when the account was neither locked nor cooled). On failure the
    counter is upserted and may trip the cooldown and/or the hard lock.

    Returns the same shape as ``check_login`` plus ``"locked": bool``. Commits.

    The public part (``allowed`` / ``reason`` / ``retry_after_seconds``) is
    derived from the counter row alone, exactly like ``check_login``: the
    failure that trips the hard lock reports the cooldown it also arms (the lock
    threshold is above the cooldown threshold, so one is always live), never a
    ``locked`` reason. ``"locked"`` is the internal signal that THIS failure
    left a registered account hard-locked; callers must not echo it.
    """
    email_lc = _normalize(email)
    now = _now()

    if success:
        attempt = await _get_attempt(session, email_lc)
        if attempt is not None:
            await session.delete(attempt)
            await session.commit()
        return {**_status("ok", allowed=True, retry_after_seconds=None), "locked": False}

    # --- failure path --------------------------------------------------------
    attempt = await _get_attempt(session, email_lc)
    window = datetime.timedelta(minutes=ATTEMPT_WINDOW_MINUTES)

    if attempt is None:
        attempt = LoginAttempt(email_lc=email_lc, failed_count=0)
        session.add(attempt)
        attempt.first_failed_at = now
    elif attempt.last_failed_at is not None and (now - attempt.last_failed_at) > window:
        # Stale burst: the last failure predates the window — reset the counter
        # so sparse typos never accumulate into a lock.
        attempt.failed_count = 0
        attempt.first_failed_at = now
        attempt.cooldown_until = None

    attempt.failed_count += 1
    attempt.last_failed_at = now
    attempt.updated_at = now

    if attempt.failed_count >= COOLDOWN_THRESHOLD:
        attempt.cooldown_until = now + datetime.timedelta(minutes=COOLDOWN_MINUTES)

    locked = False
    user = await _get_user_by_email_lc(session, email_lc)
    if user is not None and attempt.failed_count >= LOCK_THRESHOLD:
        # Hard lock — registered accounts only. Idempotent: keep the original
        # lock timestamp if already locked.
        if user.locked_at is None:
            user.locked_at = now
            user.locked_reason = LOCK_REASON_TOO_MANY_FAILED
        locked = True

    await session.commit()

    if attempt.cooldown_until is not None and attempt.cooldown_until > now:
        retry_after = math.ceil((attempt.cooldown_until - now).total_seconds())
        return {
            **_status("cooldown", allowed=False, retry_after_seconds=retry_after),
            "locked": locked,
        }
    return {**_status("ok", allowed=True, retry_after_seconds=None), "locked": locked}
