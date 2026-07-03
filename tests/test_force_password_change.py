"""Tests for the "force password change on first login" feature.

Covers the four contract points, all offline against fake in-memory sessions
(mirroring tests/test_admin_user_mgmt.py and tests/test_login_routes.py):

  * POST /admin/users sets must_change_password=True on the new row.
  * POST /admin/users/{id}/reset-password sets must_change_password=True.
  * GET /auth/context echoes the current user's must_change_password flag.
  * POST /auth/password/complete clears ONLY the caller's flag, requires auth
    (401 unauth), is idempotent, and acts on the token's user — it takes no id,
    so it can never clear someone else's flag.
"""

import uuid
from types import SimpleNamespace

from fastapi.testclient import TestClient

from app.api.dependencies import auth as auth_deps
from app.api.dependencies.auth import (
    get_current_db_user,
    get_current_db_user_allow_must_change,
    get_current_user,
)
from app.api.routes import admin as admin_routes
from app.core.database import get_session
from app.main import app
from app.models.user import Role, User
from app.schemas.auth import AuthenticatedUser, UserContext


def _ctx(*roles: str, user_id: int = 1, must_change_password: bool = False):
    return UserContext(
        user_id=user_id,
        auth_user_id=uuid.UUID("11111111-1111-1111-1111-111111111111"),
        roles=list(roles),
        must_change_password=must_change_password,
    )


async def _no_db_session():
    yield None


# --- create-user sets the flag -----------------------------------------------


class _CreateSession:
    """Fake session for the create-user route (see test_admin_user_mgmt)."""

    def __init__(self, *, role=None):
        self._role = role
        self.added: list = []
        self.commits = 0
        self._scalar_calls = 0
        self._created_user: User | None = None

    async def scalar(self, _stmt):
        self._scalar_calls += 1
        if self._scalar_calls == 1:
            return None  # duplicate-email check
        if self._scalar_calls == 2:
            return self._role
        u = self._created_user
        if u is not None and self._role is not None:
            u.roles = [self._role]
        return u

    def add(self, obj):
        self.added.append(obj)
        if isinstance(obj, User):
            self._created_user = obj

    async def flush(self):
        if self._created_user is not None and self._created_user.user_id is None:
            self._created_user.user_id = 7

    async def commit(self):
        self.commits += 1


def test_create_user_sets_must_change_password(monkeypatch):
    role = Role(role_id=3, role_name="view_only")
    role.role_description = None
    session = _CreateSession(role=role)

    async def _session():
        yield session

    async def _fake_create_auth_user(email, password, email_confirm=True):
        return uuid.UUID("33333333-3333-3333-3333-333333333333")

    monkeypatch.setattr(admin_routes, "create_auth_user", _fake_create_auth_user)

    app.dependency_overrides[get_session] = _session
    app.dependency_overrides[get_current_db_user] = lambda: _ctx(
        "super_admin", user_id=1
    )
    with TestClient(app) as client:
        resp = client.post("/admin/users", json={"email": "new@byu.edu"})
    app.dependency_overrides.clear()

    assert resp.status_code == 201
    created = next(o for o in session.added if isinstance(o, User))
    assert created.must_change_password is True


# --- reset-password sets the flag --------------------------------------------


def _reset_user(user_id=2):
    return SimpleNamespace(
        user_id=user_id,
        email=f"user{user_id}@byu.edu",
        first_name="Test",
        last_name="User",
        active=True,
        auth_user_id=uuid.UUID("22222222-2222-2222-2222-222222222222"),
        locked_at=None,
        locked_reason=None,
        must_change_password=False,
        roles=[SimpleNamespace(role_name="full_access")],
    )


class _ResetSession:
    def __init__(self, user):
        self.user = user
        self.added: list = []
        self.commits = 0

    async def scalar(self, _stmt):
        return self.user

    def add(self, obj):
        self.added.append(obj)

    async def execute(self, _stmt):
        pass

    async def commit(self):
        self.commits += 1


def test_reset_password_sets_must_change_password(monkeypatch):
    user = _reset_user(2)
    session = _ResetSession(user)

    async def _fake_set_password(auth_user_id, new_password):
        pass

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
    assert user.must_change_password is True


# --- /auth/context exposes the flag ------------------------------------------


