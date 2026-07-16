"""Tests for the failed-login log: POST /auth/login/record (writes a
login_failures row on a failure) and the engineer-only GET /admin/login-failures
listing.

A failed attempt bumps the rolling login_attempts counter AND logs a per-attempt
login_failures row (attempted email + forwarded IP/geo/reason). A success logs
NOTHING and leaves the counter alone. GET /admin/login-failures returns the log
newest-first, is engineer-gated, and paginates. Fake sessions/overrides in the
style of test_login_tracking.py — no DB.
"""

import datetime
import uuid
from types import SimpleNamespace

from fastapi.testclient import TestClient

from app.api.dependencies.auth import get_current_db_user
from app.core.database import get_session
from app.main import app
from app.models.login_failure import LoginFailure
from app.schemas.auth import UserContext


def _ctx(*roles: str, user_id: int = 1) -> UserContext:
    return UserContext(
        user_id=user_id,
        auth_user_id=uuid.UUID("11111111-1111-1111-1111-111111111111"),
        roles=list(roles),
    )


# --- POST /auth/login/record (failure logging) -------------------------------


class _FailSession:
    """Fake session covering both the throttle service (get/scalar) and the
    best-effort login_failures insert (add/commit). ``user`` is the registered
    user for the attempted email, or None for an unknown/probed email."""

    def __init__(self, user=None):
        self.user = user
        self.attempts: dict = {}
        self.added: list = []
        self.commits = 0

    async def scalar(self, _stmt):
        return self.user

    async def get(self, model, pk):
        from app.models.login_attempt import LoginAttempt

        return self.attempts.get(pk) if model is LoginAttempt else None

    def add(self, obj):
        from app.models.login_attempt import LoginAttempt

        if isinstance(obj, LoginAttempt):
            self.attempts[obj.email_lc] = obj
        self.added.append(obj)

    async def delete(self, obj):
        from app.models.login_attempt import LoginAttempt

        if isinstance(obj, LoginAttempt):
            self.attempts.pop(obj.email_lc, None)

    async def commit(self):
        self.commits += 1

    async def rollback(self):
        pass


def test_record_failure_logs_login_failure_with_ip():
    """A failure (success:false) with a forwarded context inserts a
    login_failures row: attempted email snapshotted lowercased, IP/geo/reason
    stored (trimmed)."""
    session = _FailSession(user=None)

    async def _session():
        yield session

    app.dependency_overrides[get_session] = _session
    with TestClient(app) as client:
        resp = client.post(
            "/auth/login/record",
            json={
                "email": "Probe@BYU.edu",  # mixed case; stored lowercased
                "success": False,
                "context": {
                    "ip_address": "203.0.113.9",
                    "city": "Provo",
                    "region": "Utah",
                    "country": "US",
                },
                "reason": "invalid_credentials",
            },
        )
    app.dependency_overrides.clear()

    assert resp.status_code == 200
    # The throttle response body is unchanged (anti-enumeration): only the three
    # public keys, never the internal `locked` flag.
    assert set(resp.json()) == {"allowed", "reason", "retry_after_seconds"}

    failure = next(a for a in session.added if isinstance(a, LoginFailure))
    assert failure.email == "probe@byu.edu"
    assert failure.ip_address == "203.0.113.9"
    assert failure.city == "Provo"
    assert failure.region == "Utah"
    assert failure.country == "US"
    assert failure.reason == "invalid_credentials"


def test_record_failure_without_context_still_logs():
    """A failure with no context/IP still logs a row (unlike GET /admin/logins,
    the failures log keeps IP-less attempts)."""
    session = _FailSession(user=None)

    async def _session():
        yield session

    app.dependency_overrides[get_session] = _session
    with TestClient(app) as client:
        resp = client.post(
            "/auth/login/record", json={"email": "x@byu.edu", "success": False}
        )
    app.dependency_overrides.clear()

    assert resp.status_code == 200
    failure = next(a for a in session.added if isinstance(a, LoginFailure))
    assert failure.email == "x@byu.edu"
    assert failure.ip_address is None
    assert failure.reason is None


