"""Single active session per account (#147).

The newest sign-in claims ``users.active_session_id`` (POST /auth/login); any
earlier session whose token ``session_id`` no longer matches is rejected on the
data routes (get_current_db_user -> SessionSupersededError). GET
/auth/session/active reports the state WITHOUT rejecting, so a superseded device
can detect it and sign out cleanly. All exercised with fakes — no DB.
"""

import asyncio
import uuid
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app.api.dependencies import auth as auth_deps
from app.api.dependencies.auth import (
    _enforce_single_session,
    get_current_db_user_allow_must_change,
)
from app.core.database import get_session
from app.core.security import SessionSupersededError
from app.main import app
from app.models.user import User
from app.schemas.auth import UserContext

_AUTH_UUID = "33333333-3333-3333-3333-333333333333"


# --- resolver-level enforcement ----------------------------------------------


def _ctx(session_id, active_session_id, issued_at=None) -> UserContext:
    return UserContext(
        user_id=1,
        auth_user_id=uuid.UUID(_AUTH_UUID),
        roles=["view_only"],
        session_id=session_id,
        session_issued_at=issued_at,
        active_session_id=active_session_id,
    )


class _FakeSession:
    """Fake AsyncSession for the recency-claim path: ``execute`` returns a result
    with a fixed ``rowcount`` (whether the conditional claim UPDATE matched) and
    records commits."""

    def __init__(self, rowcount=0):
        self._rowcount = rowcount
        self.executed: list = []
        self.commits = 0

    async def execute(self, stmt):
        self.executed.append(stmt)
        return SimpleNamespace(rowcount=self._rowcount)

    async def commit(self):
        self.commits += 1


def _enforce(ctx, session=None):
    return asyncio.run(_enforce_single_session(ctx, session or _FakeSession()))


def test_enforce_rejects_superseded_session_without_recency():
    # Mismatched sessions and no token iat to prove which is newer -> stay strict.
    with pytest.raises(SessionSupersededError):
        _enforce(_ctx(session_id="sessB", active_session_id="sessA"))


def test_enforce_allows_matching_session():
    session = _FakeSession()
    _enforce(_ctx(session_id="sessA", active_session_id="sessA"), session)
    # The steady state (ids equal) must not write — that would reintroduce #182.
    assert session.executed == []
    assert session.commits == 0


def test_enforce_fails_open_when_no_active_session():
    # A user who hasn't signed in since the feature shipped (NULL active) is
    # never locked out, and we do NOT claim here (grace: existing devices keep
    # working; the claim happens on a real sign-in).
    session = _FakeSession()
    _enforce(_ctx(session_id="sessB", active_session_id=None), session)
    assert session.executed == []


def test_enforce_fails_open_when_token_has_no_session_id():
    # A token predating the session_id claim can't be compared -> allowed.
    _enforce(_ctx(session_id=None, active_session_id="sessA"))


def test_enforce_newer_session_claims_and_wins():
    # #188: the new device presents a strictly-newer token (higher iat). Even
    # though ``active`` still points at the OLD session (its best-effort claim
    # POST may have been lost), the newer session is adopted server-side and
    # allowed -- "new login wins" holds without the client POST.
    session = _FakeSession(rowcount=1)
    ctx = _ctx(session_id="sessB", active_session_id="sessA", issued_at=2000)
    _enforce(ctx, session)
    assert ctx.active_session_id == "sessB"  # claimed
    assert session.commits == 1


def test_enforce_older_session_is_rejected():
    # #188: a strictly-OLDER session (its conditional claim UPDATE matches no row
    # because a newer session already holds active_session_at) is superseded.
    session = _FakeSession(rowcount=0)
    ctx = _ctx(session_id="sessOLD", active_session_id="sessNEW", issued_at=1000)
    with pytest.raises(SessionSupersededError):
        _enforce(ctx, session)


# --- full resolver: get_current_db_user raises on a superseded session --------


def _db_user(active_session_id):
    return SimpleNamespace(
        user_id=1,
        auth_user_id=uuid.UUID(_AUTH_UUID),
        email="worker@byu.edu",
        first_name="T",
        last_name="W",
        active=True,
        must_change_password=False,
        active_session_id=active_session_id,
        roles=[SimpleNamespace(role_name="view_only")],
    )


def _current(session_id, issued_at=None):
    return SimpleNamespace(
        auth_user_id=_AUTH_UUID, session_id=session_id, session_issued_at=issued_at
    )