def test_context_returns_must_change_password_true():
    # /auth/context is EXEMPT from the gate, so it depends on the exempt
    # resolver — override that one (a flagged user must read this route).
    app.dependency_overrides[get_session] = _no_db_session
    app.dependency_overrides[get_current_db_user_allow_must_change] = (
        lambda: _ctx("view_only", user_id=5, must_change_password=True)
    )
    with TestClient(app) as client:
        resp = client.get("/auth/context")
    app.dependency_overrides.clear()

    assert resp.status_code == 200
    assert resp.json()["must_change_password"] is True


def test_context_returns_must_change_password_false():
    app.dependency_overrides[get_session] = _no_db_session
    app.dependency_overrides[get_current_db_user_allow_must_change] = (
        lambda: _ctx("view_only", user_id=5, must_change_password=False)
    )
    with TestClient(app) as client:
        resp = client.get("/auth/context")
    app.dependency_overrides.clear()

    assert resp.status_code == 200
    assert resp.json()["must_change_password"] is False


# --- POST /auth/password/complete --------------------------------------------


class _CompleteSession:
    """Fake session for the complete route: ``scalar`` returns the seeded row
    (the route loads the User by user_id); ``add``/``commit`` recorded."""

    def __init__(self, user):
        self.user = user
        self.added: list = []
        self.commits = 0
        self.scalar_calls = 0

    async def scalar(self, _stmt):
        self.scalar_calls += 1
        return self.user

    def add(self, obj):
        self.added.append(obj)

    async def commit(self):
        self.commits += 1


def _db_user(user_id, *, must_change_password):
    return SimpleNamespace(
        user_id=user_id,
        must_change_password=must_change_password,
    )


def test_password_complete_requires_auth():
    app.dependency_overrides[get_session] = _no_db_session
    with TestClient(app) as client:
        resp = client.post("/auth/password/complete")
    app.dependency_overrides.clear()
    assert resp.status_code == 401
    assert resp.json()["error"]["code"] == "unauthorized"


def test_password_complete_clears_callers_flag_and_audits():
    user = _db_user(5, must_change_password=True)
    session = _CompleteSession(user)

    async def _session():
        yield session

    app.dependency_overrides[get_session] = _session
    app.dependency_overrides[get_current_db_user_allow_must_change] = (
        lambda: _ctx("view_only", user_id=5, must_change_password=True)
    )
    with TestClient(app) as client:
        resp = client.post("/auth/password/complete")
    app.dependency_overrides.clear()

    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}
    # ONLY the caller's row was flipped.
    assert user.must_change_password is False
    audit = next(a for a in session.added if type(a).__name__ == "AuditLog")
    assert audit.action_type == "password_changed"
    assert audit.entity_type == "user"
    assert audit.entity_id == 5
    assert audit.user_id == 5  # acted on self
    assert session.commits == 1


def test_password_complete_uses_token_user_not_any_supplied_id():
    """The endpoint takes no id and loads strictly by the token's user_id, so it
    can only ever touch the caller's own row — a body/query id is ignored."""
    user = _db_user(5, must_change_password=True)
    session = _CompleteSession(user)
    captured: dict = {}

    async def _capturing_scalar(stmt):
        # Confirm the WHERE clause keys on the token's user_id (5), never some
        # attacker-supplied victim id.
        captured["sql"] = str(stmt.compile(compile_kwargs={"literal_binds": True}))
        return user

    session.scalar = _capturing_scalar  # type: ignore[method-assign]

    async def _session():
        yield session

    app.dependency_overrides[get_session] = _session
    app.dependency_overrides[get_current_db_user_allow_must_change] = (
        lambda: _ctx("view_only", user_id=5, must_change_password=True)
    )
    with TestClient(app) as client:
        # Attempt to smuggle a victim id in the body — it must be ignored.
        resp = client.post("/auth/password/complete", json={"user_id": 999})
    app.dependency_overrides.clear()

    assert resp.status_code == 200
    assert user.must_change_password is False
    assert "user_id = 5" in captured["sql"]
    assert "999" not in captured["sql"]


def test_password_complete_is_idempotent_when_already_false():
    user = _db_user(5, must_change_password=False)
    session = _CompleteSession(user)

    async def _session():
        yield session

    app.dependency_overrides[get_session] = _session
    app.dependency_overrides[get_current_db_user_allow_must_change] = (
        lambda: _ctx("view_only", user_id=5, must_change_password=False)
    )
    with TestClient(app) as client:
        resp = client.post("/auth/password/complete")
    app.dependency_overrides.clear()

    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}
    # No change -> no audit, no commit.
    assert not any(type(a).__name__ == "AuditLog" for a in session.added)
    assert session.commits == 0


