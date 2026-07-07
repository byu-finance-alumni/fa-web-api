"""Tests for login tracking: POST /auth/login (records a sign-in) and the
engineer-only GET /admin/logins listing.

POST /auth/login stamps users.last_login_at and inserts a login_events row;
GET /admin/logins returns that history (engineer only) and audits the read.
Both use fake sessions/overrides in the style of test_login_routes.py — no DB.
"""

import datetime
import uuid
from types import SimpleNamespace

from fastapi.testclient import TestClient

from app.api.dependencies.auth import (
    get_current_db_user,
    get_current_db_user_allow_must_change,
)
from app.core.database import get_session
from app.main import app
from app.models.login_event import LoginEvent
from app.schemas.auth import UserContext


def _ctx(*roles: str, user_id: int = 1) -> UserContext:
    return UserContext(
        user_id=user_id,
        auth_user_id=uuid.UUID("11111111-1111-1111-1111-111111111111"),
        roles=list(roles),
    )


# --- POST /auth/login --------------------------------------------------------


class _RecordSession:
    """Fake session: ``scalar`` returns the loaded user; ``add``/``commit`` are
    recorded so we can assert the LoginEvent was written. ``execute`` captures the
    in-transaction login_attempts clear (#182)."""

    def __init__(self, user):
        self.user = user
        self.added: list = []
        self.commits = 0
        self.executed: list = []

    async def scalar(self, _stmt):
        return self.user

    async def execute(self, stmt):
        self.executed.append(stmt)
        return None

    def add(self, obj):
        self.added.append(obj)

    async def commit(self):
        self.commits += 1


def test_record_login_requires_auth():
    async def _no_db():
        yield None

    app.dependency_overrides[get_session] = _no_db
    with TestClient(app) as client:
        resp = client.post("/auth/login")
    app.dependency_overrides.clear()
    assert resp.status_code == 401


def test_record_login_stamps_last_login_and_writes_event():
    user = SimpleNamespace(
        user_id=7, email="boss@byu.edu", last_login_at=None
    )
    session = _RecordSession(user)

    async def _session():
        yield session

    app.dependency_overrides[get_session] = _session
    app.dependency_overrides[get_current_db_user_allow_must_change] = lambda: _ctx(
        "view_only", user_id=7
    )
    with TestClient(app) as client:
        resp = client.post("/auth/login")
    app.dependency_overrides.clear()

    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["last_login_at"]  # an ISO timestamp was echoed

    # last_login_at was stamped on the user row...
    assert user.last_login_at is not None
    # ...and a login_events row was written for that user (email snapshotted).
    event = next(a for a in session.added if isinstance(a, LoginEvent))
    assert event.user_id == 7
    assert event.email == "boss@byu.edu"
    assert event.occurred_at == user.last_login_at
    # ...and the rolling failed-login counter was cleared in the SAME transaction
    # (#182): the login-success path is where the clear now lives (it used to run
    # on every authenticated request in the resolver). One statement, one commit.
    assert len(session.executed) == 1
    assert session.commits == 1


def test_record_login_stores_forwarded_ip_and_location():
    """The frontend forwards client IP + Vercel geo headers; they land on the
    login_events row (trimmed, empties -> null)."""
    user = SimpleNamespace(user_id=7, email="boss@byu.edu", last_login_at=None)
    session = _RecordSession(user)

    async def _session():
        yield session

    app.dependency_overrides[get_session] = _session
    app.dependency_overrides[get_current_db_user_allow_must_change] = lambda: _ctx(
        "super_admin", user_id=7
    )
    with TestClient(app) as client:
        resp = client.post(
            "/auth/login",
            json={
                "ip_address": "203.0.113.7",
                "city": "Provo",
                "region": "Utah",
                "country": "US",
            },
        )
    app.dependency_overrides.clear()

    assert resp.status_code == 200
    event = next(a for a in session.added if isinstance(a, LoginEvent))
    assert event.ip_address == "203.0.113.7"
    assert event.city == "Provo"
    assert event.region == "Utah"
    assert event.country == "US"


def test_record_login_rejects_unknown_context_field():
    """extra='forbid' on the context body rejects unexpected keys (422)."""
    user = SimpleNamespace(user_id=7, email="boss@byu.edu", last_login_at=None)
    session = _RecordSession(user)

    async def _session():
        yield session

    app.dependency_overrides[get_session] = _session
    app.dependency_overrides[get_current_db_user_allow_must_change] = lambda: _ctx(
        "view_only", user_id=7
    )
    with TestClient(app) as client:
        resp = client.post("/auth/login", json={"latitude": "40.2"})
    app.dependency_overrides.clear()
    assert resp.status_code == 422


def test_record_login_rate_limited_after_burst():
    """A burst of >10 record-login calls from one actor in the window gets 429,
    braking a looping/compromised session from flooding login_events."""
    from app.core import rate_limit

    rate_limit.reset()
    user = SimpleNamespace(user_id=77, email="burst@byu.edu", last_login_at=None)
    session = _RecordSession(user)

    async def _session():
        yield session

    app.dependency_overrides[get_session] = _session
    app.dependency_overrides[get_current_db_user_allow_must_change] = lambda: _ctx(
        "view_only", user_id=77
    )
    with TestClient(app) as client:
        codes = [client.post("/auth/login").status_code for _ in range(11)]
    app.dependency_overrides.clear()
    rate_limit.reset()

    assert codes[:10] == [200] * 10
    assert codes[10] == 429


