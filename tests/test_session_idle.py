"""24-hour idle session expiry (#684).

Two layers, both with fakes and no DB:

  * the POLICY (``app/services/session_idle.py``) — pure arithmetic over a
    timestamp, so the thresholds are pinned exactly;
  * the RESOLVER hook (``_enforce_session_idle``) — that an expired session is
    stamped with a sentinel and a fresh one is merely touched, throttled.

The thing most worth guarding is that **NULL is fresh**. Every session alive
when this ships predates the column, so reading NULL as "never seen, therefore
idle" would sign out the whole department on their first request after deploy.
"""

import asyncio
import datetime
import uuid
from types import SimpleNamespace

import pytest

from app.api.dependencies.auth import _enforce_session_idle
from app.schemas.auth import UserContext
from app.services import session_idle

_AUTH_UUID = "44444444-4444-4444-4444-444444444444"
_SESSION_ID = "11111111-1111-1111-1111-111111111111"
NOW = datetime.datetime(2026, 8, 22, 12, 0, tzinfo=datetime.UTC)


def _ago(**kw) -> datetime.datetime:
    return NOW - datetime.timedelta(**kw)


# --- the policy --------------------------------------------------------------


def test_limit_is_twenty_four_hours():
    # Jake's number (2026-08-22). Pinned because everything else here is
    # relative to it and a silent change would be invisible in behaviour until
    # somebody was signed out early — or late.
    assert session_idle.IDLE_LIMIT_SECONDS == 24 * 60 * 60


def test_never_stamped_is_fresh_not_infinitely_idle():
    # THE deploy-day case: if this ever returns True, every pre-existing session
    # is expired on its first request.
    assert session_idle.is_expired(None, NOW) is False
    assert session_idle.idle_seconds(None, NOW) is None


@pytest.mark.parametrize(
    "last_seen,expired",
    [
        (_ago(minutes=1), False),
        (_ago(hours=23, minutes=59), False),
        (_ago(hours=24), True),
        (_ago(days=7), True),
    ],
)
def test_expiry_boundary(last_seen, expired):
    assert session_idle.is_expired(last_seen, NOW) is expired


def test_clock_skew_reads_as_fresh_not_ancient():
    # A stamp from the future means two hosts disagree about the time. Clamping
    # to 0 keeps that from presenting as a mystery logout.
    assert session_idle.idle_seconds(NOW + datetime.timedelta(hours=1), NOW) == 0
    assert session_idle.is_expired(NOW + datetime.timedelta(hours=1), NOW) is False


@pytest.mark.parametrize(
    "last_seen,touch",
    [
        (None, True),
        (_ago(seconds=5), False),
        (_ago(seconds=59), False),
        (_ago(seconds=60), True),
        (_ago(hours=3), True),
    ],
)
def test_touch_is_throttled(last_seen, touch):
    # The resolver runs on EVERY authenticated request; without this the feature
    # is an UPDATE plus a commit per request.
    assert session_idle.should_touch(last_seen, NOW) is touch


def test_throttle_is_far_below_the_limit():
    # The measured idle time can be at most one throttle window staler than the
    # truth, so the window has to be negligible against the limit.
    assert session_idle.TOUCH_THROTTLE_SECONDS < session_idle.IDLE_LIMIT_SECONDS / 100


# --- the resolver hook -------------------------------------------------------


class _FakeSession:
    """Enough AsyncSession for the hook: it commits, adds and never fails."""

    def __init__(self):
        self.commits = 0
        self.added = []
        self.executed = []

    async def commit(self):
        self.commits += 1

    async def rollback(self):  # pragma: no cover - only on the failure path
        pass

    def add(self, obj):
        self.added.append(obj)

    async def execute(self, *args, **kwargs):
        self.executed.append(args)
        return SimpleNamespace(first=lambda: None)


def _user(last_seen, session_id=_SESSION_ID):
    return SimpleNamespace(
        user_id=1,
        active_session_id=session_id,
        active_session_at=None,
        session_last_seen_at=last_seen,
    )


def _ctx(session_id=_SESSION_ID, active_session_id=_SESSION_ID) -> UserContext:
    return UserContext(
        user_id=1,
        auth_user_id=uuid.UUID(_AUTH_UUID),
        roles=["view_only"],
        session_id=session_id,
        active_session_id=active_session_id,
    )


def test_idle_session_is_stamped_with_a_sentinel():
    db, user, ctx = _FakeSession(), _user(_ago(days=2)), _ctx()
    asyncio.run(_enforce_session_idle(db, user, ctx, now=NOW))

    # Our half: the account's active session no longer matches the token's, so
    # _enforce_single_session refuses this very request.
    assert user.active_session_id.startswith("revoked:")
    # …and the CONTEXT carries it, which is what the guards actually read.
    assert ctx.active_session_id == user.active_session_id
    # The Supabase half was attempted on the same transaction.
    assert db.executed, "expected the auth.sessions row to be deleted"
    # The forced logout is auditable — otherwise it is an unexplained sign-out.
    assert [a.action_type for a in db.added] == ["session_expired_idle"]


def test_fresh_session_is_touched_not_expired():
    db, user, ctx = _FakeSession(), _user(_ago(hours=2)), _ctx()
    asyncio.run(_enforce_session_idle(db, user, ctx, now=NOW))

    assert user.active_session_id == _SESSION_ID
    assert ctx.active_session_id == _SESSION_ID
    assert user.session_last_seen_at is not None
    assert user.session_last_seen_at > _ago(hours=1)
    assert db.commits == 1
    assert not db.executed


def test_recently_touched_session_writes_nothing():
    db, user, ctx = _FakeSession(), _user(_ago(seconds=5)), _ctx()
    asyncio.run(_enforce_session_idle(db, user, ctx, now=NOW))
    assert db.commits == 0, "the throttle is what makes this affordable"


def test_never_stamped_session_starts_the_clock_and_survives():
    db, user, ctx = _FakeSession(), _user(None), _ctx()
    asyncio.run(_enforce_session_idle(db, user, ctx, now=NOW))

    assert user.active_session_id == _SESSION_ID, "deploy day must not log everyone out"
    assert user.session_last_seen_at is not None


def test_superseded_session_has_no_idle_clock():
    # It is already refused by _enforce_single_session; giving it a second
    # reason would just mean writing to a row on behalf of a dead session.
    db = _FakeSession()
    user = _user(_ago(days=30), session_id="someone-elses-session")
    ctx = _ctx(session_id=_SESSION_ID, active_session_id="someone-elses-session")
    asyncio.run(_enforce_session_idle(db, user, ctx, now=NOW))

    assert user.active_session_id == "someone-elses-session"
    assert db.commits == 0
    assert not db.added


def test_token_without_a_session_id_is_left_alone():
    db, user, ctx = _FakeSession(), _user(_ago(days=30)), _ctx(session_id=None)
    asyncio.run(_enforce_session_idle(db, user, ctx, now=NOW))
    assert db.commits == 0
