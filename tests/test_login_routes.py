"""Route tests for the login-hardening endpoints.

Covers the unauthenticated pre-login throttle endpoints (`/auth/login/precheck`,
`/auth/login/record`) wired to a fake session, and the super_admin-only
`/admin/users/{id}/reset-password` route: 403 for non-super-admins, and — with
the Supabase Admin API call mocked — that a success clears the lock, drops the
login_attempts row, audits the action, and returns a one-time temp password.
"""

import uuid
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app.api.dependencies.auth import get_current_db_user
from app.api.routes import admin as admin_routes
from app.core.database import get_session
from app.main import app
from app.schemas.auth import UserContext


def _ctx(*roles: str, user_id: int = 1) -> UserContext:
    return UserContext(
        user_id=user_id,
        auth_user_id=uuid.UUID("11111111-1111-1111-1111-111111111111"),
        roles=list(roles),
    )


# --- pre-login throttle endpoints --------------------------------------------


class _ThrottleSession:
    """Fake session that records attempts in-memory for the throttle routes.

    Exposes only what app/services/login_lockout.py touches. ``user`` is the
    seeded registered user (or None for an unknown email).
    """

    def __init__(self, user=None):
        self.user = user
        self.attempts: dict = {}
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

    async def delete(self, obj):
        from app.models.login_attempt import LoginAttempt

        if isinstance(obj, LoginAttempt):
            self.attempts.pop(obj.email_lc, None)

    async def commit(self):
        self.commits += 1


@pytest.fixture
def throttle_client():
    session = _ThrottleSession(user=None)

    async def _session():
        yield session

    app.dependency_overrides[get_session] = _session
    with TestClient(app) as client:
        yield client, session
    app.dependency_overrides.clear()


def test_precheck_ok_when_no_prior_failures(throttle_client):
    client, _ = throttle_client
    resp = client.post("/auth/login/precheck", json={"email": "new@byu.edu"})
    assert resp.status_code == 200
    assert resp.json() == {
        "allowed": True,
        "reason": "ok",
        "retry_after_seconds": None,
    }


def test_record_failure_then_precheck_cooldown(throttle_client):
    client, session = throttle_client
    # Drive enough failures to trip the cooldown (COOLDOWN_THRESHOLD = 10).
    for _ in range(10):
        resp = client.post(
            "/auth/login/record", json={"email": "x@byu.edu", "success": False}
        )
    assert resp.status_code == 200
    body = resp.json()
    assert body["reason"] == "cooldown"
    assert body["allowed"] is False
    assert body["retry_after_seconds"] is not None

    # Precheck now reflects the cooldown.
    pre = client.post("/auth/login/precheck", json={"email": "x@byu.edu"})
    assert pre.json()["reason"] == "cooldown"


def test_record_rejects_unknown_field(throttle_client):
    client, _ = throttle_client
    resp = client.post(
        "/auth/login/record",
        json={"email": "x@byu.edu", "success": False, "extra": 1},
    )
    assert resp.status_code == 422


def test_record_success_response_shape_hides_locked(throttle_client):
    client, _ = throttle_client
    resp = client.post(
        "/auth/login/record", json={"email": "x@byu.edu", "success": True}
    )
    assert resp.status_code == 200
    # Anti-enumeration: the route never echoes the internal `locked` flag.
    assert set(resp.json()) == {"allowed", "reason", "retry_after_seconds"}


def test_record_success_does_not_clear_existing_cooldown(throttle_client):
    """An unauthenticated `success:true` MUST NOT wipe a set cooldown row.

    Otherwise an attacker could POST `{email, success:true}` to clear a
    legitimately-set cooldown and brute-force unbounded. The genuine clear only
    happens on the authenticated path (see get_current_db_user).
    """
    client, session = throttle_client
    # Trip the cooldown with COOLDOWN_THRESHOLD failures.
    for _ in range(10):
        client.post(
            "/auth/login/record", json={"email": "x@byu.edu", "success": False}
        )
    assert session.attempts  # a cooldown row exists
    row_before = session.attempts["x@byu.edu"]
    commits_before = session.commits

    # A "success" claim from the unauthenticated caller returns ok but leaves
    # the row (and counter) intact — and does not mutate/commit.
    resp = client.post(
        "/auth/login/record", json={"email": "x@byu.edu", "success": True}
    )
    assert resp.status_code == 200
    assert resp.json() == {
        "allowed": True,
        "reason": "ok",
        "retry_after_seconds": None,
    }
    # The cooldown row is untouched.
    assert session.attempts.get("x@byu.edu") is row_before
    assert session.commits == commits_before

    # And the cooldown is still in force on a subsequent precheck.
    pre = client.post("/auth/login/precheck", json={"email": "x@byu.edu"})
    assert pre.json()["reason"] == "cooldown"