# --- backend gate enforcement (appsec CRITICAL) ------------------------------
#
# The force-change gate must be enforced server-side, not only in the frontend:
# a user holding a valid session token while must_change_password=True must be
# 403'd on data endpoints, yet still able to reach the two routes that let them
# read and clear the flag.
#
# The data-endpoint tests drive the REAL ``get_current_db_user`` end to end (so
# the MustChangePasswordError raise is genuinely exercised) by overriding only
# the token identity (``get_current_user``) and patching the DB lookup — mirroring
# tests/test_authz.py. The two exempt routes override the exempt resolver they
# actually depend on.

AUTH_UUID = uuid.UUID("11111111-1111-1111-1111-111111111111")


def _orm_user(*, must_change_password: bool, active: bool = True):
    """A fake ORM user row the patched repository lookup returns, so the real
    get_current_db_user resolves it (and applies/clears the gate)."""
    return SimpleNamespace(
        user_id=5,
        auth_user_id=AUTH_UUID,
        email="worker@byu.edu",
        first_name="Test",
        last_name="Worker",
        active=active,
        must_change_password=must_change_password,
        active_session_id=None,
        roles=[SimpleNamespace(role_name="view_only")],
    )


def test_flagged_user_is_403_on_protected_data_endpoint(monkeypatch):
    """A must_change_password=True user is blocked (403 password_change_required)
    on a normal RequireViewAccess route (GET /alumni), via the real auth chain."""

    async def _fake_lookup(session, auth_uuid):
        return _orm_user(must_change_password=True)

    async def _noop_clear(session, email):
        return None

    monkeypatch.setattr(auth_deps, "get_user_with_roles_by_auth_id", _fake_lookup)
    monkeypatch.setattr(auth_deps, "_clear_login_attempts", _noop_clear)

    app.dependency_overrides[get_session] = _no_db_session
    app.dependency_overrides[get_current_user] = lambda: AuthenticatedUser(
        auth_user_id=str(AUTH_UUID), email="worker@byu.edu"
    )
    with TestClient(app) as client:
        resp = client.get("/alumni")
    app.dependency_overrides.clear()

    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "password_change_required"


def test_flagged_user_can_reach_context():
    """The exempt GET /auth/context stays reachable while flagged and returns
    the flag (so the frontend can route into the set-password screen)."""
    app.dependency_overrides[get_session] = _no_db_session
    app.dependency_overrides[get_current_db_user_allow_must_change] = (
        lambda: _ctx("view_only", user_id=5, must_change_password=True)
    )
    with TestClient(app) as client:
        resp = client.get("/auth/context")
    app.dependency_overrides.clear()

    assert resp.status_code == 200
    assert resp.json()["must_change_password"] is True


def test_flagged_user_can_reach_password_complete():
    """The exempt POST /auth/password/complete stays reachable while flagged and
    clears the flag (200)."""
    user = _db_user(5, must_change_password=True)
    session = _CompleteSession(user)

    async def _session():
        yield session

    app.dependency_overrides[get_session] = _session
    app.dependency_overrides[get_current_db_user_allow_must_change] = (
        lambda: _ctx("view_only", user_id=5, must_change_password=True)
    )
    with TestClient(app) as client:
        resp = client.post("/auth/password/complete")
    app.dependency_overrides.clear()

    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}
    assert user.must_change_password is False


def test_user_reaches_protected_endpoint_again_after_flag_cleared(monkeypatch):
    """Once must_change_password is false, the gate no longer fires: the same
    user reaches the normal protected endpoint (GET /alumni, 200), via the real
    auth chain. The service is stubbed so the test stays offline."""
    from app.api.routes import alumni as alumni_routes

    async def _fake_lookup(session, auth_uuid):
        return _orm_user(must_change_password=False)

    async def _noop_clear(session, email):
        return None

    async def _fake_list_alumni(*_args, **_kwargs):
        return [], 0

    monkeypatch.setattr(auth_deps, "get_user_with_roles_by_auth_id", _fake_lookup)
    monkeypatch.setattr(auth_deps, "_clear_login_attempts", _noop_clear)
    monkeypatch.setattr(alumni_routes.service, "list_alumni", _fake_list_alumni)

    app.dependency_overrides[get_session] = _no_db_session
    app.dependency_overrides[get_current_user] = lambda: AuthenticatedUser(
        auth_user_id=str(AUTH_UUID), email="worker@byu.edu"
    )
    with TestClient(app) as client:
        resp = client.get("/alumni")
    app.dependency_overrides.clear()

    assert resp.status_code == 200