def test_record_login_allowed_on_temp_password():
    """A user on a temp password (must_change_password) has still signed in, so
    the login records — the route uses the force-change-EXEMPT resolver."""
    user = SimpleNamespace(user_id=9, email="temp@byu.edu", last_login_at=None)
    session = _RecordSession(user)

    async def _session():
        yield session

    # Overriding the exempt resolver is what the route depends on; if it depended
    # on the gated resolver instead, a must-change user would 403 here.
    app.dependency_overrides[get_session] = _session
    app.dependency_overrides[get_current_db_user_allow_must_change] = lambda: _ctx(
        "view_only", user_id=9
    )
    with TestClient(app) as client:
        resp = client.post("/auth/login")
    app.dependency_overrides.clear()

    assert resp.status_code == 200
    assert session.commits == 1


# --- GET /admin/logins -------------------------------------------------------


class _ListSession:
    """Fake session for list_logins: ``scalar`` -> total count, ``scalars`` ->
    the page of events, ``add``/``commit`` record the read-audit."""

    def __init__(self, events):
        self.events = events
        self.added: list = []
        self.commits = 0

    async def scalar(self, _stmt):
        return len(self.events)

    async def scalars(self, _stmt):
        return SimpleNamespace(all=lambda: self.events)

    def add(self, obj):
        self.added.append(obj)

    async def commit(self):
        self.commits += 1


def test_list_logins_forbidden_below_engineer():
    app.dependency_overrides[get_current_db_user] = lambda: _ctx("super_admin")

    async def _no_db():
        yield None

    app.dependency_overrides[get_session] = _no_db
    with TestClient(app) as client:
        resp = client.get("/admin/logins")
    app.dependency_overrides.clear()
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "forbidden"


def test_list_logins_requires_auth():
    async def _no_db():
        yield None

    app.dependency_overrides[get_session] = _no_db
    with TestClient(app) as client:
        resp = client.get("/admin/logins")
    app.dependency_overrides.clear()
    assert resp.status_code == 401


def test_list_logins_returns_page_and_audits_read():
    now = datetime.datetime(2026, 6, 18, 16, 19, tzinfo=datetime.UTC)
    events = [
        SimpleNamespace(
            login_event_id=2,
            user_id=7,
            email="boss@byu.edu",
            occurred_at=now,
            ip_address="203.0.113.7",
            city="Provo",
            region="Utah",
            country="US",
        ),
        SimpleNamespace(
            login_event_id=1,
            user_id=None,
            email="gone@byu.edu",
            occurred_at=now,
            ip_address="198.51.100.9",
            city="Salt Lake City",
            region="UT",
            country="US",
        ),
    ]
    session = _ListSession(events)

    async def _session():
        yield session

    app.dependency_overrides[get_session] = _session
    app.dependency_overrides[get_current_db_user] = lambda: _ctx(
        "engineer", user_id=1
    )
    with TestClient(app) as client:
        resp = client.get("/admin/logins")
    app.dependency_overrides.clear()

    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 2
    assert [r["login_event_id"] for r in body["items"]] == [2, 1]
    # A deleted user's row keeps the snapshotted email with a null user_id.
    assert body["items"][1]["user_id"] is None
    assert body["items"][1]["email"] == "gone@byu.edu"

    # The read itself is audited (read_login_log), and nothing else.
    audit = next(a for a in session.added if type(a).__name__ == "AuditLog")
    assert audit.action_type == "read_login_log"
    assert audit.entity_type == "login_event"
    assert audit.user_id == 1
    assert session.commits == 1


# --- DELETE /admin/logins (purge all) ----------------------------------------


class _PurgeSession:
    """Fake session for purge_logins: ``execute`` reports the delete rowcount and
    ``commit`` is recorded so we can assert the wipe was committed."""

    def __init__(self, rowcount):
        self.rowcount = rowcount
        self.commits = 0
        self.executed: list = []

    async def execute(self, stmt):
        self.executed.append(stmt)
        return SimpleNamespace(rowcount=self.rowcount)

    async def commit(self):
        self.commits += 1


def test_purge_logins_engineer_deletes_all_and_returns_count():
    session = _PurgeSession(rowcount=7)

    async def _session():
        yield session

    app.dependency_overrides[get_session] = _session
    app.dependency_overrides[get_current_db_user] = lambda: _ctx(
        "engineer", user_id=1
    )
    with TestClient(app) as client:
        resp = client.delete("/admin/logins")
    app.dependency_overrides.clear()

    assert resp.status_code == 200
    assert resp.json() == {"deleted": 7}
    assert len(session.executed) == 1  # a single bulk DELETE
    assert session.commits == 1


def test_purge_logins_forbidden_below_engineer():
    async def _no_db():
        yield None

    app.dependency_overrides[get_session] = _no_db
    for role in ("super_admin", "full_access", "student", "view_only"):
        app.dependency_overrides[get_current_db_user] = lambda r=role: _ctx(r)
        with TestClient(app) as client:
            resp = client.delete("/admin/logins")
        assert resp.status_code == 403
        assert resp.json()["error"]["code"] == "forbidden"
    app.dependency_overrides.clear()


def test_purge_logins_requires_auth():
    async def _no_db():
        yield None

    app.dependency_overrides[get_session] = _no_db
    with TestClient(app) as client:
        resp = client.delete("/admin/logins")
    app.dependency_overrides.clear()
    assert resp.status_code == 401
