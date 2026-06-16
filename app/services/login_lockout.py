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

  * Account enumeration: ``check_login`` / ``record_attempt`` internally
    distinguish ``cooldown`` (anyone) from ``locked`` (registered only). That
    distinction is a potential enumeration oracle — a ``locked`` reason only ever
    appears for a real account. We mitigate this by keeping the distinction
    INTERNAL: the frontend collapses both ``cooldown`` and ``locked`` into one
    generic "too many attempts, try later or contact an administrator" message,
    and the cooldown path itself applies to unregistered emails too, so timing /
    behavior do not cleanly separate registered from unregistered. The richer
    ``reason`` is returned for server-side logic/observability, not for display.

  * Lockout denial-of-service: because the hard lock keys on the (registered)
    email and not the attacker's IP, an attacker who knows a victim's email can
    deliberately burn failed attempts to lock that victim out until an admin
    resets it. This is an accepted, deliberate tradeoff (a sticky lock is the
    point); it is bounded by (1) the ``/auth/login/record`` endpoint being
    WAF-rate-limited, and (2) super_admin self-service reset. The cooldown layer
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

    Returns ``{"allowed": bool, "reason": "ok"|"cooldown"|"locked",
    "retry_after_seconds": int|None}``. Read-only: this never mutates state.

    Precedence: a hard lock on a registered account beats a cooldown. ``locked``
    has no retry-after (it does not self-clear; only an admin reset does).
    """
    email_lc = _normalize(email)
    now = _now()

    user = await _get_user_by_email_lc(session, email_lc)
    if user is not None and user.locked_at is not None:
        return _status("locked", allowed=False, retry_after_seconds=None)

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

    if locked:
        return {**_status("locked", allowed=False, retry_after_seconds=None), "locked": True}
    if attempt.cooldown_until is not None and attempt.cooldown_until > now:
        retry_after = math.ceil((attempt.cooldown_until - now).total_seconds())
        return {
            **_status("cooldown", allowed=False, retry_after_seconds=retry_after),
            "locked": False,
        }
    return {**_status("ok", allowed=True, retry_after_seconds=None), "locked": False}