# --- super_admin reset-password ----------------------------------------------


def _fake_user(user_id=2, *, locked_at=None, roles=("full_access",)):
    return SimpleNamespace(
        user_id=user_id,
        email=f"user{user_id}@byu.edu",
        first_name="Test",
        last_name="User",
        active=True,
        auth_user_id=uuid.UUID("22222222-2222-2222-2222-222222222222"),
        locked_at=locked_at,
        locked_reason="too_many_failed_logins" if locked_at else None,
        must_change_password=False,
        active_session_id=None,
        roles=[SimpleNamespace(role_name=r) for r in roles],
    )


class _ResetSession:
    """Fake session for the reset-password route: ``scalar`` returns the target
    user; ``execute`` (the login_attempts delete) and ``add``/``commit`` are
    recorded."""

    def __init__(self, user):
        self.user = user
        self.added: list = []
        self.executed: list = []
        self.commits = 0

    async def scalar(self, _stmt):
        return self.user

    def add(self, obj):
        self.added.append(obj)

    async def execute(self, stmt):
        self.executed.append(stmt)

    async def commit(self):
        self.commits += 1


def test_reset_password_forbidden_below_super_admin():
    app.dependency_overrides[get_current_db_user] = lambda: _ctx("full_access")

    async def _no_db():
        yield None

    app.dependency_overrides[get_session] = _no_db
    with TestClient(app) as client:
        resp = client.post("/admin/users/2/reset-password")
    app.dependency_overrides.clear()
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "forbidden"


def test_reset_password_requires_auth():
    async def _no_db():
        yield None

    app.dependency_overrides[get_session] = _no_db
    with TestClient(app) as client:
        resp = client.post("/admin/users/2/reset-password")
    app.dependency_overrides.clear()
    assert resp.status_code == 401


def test_reset_password_clears_lock_and_audits(monkeypatch):
    import uuid as _uuid

    user = _fake_user(2, locked_at="2026-06-13T00:00:00Z")
    session = _ResetSession(user)

    # Mock the Supabase Admin API call so no network happens; capture the args.
    calls: list = []

    async def _fake_set_password(auth_user_id, new_password):
        calls.append((auth_user_id, new_password))

    monkeypatch.setattr(admin_routes, "set_user_password", _fake_set_password)

    async def _session():
        yield session

    app.dependency_overrides[get_session] = _session
    app.dependency_overrides[get_current_db_user] = lambda: _ctx(
        "super_admin", user_id=1
    )
    with TestClient(app) as client:
        resp = client.post("/admin/users/2/reset-password")
    app.dependency_overrides.clear()

    assert resp.status_code == 200
    body = resp.json()
    # A non-trivial temp password is returned exactly once.
    assert "temp_password" in body
    assert isinstance(body["temp_password"], str)
    assert len(body["temp_password"]) >= 16

    # The lock was cleared on the user row.
    assert user.locked_at is None
    assert user.locked_reason is None

    # The Supabase admin call got the user's auth UUID and the SAME password
    # returned to the client (and only that — never logged elsewhere).
    assert len(calls) == 1
    assert calls[0][0] == _uuid.UUID("22222222-2222-2222-2222-222222222222")
    assert calls[0][1] == body["temp_password"]

    # The login_attempts row delete was issued.
    assert len(session.executed) == 1

    # An audit row was written by the super_admin actor against the target user.
    audit = next(a for a in session.added if type(a).__name__ == "AuditLog")
    assert audit.action_type == "reset_password"
    assert audit.entity_type == "user"
    assert audit.entity_id == 2
    assert audit.user_id == 1
    # Field detail is populated (FIX 2): the audited field is the password, the
    # prior state is recorded (locked, since this user was locked), new is reset.
    assert audit.field_name == "password"
    assert audit.old_value == "locked"
    assert audit.new_value == "reset"
    # The password must never appear in the audit row.
    assert body["temp_password"] not in (audit.old_value or "")
    assert body["temp_password"] not in (audit.new_value or "")
    assert session.commits == 1


