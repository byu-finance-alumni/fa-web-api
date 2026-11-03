"""Pre-login throttling and lockout.

The frontend performs the actual Supabase password sign-in. Around it, it calls
this service to (a) refuse to even attempt a login that is currently throttled
(``check_login``) and (b) record the outcome of each attempt (``record_attempt``).
The throttle state lives in the ``login_attempts`` table.

Two layers, by failed-attempt count within a rolling window:

  * COOLDOWN (soft, time-boxed): at ``COOLDOWN_THRESHOLD`` failures we set a short
    ``cooldown_until``. Applies to ANY email — registered or not — and clears
    itself when the timer elapses. This is the only thing failure reports drive.

  * HARD LOCK (long, time-boxed) — NO LONGER ARMED BY FAILURE REPORTS (BT-1103-1,
    owner decision 2026-11-03). Failures reach this service through the
    UNAUTHENTICATED ``POST /auth/login/record``, which accepts any email from
    anyone, so a report is not evidence that the account's password was guessed.
    Letting those reports set ``users.locked_at`` meant a stranger could lock a
    staff member out for a day. Real password guessing goes to Supabase
    directly, which applies its own limits; the short cooldown here remains.

    Nothing in the application sets ``users.locked_at`` any more, but the
    infrastructure is kept (no schema change): a lock armed before this change
    is still honoured until it EXPIRES ``HARD_LOCK_DURATION`` (24 hours) after
    ``locked_at`` — every consumer asks :func:`is_lock_active` — and a
    super_admin password reset still clears it early. The auth resolver keeps
    enforcing it (403 / ``account_locked``, engineers exempt) and the admin user
    list keeps showing it, so a lock set by hand or by a future AUTHENTICATED
    signal works without further changes.

The rolling counter resets if the most recent failure is older than
``ATTEMPT_WINDOW_MINUTES`` — so sparse, occasional typos never accumulate into a
cooldown; only sustained bursts do.

Security tradeoffs (documented for the appsec review):

  * Account enumeration (hardened 2026-10-06): the two pre-login routes are
    UNAUTHENTICATED, so nothing they return may depend on whether the email is a
    real account or whether that account is hard-locked. Both public functions
    below derive their answer ONLY from the ``login_attempts`` counter row,
    which is keyed on the caller-supplied email string and behaves identically
    for a registered and an unregistered address. Neither looks the user up.
    A (pre-existing) hard lock is enforced after authentication instead
    (``get_current_db_user_allow_must_change`` refuses a locked account's token
    with 403 / ``account_locked``) — the only place the lock state can be
    revealed safely, since the caller has already proven the password.

  * Cooldown denial-of-service: the cooldown keys on the email, so anyone who
    knows an address can keep it in a cooldown with a steady trickle of reports.
    That is accepted: it is at most ``COOLDOWN_MINUTES`` at a time, it clears on
    its own, and it is bounded by the per-IP limiter and the #457 automatic
    source block on ``/auth/login/record``.
"""

from __future__ import annotations

import datetime
import math

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.login_attempt import LoginAttempt

# --- Thresholds (tunable policy constants) -----------------------------------
# Soft cooldown kicks in at this many failures within the window.
COOLDOWN_THRESHOLD = 10
# Length of the soft cooldown.
COOLDOWN_MINUTES = 5
# The rolling counter resets if the last failure is older than this.
ATTEMPT_WINDOW_MINUTES = 60
# How long a hard lock refuses the account before it expires on its own (owner
# decision, 2026-10-06). The single source for every consumer of ``locked_at``:
# the auth resolver and the admin user list. Nothing here arms a lock any more
# (see the module docstring); this governs locks that already exist.
HARD_LOCK_DURATION = datetime.timedelta(hours=24)

# The ``locked_reason`` recorded on locks armed before BT-1103-1. Kept so those
# rows (and tests that model them) stay meaningful; nothing writes it now.
LOCK_REASON_TOO_MANY_FAILED = "too_many_failed_logins"


def _now() -> datetime.datetime:
    return datetime.datetime.now(datetime.UTC)


def is_lock_active(
    locked_at: datetime.datetime | None, now: datetime.datetime | None = None
) -> bool:
    """True while a hard lock armed at ``locked_at`` is still in force.

    A NULL ``locked_at`` (never locked, or reset) and one older than
    ``HARD_LOCK_DURATION`` both read as unlocked. Ask this rather than testing
    ``locked_at`` for NULL — an expired timestamp is deliberately left on the row
    (no writer needs to run for the lock to lapse).
    """
    if locked_at is None:
        return False
    return (now or _now()) < locked_at + HARD_LOCK_DURATION


def _normalize(email: str) -> str:
    """Lowercase + strip so the throttle key is canonical and case-insensitive."""
    return email.strip().lower()


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

    On success the rolling counter is cleared. On failure the counter is
    upserted and may trip the cooldown. It NEVER arms, extends or re-arms a hard
    lock (``users.locked_at``) — see the module docstring — and it does not look
    the user up at all, so behaviour and query pattern are identical for every
    email.

    Returns the same shape as ``check_login``. Commits.
    """
    email_lc = _normalize(email)
    now = _now()

    if success:
        attempt = await _get_attempt(session, email_lc)
        if attempt is not None:
            await session.delete(attempt)
            await session.commit()
        return _status("ok", allowed=True, retry_after_seconds=None)

    # --- failure path --------------------------------------------------------
    attempt = await _get_attempt(session, email_lc)
    window = datetime.timedelta(minutes=ATTEMPT_WINDOW_MINUTES)

    if attempt is None:
        attempt = LoginAttempt(email_lc=email_lc, failed_count=0)
        session.add(attempt)
        attempt.first_failed_at = now
    elif attempt.last_failed_at is not None and (now - attempt.last_failed_at) > window:
        # Stale burst: the last failure predates the window — reset the counter
        # so sparse typos never accumulate into a cooldown.
        attempt.failed_count = 0
        attempt.first_failed_at = now
        attempt.cooldown_until = None

    attempt.failed_count += 1
    attempt.last_failed_at = now
    attempt.updated_at = now

    if attempt.failed_count >= COOLDOWN_THRESHOLD:
        attempt.cooldown_until = now + datetime.timedelta(minutes=COOLDOWN_MINUTES)

    await session.commit()

    if attempt.cooldown_until is not None and attempt.cooldown_until > now:
        retry_after = math.ceil((attempt.cooldown_until - now).total_seconds())
        return _status("cooldown", allowed=False, retry_after_seconds=retry_after)
    return _status("ok", allowed=True, retry_after_seconds=None)
