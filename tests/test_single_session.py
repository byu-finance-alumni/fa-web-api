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


def _ctx(session_id, active_session_id) -> UserContext:
    return UserContext(
        user_id=1,
        auth_user_id=uuid.UUID(_AUTH_UUID),
        roles=["view_only"],
        session_id=session_id,
        active_session_id=active_session_id,
    )


def test_enforce_rejects_superseded_session():
    with pytest.raises(SessionSupersededError):
        _enforce_single_session(_ctx(session_id="sessB", active_session_id="sessA"))


def test_enforce_allows_matching_session():
    _enforce_single_session(_ctx(session_id="sessA", active_session_id="sessA"))


def test_enforce_fails_open_when_no_active_session():
    # A user who hasn't signed in since the feature shipped (NULL active) is
    # never locked out.
    _enforce_single_session(_ctx(session_id="sessB", active_session_id=None))


def test_enforce_fails_open_when_token_has_no_session_id():
    # A token predating the session_id claim can't be compared -> allowed.
    _enforce_single_session(_ctx(session_id=None, active_session_id="sessA"))


# --- full resolver: get_current_db_user raises on a superseded session --------


class _NoWriteSession:
    """Session for the read-path resolver: records any statement/commit so tests
    can assert the read path issues NONE (#182 — no per-request write)."""

    def __init__(self):
        self.executed: list = []
        self.commits = 0

    async def execute(self, stmt):
        self.executed.append(stmt)

    async def commit(self):
        self.commits += 1


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


def test_get_current_db_user_rejects_superseded(monkeypatch):
    async def _lookup(_session, _auth_id):
        return _db_user(active_session_id="sessA")

    monkeypatch.setattr(auth_deps, "get_user_with_roles_by_auth_id", _lookup)
    current = SimpleNamespace(auth_user_id=_AUTH_UUID, session_id="sessB")
    with pytest.raises(SessionSupersededError):
        asyncio.run(auth_deps.get_current_db_user(current, _NoWriteSession()))


def test_get_current_db_user_allows_active_session(monkeypatch):
    async def _lookup(_session, _auth_id):
        return _db_user(active_session_id="sessA")

    monkeypatch.setattr(auth_deps, "get_user_with_roles_by_auth_id", _lookup)
    session = _NoWriteSession()
    current = SimpleNamespace(auth_user_id=_AUTH_UUID, session_id="sessA")
    ctx = asyncio.run(auth_deps.get_current_db_user(current, session))
    assert ctx.user_id == 1
    # #182: the matching-session read path issues no DELETE/commit.
    assert session.executed == []
    assert session.commits == 0


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
