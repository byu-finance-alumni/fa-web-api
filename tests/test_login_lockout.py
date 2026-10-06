"""Unit tests for the pre-login throttle/lockout service.

These drive the real ``app/services/login_lockout.py`` logic against a minimal
in-memory fake session (no database). They assert the cooldown trips at
``COOLDOWN_THRESHOLD``, the hard lock trips at ``LOCK_THRESHOLD`` for a REGISTERED
email but never for an unknown one, ``check_login`` reflects the cooldown but
NEVER the lock (anti-enumeration: the lock is enforced after authentication), a
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
    # The lock-threshold-th failure hard-locks the registered account...
    status = run(ll.record_attempt(session, REGISTERED_EMAIL, success=False))
    assert status["locked"] is True
    assert user.locked_at is not None
    assert user.locked_reason == ll.LOCK_REASON_TOO_MANY_FAILED
    # ...but the PUBLIC part of the status is the counter's cooldown, exactly
    # what an unknown address gets on the same failure count — never `locked`.
    assert status["reason"] == "cooldown"
    assert status["allowed"] is False
    assert 0 < status["retry_after_seconds"] <= ll.COOLDOWN_MINUTES * 60


def test_lock_threshold_is_above_cooldown_threshold():
    """The failure that arms the lock reports the cooldown it also arms; that is
    only true while the lock needs MORE failures than the cooldown does."""
    assert ll.LOCK_THRESHOLD > ll.COOLDOWN_THRESHOLD


def test_unknown_email_is_never_hard_locked():
    # No registered user backing this email.
    session = FakeSession(user=None)
    status = _fail_n(session, UNKNOWN_EMAIL, ll.LOCK_THRESHOLD + 5)
    # Cooldown still applies to an unknown email, but it is NEVER hard-locked.
    assert status["locked"] is False
    assert status["reason"] == "cooldown"


def test_public_status_is_identical_for_registered_and_unknown_past_the_lock():
    """Anti-enumeration at the service layer: drive both well past the lock
    threshold; every public status must match, failure for failure."""
    registered = FakeSession(user=_registered_user())
    unknown = FakeSession(user=None)
    for _ in range(ll.LOCK_THRESHOLD + 3):
        a = run(ll.record_attempt(registered, REGISTERED_EMAIL, success=False))
        b = run(ll.record_attempt(unknown, UNKNOWN_EMAIL, success=False))
        a.pop("locked")
        b.pop("locked")
        assert a == b
    assert registered.user.locked_at is not None  # the lock itself still armed


class _CountingSession(FakeSession):
    def __init__(self, user=None):
        super().__init__(user=user)
        self.user_lookups = 0

    async def scalar(self, _stmt):
        self.user_lookups += 1
        return self.user


def test_check_login_never_reveals_a_lock():
    """A hard-locked account with no live cooldown answers `ok`, exactly like an
    address that has never existed — and the user is not even looked up, so the
    query pattern is the same too. The lock is enforced after authentication."""
    locked = _CountingSession(user=_registered_user(locked_at=_now()))
    unknown = _CountingSession(user=None)
    assert run(ll.check_login(locked, REGISTERED_EMAIL)) == run(
        ll.check_login(unknown, UNKNOWN_EMAIL)
    ) == {"allowed": True, "reason": "ok", "retry_after_seconds": None}
    assert locked.user_lookups == unknown.user_lookups == 0


def test_locked_and_cooling_reports_only_the_cooldown():
    # Locked AND within a cooldown window -> the counter's cooldown, never `locked`.
    user = _registered_user()
    session = FakeSession(user=user)
    _fail_n(session, REGISTERED_EMAIL, ll.LOCK_THRESHOLD)
    assert user.locked_at is not None
    status = run(ll.check_login(session, REGISTERED_EMAIL))
    assert status["reason"] == "cooldown"
    assert status["allowed"] is False
    assert status["retry_after_seconds"] is not None


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


# --- expiry and re-lock (HARD_LOCK_DURATION) ----------------------------------


def _ago(**kw) -> datetime.datetime:
    return _now() - datetime.timedelta(**kw)


def test_is_lock_active_boundaries():
    now = _now()
    assert ll.is_lock_active(None, now) is False
    assert ll.is_lock_active(now - datetime.timedelta(hours=23, minutes=59), now)
    assert not ll.is_lock_active(now - ll.HARD_LOCK_DURATION, now)
    assert not ll.is_lock_active(now - datetime.timedelta(hours=25), now)


def test_more_failures_do_not_extend_a_live_lock():
    original = _ago(hours=2)
    user = _registered_user(locked_at=original)
    session = FakeSession(user=user)
    status = _fail_n(session, REGISTERED_EMAIL, ll.LOCK_THRESHOLD + 5)
    assert status["locked"] is True
    assert user.locked_at == original


def test_expired_lock_re_locks_after_a_fresh_burst_with_a_new_timestamp():
    stale = _ago(hours=30)
    user = _registered_user(locked_at=stale)
    session = FakeSession(user=user)
    status = _fail_n(session, REGISTERED_EMAIL, ll.LOCK_THRESHOLD - 1)
    assert status["locked"] is False
    assert user.locked_at == stale  # not re-armed yet
    status = run(ll.record_attempt(session, REGISTERED_EMAIL, success=False))
    assert status["locked"] is True
    assert user.locked_at is not None and user.locked_at > stale
    assert ll.is_lock_active(user.locked_at)
    assert user.locked_reason == ll.LOCK_REASON_TOO_MANY_FAILED


def test_one_failure_after_expiry_does_not_instantly_re_lock():
    """An attacker hammering all day keeps the rolling counter past the lock
    threshold; the first failure after the 24h runs out must restart that burst
    instead of re-locking on the spot."""
    stale = _ago(hours=24, minutes=5)
    user = _registered_user(locked_at=stale)
    session = FakeSession(user=user)
    # A burst that started before the lock expired and is still inside the
    # rolling window (last failure a minute ago).
    session.attempts[REGISTERED_EMAIL.lower()] = ll.LoginAttempt(
        email_lc=REGISTERED_EMAIL.lower(),
        failed_count=ll.LOCK_THRESHOLD + 40,
        first_failed_at=_ago(hours=26),
        last_failed_at=_ago(minutes=1),
        cooldown_until=None,
    )
    status = run(ll.record_attempt(session, REGISTERED_EMAIL, success=False))
    assert status["locked"] is False
    assert user.locked_at == stale
    attempt = session.attempts[REGISTERED_EMAIL.lower()]
    assert attempt.failed_count == 1
    # ...and the restarted burst still re-locks at the threshold.
    status = _fail_n(session, REGISTERED_EMAIL, ll.LOCK_THRESHOLD - 1)
    assert status["locked"] is True
    assert ll.is_lock_active(user.locked_at)


def test_burst_started_after_expiry_is_not_restarted():
    """Only a burst carried over from BEFORE the expiry is restarted; failures
    counted after it accumulate normally."""
    stale = _ago(hours=30)
    user = _registered_user(locked_at=stale)
    session = FakeSession(user=user)
    session.attempts[REGISTERED_EMAIL.lower()] = ll.LoginAttempt(
        email_lc=REGISTERED_EMAIL.lower(),
        failed_count=ll.LOCK_THRESHOLD - 1,
        first_failed_at=_ago(minutes=30),
        last_failed_at=_ago(minutes=1),
        cooldown_until=None,
    )
    status = run(ll.record_attempt(session, REGISTERED_EMAIL, success=False))
    assert status["locked"] is True
    assert ll.is_lock_active(user.locked_at)
