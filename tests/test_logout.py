"""POST /auth/logout: sign-out ends the caller's session on the API at once.

The route stamps ``users.active_session_id`` with a never-matching sentinel —
but only while the account's active session is the caller's own (or unclaimed),
so a newer sign-in on another device is never touched. Run against an
in-memory SQLite ``users`` table so the conditional UPDATE is exercised for
real.
"""

import datetime
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.api.dependencies.auth import (
    _enforce_single_session,
    get_current_db_user_allow_must_change,
)
from app.core.database import Base, get_session
from app.core.security import SessionSupersededError
from app.main import app
from app.models.login_attempt import LoginAttempt
from app.models.maintenance import MaintenanceMode
from app.models.user import User
from app.schemas.auth import UserContext
from app.services import auth_sessions

_AUTH_UUID = uuid.UUID("44444444-4444-4444-4444-444444444444")


class _AsyncSession:
    """The async-session surface the routes use, over a sync ORM session."""

    def __init__(self, session: Session):
        self._s = session
        self.commits = 0
        self.added: list = []

    async def execute(self, stmt, params=None):
        return self._s.execute(stmt, params)

    async def scalar(self, stmt, params=None):
        return self._s.scalar(stmt, params)

    def add(self, obj):
        # Only the users table is real here; other rows (login_events) are
        # captured, not persisted.
        if isinstance(obj, User):
            self._s.add(obj)
        else:
            self.added.append(obj)

    async def commit(self):
        self.commits += 1
        self._s.commit()


@pytest.fixture
def db():
    engine = create_engine(
        "sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False}
    )
    with engine.begin() as conn:
        Base.metadata.create_all(
            conn,
            tables=[
                User.__table__,
                LoginAttempt.__table__,
                MaintenanceMode.__table__,
            ],
        )
    with Session(engine) as session:
        yield session
    engine.dispose()


def _user(db, active_session_id, active_session_at=None):
    db.add(
        User(
            user_id=7,
            auth_user_id=_AUTH_UUID,
            email="boss@byu.edu",
            active=True,
            must_change_password=False,
            active_session_id=active_session_id,
            active_session_at=active_session_at,
        )
    )
    db.commit()


def _row(db) -> User:
    db.expire_all()
    return db.get(User, 7)


def _call(db, *, session_id, roles=("full_access",), path="/auth/logout"):
    shim = _AsyncSession(db)

    async def _session():
        yield shim

    app.dependency_overrides[get_session] = _session
    app.dependency_overrides[get_current_db_user_allow_must_change] = (
        lambda: UserContext(
            user_id=7,
            auth_user_id=_AUTH_UUID,
            roles=list(roles),
            session_id=session_id,
        )
    )
    try:
        with TestClient(app) as client:
            return client.post(path)
    finally:
        app.dependency_overrides.clear()


def _ctx(session_id, active_session_id):
    return UserContext(
        user_id=7,
        auth_user_id=_AUTH_UUID,
        roles=["full_access"],
        session_id=session_id,
        active_session_id=active_session_id,
    )


def test_logout_requires_auth():
    with TestClient(app) as client:
        assert client.post("/auth/logout").status_code == 401


def test_logout_ends_the_callers_session(db):
    _user(db, "sessA")
    resp = _call(db, session_id="sessA")
    assert resp.status_code == 200
    assert resp.json() == {"ended": True}
    row = _row(db)
    assert row.active_session_id.startswith(auth_sessions.SIGNED_OUT_SENTINEL_PREFIX)
    assert row.active_session_at is not None
    # The very same token is now rejected by every data route (401).
    with pytest.raises(SessionSupersededError):
        _enforce_single_session(_ctx("sessA", row.active_session_id))


def test_logout_leaves_a_newer_session_elsewhere_alone(db):
    # Device B signed in after device A; A signing out must not end B.
    _user(db, "sessB")
    resp = _call(db, session_id="sessA")
    assert resp.status_code == 200
    assert resp.json() == {"ended": False}
    row = _row(db)
    assert row.active_session_id == "sessB"
    _enforce_single_session(_ctx("sessB", row.active_session_id))


def test_logout_ends_an_unclaimed_session(db):
    # NULL is the fail-open state: without the stamp this token would stay valid.
    _user(db, None)
    resp = _call(db, session_id="sessA")
    assert resp.json() == {"ended": True}
    with pytest.raises(SessionSupersededError):
        _enforce_single_session(_ctx("sessA", _row(db).active_session_id))


def test_logout_without_a_session_id_writes_nothing(db):
    _user(db, "sessA")
    resp = _call(db, session_id=None)
    assert resp.json() == {"ended": False}
    assert _row(db).active_session_id == "sessA"


def test_logout_is_idempotent(db):
    _user(db, "sessA")
    assert _call(db, session_id="sessA").json() == {"ended": True}
    stamped = _row(db).active_session_id
    assert _call(db, session_id="sessA").json() == {"ended": False}
    assert _row(db).active_session_id == stamped


@pytest.mark.parametrize("roles", [("engineer",), ("full_access",)])
def test_a_fresh_sign_in_after_logout_claims_the_account(db, monkeypatch, roles):
    # Signing out never locks anyone out (engineers included): a NEW session —
    # created after the sign-out — claims the account through POST /auth/login.
    _user(db, "sessA", datetime.datetime(2026, 1, 1, tzinfo=datetime.UTC))
    assert _call(db, session_id="sessA", roles=roles).json() == {"ended": True}

    async def _live(_session, _sid):
        return datetime.datetime.now(datetime.UTC) + datetime.timedelta(seconds=1)

    monkeypatch.setattr(auth_sessions, "live_session_created_at", _live)
    resp = _call(db, session_id="sess-new", roles=roles, path="/auth/login")
    assert resp.status_code == 200
    row = _row(db)
    assert row.active_session_id == "sess-new"
    _enforce_single_session(_ctx("sess-new", row.active_session_id))


def test_an_older_session_cannot_reclaim_after_logout(db, monkeypatch):
    # A surviving session created BEFORE the sign-out cannot take the account
    # back through POST /auth/login.
    _user(db, "sessA", datetime.datetime(2026, 1, 1, tzinfo=datetime.UTC))
    assert _call(db, session_id="sessA").json() == {"ended": True}

    async def _old(_session, _sid):
        return datetime.datetime(2026, 1, 2, tzinfo=datetime.UTC)

    monkeypatch.setattr(auth_sessions, "live_session_created_at", _old)
    _call(db, session_id="sess-old", path="/auth/login")
    assert _row(db).active_session_id.startswith(
        auth_sessions.SIGNED_OUT_SENTINEL_PREFIX
    )
