"""Unit tests for the pre-login throttle/lockout service.

These drive the real ``app/services/login_lockout.py`` logic against a minimal
in-memory fake session (no database). They assert the cooldown trips at
``COOLDOWN_THRESHOLD``, failure reports NEVER arm, extend or re-arm the hard lock
(BT-1103-1: they arrive unauthenticated, so they prove nothing), ``check_login``
reflects the cooldown but NEVER a lock (anti-enumeration: an existing lock is
enforced after authentication), a success resets the counter, and the rolling
window reset works.

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


# --- hard lock: never armed by failure reports (BT-1103-1) --------------------

MANY = 25  # comfortably past the old 20-failure lock threshold


def test_many_failures_cool_down_but_never_hard_lock_a_registered_account():
    user = _registered_user()
    session = FakeSession(user=user)
    for n in range(1, MANY + 1):
        status = run(ll.record_attempt(session, REGISTERED_EMAIL, success=False))
        assert user.locked_at is None
        assert user.locked_reason is None
        if n >= ll.COOLDOWN_THRESHOLD:
            assert status["reason"] == "cooldown"
            assert status["allowed"] is False
            assert 0 < status["retry_after_seconds"] <= ll.COOLDOWN_MINUTES * 60
        else:
            assert status["reason"] == "ok"
    assert set(status) == {"allowed", "reason", "retry_after_seconds"}
    assert session.attempts[REGISTERED_EMAIL.lower()].failed_count == MANY


def test_failures_do_not_extend_a_live_lock():
    original = _now() - datetime.timedelta(hours=2)
    user = _registered_user(locked_at=original)
    _fail_n(FakeSession(user=user), REGISTERED_EMAIL, MANY)
    assert user.locked_at == original


def test_failures_do_not_re_arm_an_expired_lock():
    stale = _now() - datetime.timedelta(hours=30)
    user = _registered_user(locked_at=stale)
    _fail_n(FakeSession(user=user), REGISTERED_EMAIL, MANY)
    assert user.locked_at == stale
    assert not ll.is_lock_active(user.locked_at)


def test_lock_threshold_constant_is_gone():
    """Guard against the arming code quietly coming back."""
    assert not hasattr(ll, "LOCK_THRESHOLD")


def test_public_status_is_identical_for_registered_and_unknown():
    """Anti-enumeration at the service layer, failure for failure."""
    registered = FakeSession(user=_registered_user())
    unknown = FakeSession(user=None)
    for _ in range(MANY):
        a = run(ll.record_attempt(registered, REGISTERED_EMAIL, success=False))
        b = run(ll.record_attempt(unknown, UNKNOWN_EMAIL, success=False))
        assert a == b


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


def test_record_attempt_never_looks_the_user_up():
    """Failure recording no longer needs the account at all, so the query
    pattern is the same for every email."""
    registered = _CountingSession(user=_registered_user())
    unknown = _CountingSession(user=None)
    _fail_n(registered, REGISTERED_EMAIL, MANY)
    _fail_n(unknown, UNKNOWN_EMAIL, MANY)
    assert registered.user_lookups == unknown.user_lookups == 0


def test_locked_and_cooling_reports_only_the_cooldown():
    # A pre-existing lock AND a cooldown -> the counter's cooldown, never `locked`.
    user = _registered_user(locked_at=_now())
    session = FakeSession(user=user)
    _fail_n(session, REGISTERED_EMAIL, ll.COOLDOWN_THRESHOLD)
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


# --- expiry of existing locks (HARD_LOCK_DURATION) ----------------------------


def _ago(**kw) -> datetime.datetime:
    return _now() - datetime.timedelta(**kw)


def test_is_lock_active_boundaries():
    now = _now()
    assert ll.is_lock_active(None, now) is False
    assert ll.is_lock_active(now - datetime.timedelta(hours=23, minutes=59), now)
    assert not ll.is_lock_active(now - ll.HARD_LOCK_DURATION, now)
    assert not ll.is_lock_active(now - datetime.timedelta(hours=25), now)