def test_get_current_db_user_rejects_superseded(monkeypatch):
    async def _lookup(_session, _auth_id):
        return _db_user(active_session_id="sessA")

    monkeypatch.setattr(auth_deps, "get_user_with_roles_by_auth_id", _lookup)
    # No iat -> can't prove newer -> the mismatched (superseded) session is rejected.
    with pytest.raises(SessionSupersededError):
        asyncio.run(auth_deps.get_current_db_user(_current("sessB"), _FakeSession()))


def test_get_current_db_user_allows_active_session(monkeypatch):
    async def _lookup(_session, _auth_id):
        return _db_user(active_session_id="sessA")

    monkeypatch.setattr(auth_deps, "get_user_with_roles_by_auth_id", _lookup)
    session = _FakeSession()
    ctx = asyncio.run(auth_deps.get_current_db_user(_current("sessA"), session))
    assert ctx.user_id == 1
    # The matching-session steady state must not write (no #182-style per-request
    # DELETE/UPDATE + commit).
    assert session.executed == []
    assert session.commits == 0


def test_get_current_db_user_new_login_wins_even_if_claim_lost(monkeypatch):
    # #188 end-to-end: active still points at the OLD session (the new device's
    # POST /auth/login claim never landed), but the NEW device's token is newer,
    # so it claims the session server-side and is allowed instead of kicked.
    async def _lookup(_session, _auth_id):
        return _db_user(active_session_id="sessOLD")

    monkeypatch.setattr(auth_deps, "get_user_with_roles_by_auth_id", _lookup)
    session = _FakeSession(rowcount=1)
    ctx = asyncio.run(
        auth_deps.get_current_db_user(
            _current("sessNEW", issued_at=2000), session
        )
    )
    assert ctx.active_session_id == "sessNEW"
    assert session.commits == 1


# --- POST /auth/login claims the newest session ------------------------------


class _RecordSession:
    def __init__(self, user):
        self.user = user
        self.added: list = []
        self.commits = 0
        self.executed: list = []

    async def scalar(self, _stmt):
        return self.user

    async def execute(self, stmt):
        # record_login clears login_attempts in-transaction (#182).
        self.executed.append(stmt)
        return None

    def add(self, obj):
        self.added.append(obj)

    async def commit(self):
        self.commits += 1


def test_record_login_claims_active_session():
    db_user = SimpleNamespace(
        user_id=7,
        email="boss@byu.edu",
        last_login_at=None,
        active_session_id=None,
        active_session_at=None,
    )
    session = _RecordSession(db_user)

    async def _session():
        yield session

    app.dependency_overrides[get_session] = _session
    app.dependency_overrides[get_current_db_user_allow_must_change] = (
        lambda: UserContext(
            user_id=7,
            auth_user_id=uuid.UUID(_AUTH_UUID),
            roles=["view_only"],
            session_id="sess-new",
        )
    )
    with TestClient(app) as client:
        resp = client.post("/auth/login")
    app.dependency_overrides.clear()

    assert resp.status_code == 200
    assert db_user.active_session_id == "sess-new"
    assert db_user.active_session_at is not None


# --- GET /auth/session/active reports without rejecting -----------------------


def _override_active(session_id, active_session_id):
    app.dependency_overrides[get_current_db_user_allow_must_change] = (
        lambda: UserContext(
            user_id=1,
            auth_user_id=uuid.UUID(_AUTH_UUID),
            roles=["view_only"],
            session_id=session_id,
            active_session_id=active_session_id,
        )
    )


def test_session_active_true_when_matching():
    _override_active("sessA", "sessA")
    with TestClient(app) as client:
        resp = client.get("/auth/session/active")
    app.dependency_overrides.clear()
    assert resp.status_code == 200
    assert resp.json() == {"active": True}


def test_session_active_false_when_superseded():
    # A superseded device gets a clean {active: false}, NOT a 401.
    _override_active("sessB", "sessA")
    with TestClient(app) as client:
        resp = client.get("/auth/session/active")
    app.dependency_overrides.clear()
    assert resp.status_code == 200
    assert resp.json() == {"active": False}


def test_session_active_true_when_no_claim():
    _override_active("sessB", None)
    with TestClient(app) as client:
        resp = client.get("/auth/session/active")
    app.dependency_overrides.clear()
    assert resp.status_code == 200
    assert resp.json() == {"active": True}


def test_session_active_requires_auth():
    async def _no_db():
        yield None

    app.dependency_overrides[get_session] = _no_db
    with TestClient(app) as client:
        resp = client.get("/auth/session/active")
    app.dependency_overrides.clear()
    assert resp.status_code == 401


# Keep an import of User referenced so a schema drift in the model surfaces here.
assert hasattr(User, "active_session_id")