def test_reset_password_upstream_failure_is_502_and_does_not_clear_lock(monkeypatch):
    from app.core.errors import ServiceError

    user = _fake_user(2, locked_at="2026-06-13T00:00:00Z")
    session = _ResetSession(user)

    async def _boom(auth_user_id, new_password):
        raise ServiceError("The authentication service rejected the password reset.")

    monkeypatch.setattr(admin_routes, "set_user_password", _boom)

    async def _session():
        yield session

    app.dependency_overrides[get_session] = _session
    app.dependency_overrides[get_current_db_user] = lambda: _ctx(
        "super_admin", user_id=1
    )
    with TestClient(app) as client:
        resp = client.post("/admin/users/2/reset-password")
    app.dependency_overrides.clear()

    assert resp.status_code == 502
    assert resp.json()["error"]["code"] == "service_unavailable"
    # The lock must remain because the auth-provider reset failed.
    assert user.locked_at is not None
    assert session.commits == 0


# --- authenticated-path login_attempts clear ---------------------------------


class _AuthClearSession:
    """Fake session for get_current_db_user: records executed statements and
    commits/rollbacks so we can assert the login_attempts clear fired."""

    def __init__(self):
        self.executed: list = []
        self.commits = 0
        self.rollbacks = 0

    async def execute(self, stmt):
        self.executed.append(stmt)

    async def commit(self):
        self.commits += 1

    async def rollback(self):
        self.rollbacks += 1


def test_authenticated_user_resolution_clears_login_attempts(monkeypatch):
    """Resolving an authenticated DB user clears that email's login_attempts row.

    This is the trustworthy success-clear that replaced the abusable
    unauthenticated `/auth/login/record {success:true}` path.
    """
    import asyncio

    from app.api.dependencies import auth as auth_deps
    from app.models.login_attempt import LoginAttempt

    auth_uuid = uuid.UUID("33333333-3333-3333-3333-333333333333")
    user = SimpleNamespace(
        user_id=7,
        auth_user_id=auth_uuid,
        email="Alum@BYU.edu",  # mixed case; clear must lowercase
        first_name="A",
        last_name="B",
        active=True,
        must_change_password=False,
        active_session_id=None,
        roles=[SimpleNamespace(role_name="view_only")],
    )

    async def _fake_lookup(session, _auth_id):
        return user

    monkeypatch.setattr(
        auth_deps, "get_user_with_roles_by_auth_id", _fake_lookup
    )

    session = _AuthClearSession()
    current = SimpleNamespace(auth_user_id=str(auth_uuid), session_id=None)

    ctx = asyncio.run(auth_deps.get_current_db_user(current, session))

    assert ctx.user_id == 7
    # Exactly one DELETE against login_attempts was issued and committed.
    assert len(session.executed) == 1
    stmt = session.executed[0]
    assert stmt.is_delete
    assert stmt.table.name == LoginAttempt.__tablename__
    # Lowercased-email keying in the WHERE clause.
    assert "alum@byu.edu" in str(stmt.compile(compile_kwargs={"literal_binds": True}))
    assert session.commits == 1
    assert session.rollbacks == 0


def test_authenticated_clear_failure_never_breaks_request(monkeypatch):
    """A failure clearing login_attempts must not break the authenticated request."""
    import asyncio

    from app.api.dependencies import auth as auth_deps

    auth_uuid = uuid.UUID("44444444-4444-4444-4444-444444444444")
    user = SimpleNamespace(
        user_id=8,
        auth_user_id=auth_uuid,
        email="x@byu.edu",
        first_name="A",
        last_name="B",
        active=True,
        must_change_password=False,
        active_session_id=None,
        roles=[SimpleNamespace(role_name="view_only")],
    )

    async def _fake_lookup(session, _auth_id):
        return user

    monkeypatch.setattr(
        auth_deps, "get_user_with_roles_by_auth_id", _fake_lookup
    )

    class _BoomSession:
        def __init__(self):
            self.rollbacks = 0

        async def execute(self, stmt):
            raise RuntimeError("db down")

        async def commit(self):
            raise RuntimeError("db down")

        async def rollback(self):
            self.rollbacks += 1

    session = _BoomSession()
    current = SimpleNamespace(auth_user_id=str(auth_uuid), session_id=None)

    # Must still resolve the user despite the clear blowing up.
    ctx = asyncio.run(auth_deps.get_current_db_user(current, session))
    assert ctx.user_id == 8
    assert session.rollbacks == 1
