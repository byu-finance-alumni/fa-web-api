"""Unit tests for the pre-login throttle/lockout service.

These drive the real ``app/services/login_lockout.py`` logic against a minimal
in-memory fake session (no database). They assert the cooldown trips at
``COOLDOWN_THRESHOLD``, the hard lock trips at ``LOCK_THRESHOLD`` for a REGISTERED
email but never for an unknown one, ``check_login`` reflects both states, a
success resets the counter, and the rolling window reset works.

The coroutines are driven with ``asyncio.run`` (the project has no pytest-asyncio
plugin); the fake session is fully synchronous under the hood.
"""

import asyncio
import datetime
from types import SimpleNamespace

from app.services import login_lockout as ll

REGISTERED_EMAIL = "Alum@BYU.edu"  # mixed case on purpose (keying is case-insensitive)
UNKNOWN_EMAIL = "stranger@example.com"


def run(coro):
    return asyncio.run(coro)


def _now() -> datetime.datetime:
    return datetime.datetime.now(datetime.UTC)


class FakeSession:
    """In-memory stand-in for AsyncSession covering what the service uses.

    Holds a single optional registered ``User`` and a dict of ``LoginAttempt``
    rows keyed by ``email_lc``. ``scalar`` answers the case-insensitive user
    lookup; ``get`` answers the LoginAttempt primary-key lookup.
    """

    def __init__(self, user=None):
        self.user = user
        self.attempts: dict[str, object] = {}
        self.commits = 0

    async def scalar(self, _stmt):
        # The only scalar() query in the service is the user-by-lowercased-email
        # lookup. Return the seeded user regardless of statement internals.
        return self.user

    async def get(self, model, pk):
        if model is ll.LoginAttempt:
            return self.attempts.get(pk)
        return None

    def add(self, obj):
        if isinstance(obj, ll.LoginAttempt):
            self.attempts[obj.email_lc] = obj

    async def delete(self, obj):
        if isinstance(obj, ll.LoginAttempt):
            self.attempts.pop(obj.email_lc, None)

    async def commit(self):
        self.commits += 1


def _registered_user(email=REGISTERED_EMAIL, locked_at=None):
    return SimpleNamespace(
        user_id=2, email=email, locked_at=locked_at, locked_reason=None
    )


def _fail_n(session, email, n):
    last = None
    for _ in range(n):
        last = run(ll.record_attempt(session, email, success=False))
    return last


# --- cooldown -----------------------------------------------------------------


def test_cooldown_set_at_threshold():
    session = FakeSession(user=_registered_user())
    # One below threshold: still ok.
    status = _fail_n(session, REGISTERED_EMAIL, ll.COOLDOWN_THRESHOLD - 1)
    assert status["reason"] == "ok"
    assert status["locked"] is False
    # The threshold-th failure trips the cooldown.
    status = run(ll.record_attempt(session, REGISTERED_EMAIL, success=False))
    assert status["reason"] == "cooldown"
    assert status["allowed"] is False
    assert status["retry_after_seconds"] is not None
    assert 0 < status["retry_after_seconds"] <= ll.COOLDOWN_MINUTES * 60


def test_check_login_reflects_cooldown():
    session = FakeSession(user=_registered_user())
    _fail_n(session, REGISTERED_EMAIL, ll.COOLDOWN_THRESHOLD)
    status = run(ll.check_login(session, REGISTERED_EMAIL))
    assert status["reason"] == "cooldown"
    assert status["allowed"] is False
    assert status["retry_after_seconds"] is not None


# --- hard lock ----------------------------------------------------------------


def test_hard_lock_set_for_registered_email_at_lock_threshold():
    user = _registered_user()
    session = FakeSession(user=user)
    # Below the lock threshold: not locked (cooldown only).
    status = _fail_n(session, REGISTERED_EMAIL, ll.LOCK_THRESHOLD - 1)
    assert status["locked"] is False
    assert user.locked_at is None
    # The lock-threshold-th failure hard-locks the registered account.
    status = run(ll.record_attempt(session, REGISTERED_EMAIL, success=False))
    assert status["locked"] is True
    assert status["reason"] == "locked"
    assert status["retry_after_seconds"] is None
    assert user.locked_at is not None
    assert user.locked_reason == ll.LOCK_REASON_TOO_MANY_FAILED


def test_unknown_email_is_never_hard_locked():
    # No registered user backing this email.
    session = FakeSession(user=None)
    status = _fail_n(session, UNKNOWN_EMAIL, ll.LOCK_THRESHOLD + 5)
    # Cooldown still applies to an unknown email, but it is NEVER hard-locked.
    assert status["locked"] is False
    assert status["reason"] == "cooldown"


def test_check_login_reflects_lock():
    user = _registered_user(locked_at=_now())
    session = FakeSession(user=user)
    status = run(ll.check_login(session, REGISTERED_EMAIL))
    assert status["reason"] == "locked"
    assert status["allowed"] is False
    assert status["retry_after_seconds"] is None


def test_lock_takes_precedence_over_cooldown_in_check():
    # Locked AND within a cooldown window -> check reports the (stickier) lock.
    user = _registered_user()
    session = FakeSession(user=user)
    _fail_n(session, REGISTERED_EMAIL, ll.LOCK_THRESHOLD)
    assert user.locked_at is not None
    status = run(ll.check_login(session, REGISTERED_EMAIL))
    assert status["reason"] == "locked"


# --- success reset ------------------------------------------------------------


def test_success_resets_counter():
    session = FakeSession(user=_registered_user())
    _fail_n(session, REGISTERED_EMAIL, ll.COOLDOWN_THRESHOLD)
    assert session.attempts  # a row exists
    status = run(ll.record_attempt(session, REGISTERED_EMAIL, success=True))
    assert status["reason"] == "ok"
    assert status["allowed"] is True
    assert not session.attempts  # row deleted
    # And a subsequent check is clean.
    assert run(ll.check_login(session, REGISTERED_EMAIL))["reason"] == "ok"


# --- window reset -------------------------------------------------------------


def test_window_reset_drops_stale_count():
    session = FakeSession(user=_registered_user())
    # Accumulate up to (but not tripping) the cooldown.
    _fail_n(session, REGISTERED_EMAIL, ll.COOLDOWN_THRESHOLD - 1)
    row = session.attempts[REGISTERED_EMAIL.lower()]
    assert row.failed_count == ll.COOLDOWN_THRESHOLD - 1

    # Age the last failure beyond the rolling window.
    stale = _now() - datetime.timedelta(minutes=ll.ATTEMPT_WINDOW_MINUTES + 1)
    row.last_failed_at = stale

    # The next failure resets the counter to 0 first, so it is now 1 (not the
    # threshold) and does NOT trip the cooldown.
    status = run(ll.record_attempt(session, REGISTERED_EMAIL, success=False))
    assert row.failed_count == 1
    assert status["reason"] == "ok"


def test_email_keying_is_case_insensitive():
    session = FakeSession(user=_registered_user())
    run(ll.record_attempt(session, "Alum@BYU.edu", success=False))
    run(ll.record_attempt(session, "alum@byu.edu", success=False))
    # Both failures land on the same lowercased key.
    assert set(session.attempts) == {"alum@byu.edu"}
    assert session.attempts["alum@byu.edu"].failed_count == 2