def test_record_success_logs_no_failure_and_does_not_clear_counter():
    """A success (success:true) from this unauthenticated caller logs NOTHING and
    does not mutate/commit — so a cooldown counter can't be wiped this way."""
    session = _FailSession(user=None)

    async def _session():
        yield session

    app.dependency_overrides[get_session] = _session
    with TestClient(app) as client:
        resp = client.post(
            "/auth/login/record",
            json={
                "email": "x@byu.edu",
                "success": True,
                "context": {"ip_address": "203.0.113.9"},
            },
        )
    app.dependency_overrides.clear()

    assert resp.status_code == 200
    # No login_failures row was written...
    assert not any(isinstance(a, LoginFailure) for a in session.added)
    # ...and nothing was committed at all (the success path is a pure read).
    assert session.commits == 0


# --- GET /admin/login-failures -----------------------------------------------


class _ListSession:
    """Fake session for list_login_failures: ``scalar`` -> total count,
    ``scalars`` -> the page, ``add``/``commit`` record the read-audit."""

    def __init__(self, failures):
        self.failures = failures
        self.added: list = []
        self.commits = 0

    async def scalar(self, _stmt):
        return len(self.failures)

    async def scalars(self, _stmt):
        return SimpleNamespace(all=lambda: self.failures)

    def add(self, obj):
        self.added.append(obj)

    async def commit(self):
        self.commits += 1


def test_list_login_failures_forbidden_below_engineer():
    app.dependency_overrides[get_current_db_user] = lambda: _ctx("super_admin")

    async def _no_db():
        yield None

    app.dependency_overrides[get_session] = _no_db
    with TestClient(app) as client:
        resp = client.get("/admin/login-failures")
    app.dependency_overrides.clear()
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "forbidden"


def test_list_login_failures_requires_auth():
    async def _no_db():
        yield None

    app.dependency_overrides[get_session] = _no_db
    with TestClient(app) as client:
        resp = client.get("/admin/login-failures")
    app.dependency_overrides.clear()
    assert resp.status_code == 401


def test_list_login_failures_returns_page_newest_first_and_audits():
    now = datetime.datetime(2026, 7, 16, 16, 19, tzinfo=datetime.UTC)
    # The route orders by occurred_at DESC; the fake session returns them already
    # in that order (newest id first), so the response should preserve it.
    failures = [
        SimpleNamespace(
            login_failure_id=2,
            email="probe@byu.edu",
            occurred_at=now,
            ip_address="203.0.113.9",
            city="Provo",
            region="Utah",
            country="US",
            reason="invalid_credentials",
        ),
        SimpleNamespace(
            login_failure_id=1,
            email="nobody@byu.edu",
            occurred_at=now,
            ip_address=None,
            city=None,
            region=None,
            country=None,
            reason=None,
        ),
    ]
    session = _ListSession(failures)

    async def _session():
        yield session

    app.dependency_overrides[get_session] = _session
    app.dependency_overrides[get_current_db_user] = lambda: _ctx(
        "engineer", user_id=1
    )
    with TestClient(app) as client:
        resp = client.get("/admin/login-failures")
    app.dependency_overrides.clear()

    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 2
    assert [r["login_failure_id"] for r in body["items"]] == [2, 1]
    # An IP-less failure is still returned (kept, unlike GET /admin/logins).
    assert body["items"][1]["ip_address"] is None
    assert body["items"][1]["email"] == "nobody@byu.edu"

    audit = next(a for a in session.added if type(a).__name__ == "AuditLog")
    assert audit.action_type == "read_login_failure_log"
    assert audit.entity_type == "login_failure"
    assert audit.user_id == 1
    assert session.commits == 1


def test_list_login_failures_paginates():
    session = _ListSession([])

    async def _session():
        yield session

    app.dependency_overrides[get_session] = _session
    app.dependency_overrides[get_current_db_user] = lambda: _ctx(
        "engineer", user_id=1
    )
    with TestClient(app) as client:
        ok = client.get("/admin/login-failures?limit=5&offset=10")
        too_big = client.get("/admin/login-failures?limit=201")
        negative = client.get("/admin/login-failures?offset=-1")
    app.dependency_overrides.clear()

    assert ok.status_code == 200
    body = ok.json()
    assert body["limit"] == 5
    assert body["offset"] == 10
    # The hard cap (200) and non-negative offset are enforced by the route.
    assert too_big.status_code == 422
    assert negative.status_code == 422
