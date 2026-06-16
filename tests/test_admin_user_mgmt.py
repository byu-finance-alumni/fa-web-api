"""Tests for the super_admin user-management routes: create a login user and
edit a user's name.

`POST /admin/users` provisions a Supabase auth identity (mocked here so no
network happens) then inserts a `users` row + `user_roles` row and returns a
one-time temp password. `PATCH /admin/users/{id}/name` edits the name and audits
each changed field. Both are super_admin-only.

The route logic runs against minimal in-memory fake sessions (no database),
mirroring tests/test_admin_routes.py and tests/test_login_routes.py.
"""

import uuid
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app.api.dependencies.auth import get_current_db_user
from app.api.routes import admin as admin_routes
from app.core.database import get_session
from app.main import app
from app.models.user import Role, User, UserRole
from app.schemas.auth import UserContext


def _ctx(*roles: str, user_id: int = 1) -> UserContext:
    return UserContext(
        user_id=user_id,
        auth_user_id=uuid.UUID("11111111-1111-1111-1111-111111111111"),
        roles=list(roles),
    )


async def _no_db_session():
    yield None


# --- create user -------------------------------------------------------------


class _CreateSession:
    """Fake session for the create-user route.

    ``scalar`` is call-order driven: 1) duplicate-email check (None unless an
    email is seeded), 2) role lookup (a seeded Role), 3+) the post-commit
    ``_load_user`` reload (the created user with roles). ``flush`` assigns a
    user_id so the role link + audit + reload can reference it.
    """

    def __init__(self, *, existing_email=None, role=None):
        self._existing_email = existing_email
        self._role = role
        self.added: list = []
        self.commits = 0
        self.flushes = 0
        self._scalar_calls = 0
        self._created_user: User | None = None

    async def scalar(self, _stmt):
        self._scalar_calls += 1
        if self._scalar_calls == 1:
            # duplicate-email check: return a user_id if seeded as existing
            return 99 if self._existing_email else None
        if self._scalar_calls == 2:
            return self._role
        # _load_user reload after commit -> return the created user with roles
        u = self._created_user
        if u is not None and self._role is not None:
            u.roles = [self._role]  # viewonly relationship stand-in
        return u

    def add(self, obj):
        self.added.append(obj)
        if isinstance(obj, User):
            self._created_user = obj

    async def flush(self):
        self.flushes += 1
        if self._created_user is not None and self._created_user.user_id is None:
            self._created_user.user_id = 7

    async def commit(self):
        self.commits += 1


def _seed_role(role_name="view_only", role_id=3) -> Role:
    role = Role(role_id=role_id, role_name=role_name)
    role.role_description = None
    return role


@pytest.fixture
def create_client():
    """Client wired with a fresh _CreateSession (no existing email) and a seeded
    view_only role, acting as a super_admin."""
    session = _CreateSession(role=_seed_role())

    async def _session():
        yield session

    app.dependency_overrides[get_session] = _session
    app.dependency_overrides[get_current_db_user] = lambda: _ctx(
        "super_admin", user_id=1
    )
    with TestClient(app) as test_client:
        yield test_client, session
    app.dependency_overrides.clear()


def test_create_user_requires_auth():
    app.dependency_overrides[get_session] = _no_db_session
    with TestClient(app) as client:
        resp = client.post("/admin/users", json={"email": "new@byu.edu"})
    app.dependency_overrides.clear()
    assert resp.status_code == 401


@pytest.mark.parametrize("role", ["view_only", "full_access"])
def test_create_user_forbidden_below_super_admin(role):
    app.dependency_overrides[get_session] = _no_db_session
    app.dependency_overrides[get_current_db_user] = lambda: _ctx(role)
    with TestClient(app) as client:
        resp = client.post("/admin/users", json={"email": "new@byu.edu"})
    app.dependency_overrides.clear()
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "forbidden"


def test_create_user_happy_path(create_client, monkeypatch):
    test_client, session = create_client

    new_auth_id = uuid.UUID("33333333-3333-3333-3333-333333333333")
    calls: list = []

    async def _fake_create_auth_user(email, password, email_confirm=True):
        calls.append((email, password, email_confirm))
        return new_auth_id

    monkeypatch.setattr(admin_routes, "create_auth_user", _fake_create_auth_user)

    resp = test_client.post(
        "/admin/users",
        json={
            "email": "New.User@BYU.edu",
            "first_name": "New",
            "last_name": "User",
            "role_name": "view_only",
        },
    )
    assert resp.status_code == 201
    body = resp.json()

    # Email is normalized to lowercase and echoed back.
    assert body["email"] == "new.user@byu.edu"
    assert body["first_name"] == "New"
    assert body["last_name"] == "User"
    assert body["active"] is True
    assert body["roles"] == ["view_only"]
    assert body["user_id"] == 7

    # A non-trivial one-time temp password is returned exactly once.
    assert isinstance(body["temp_password"], str)
    assert len(body["temp_password"]) >= 16

    # The Supabase admin create got the normalized email and the SAME password
    # returned to the client, with email_confirm=True.
    assert len(calls) == 1
    assert calls[0][0] == "new.user@byu.edu"
    assert calls[0][1] == body["temp_password"]
    assert calls[0][2] is True

    # A users row, a user_roles row, and a create_user audit were written.
    created_user = next(o for o in session.added if isinstance(o, User))
    assert created_user.auth_user_id == new_auth_id
    assert created_user.active is True
    assert any(isinstance(o, UserRole) for o in session.added)

    audit = next(a for a in session.added if type(a).__name__ == "AuditLog")
    assert audit.action_type == "create_user"
    assert audit.entity_type == "user"
    assert audit.entity_id == 7
    assert audit.user_id == 1  # the actor
    assert audit.new_value == "new.user@byu.edu"
    # The password must never appear in the audit row.
    assert body["temp_password"] not in (audit.new_value or "")
    assert body["temp_password"] not in (audit.old_value or "")
    assert session.commits == 1


def test_create_user_duplicate_email_is_409(monkeypatch):
    session = _CreateSession(existing_email="dupe@byu.edu", role=_seed_role())

    async def _session():
        yield session

    called = {"n": 0}

    async def _fake_create_auth_user(email, password, email_confirm=True):
        called["n"] += 1
        return uuid.uuid4()

    monkeypatch.setattr(admin_routes, "create_auth_user", _fake_create_auth_user)

    app.dependency_overrides[get_session] = _session
    app.dependency_overrides[get_current_db_user] = lambda: _ctx(
        "super_admin", user_id=1
    )
    with TestClient(app) as client:
        resp = client.post("/admin/users", json={"email": "dupe@byu.edu"})
    app.dependency_overrides.clear()

    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "conflict"
    # The auth provider must never be touched for a duplicate.
    assert called["n"] == 0
    assert session.commits == 0


def test_create_user_rejects_unknown_role(create_client):
    test_client, _ = create_client
    resp = test_client.post(
        "/admin/users", json={"email": "new@byu.edu", "role_name": "wizard"}
    )
    assert resp.status_code == 422


def test_create_user_rejects_bad_email(create_client):
    test_client, _ = create_client
    resp = test_client.post("/admin/users", json={"email": "not-an-email"})
    assert resp.status_code == 422


def test_create_user_rejects_bad_name(create_client):
    test_client, _ = create_client
    resp = test_client.post(
        "/admin/users",
        json={"email": "new@byu.edu", "first_name": "Robert'); DROP TABLE--"},
    )
    assert resp.status_code == 422


def test_create_user_defaults_to_view_only(create_client, monkeypatch):
    test_client, _ = create_client

    async def _fake_create_auth_user(email, password, email_confirm=True):
        return uuid.UUID("44444444-4444-4444-4444-444444444444")

    monkeypatch.setattr(admin_routes, "create_auth_user", _fake_create_auth_user)

    resp = test_client.post("/admin/users", json={"email": "default@byu.edu"})
    assert resp.status_code == 201
    assert resp.json()["roles"] == ["view_only"]


# --- edit user name ----------------------------------------------------------


def _fake_user(user_id=2, *, first_name="Test", last_name="User"):
    return SimpleNamespace(
        user_id=user_id,
        email=f"user{user_id}@byu.edu",
        first_name=first_name,
        last_name=last_name,
        active=True,
        auth_user_id=uuid.UUID("22222222-2222-2222-2222-222222222222"),
        locked_at=None,
        locked_reason=None,
        roles=[SimpleNamespace(role_name="view_only")],
    )


class _NameSession:
    """Fake session for the name-edit route: ``scalar`` returns the target user
    (every call — the route reloads after commit); ``add``/``commit`` recorded."""

    def __init__(self, user):
        self.user = user
        self.added: list = []
        self.commits = 0

    async def scalar(self, _stmt):
        return self.user

    def add(self, obj):
        self.added.append(obj)

    async def commit(self):
        self.commits += 1


def test_update_name_requires_auth():
    app.dependency_overrides[get_session] = _no_db_session
    with TestClient(app) as client:
        resp = client.patch("/admin/users/2/name", json={"first_name": "X"})
    app.dependency_overrides.clear()
    assert resp.status_code == 401


@pytest.mark.parametrize("role", ["view_only", "full_access"])
def test_update_name_forbidden_below_super_admin(role):
    app.dependency_overrides[get_session] = _no_db_session
    app.dependency_overrides[get_current_db_user] = lambda: _ctx(role)
    with TestClient(app) as client:
        resp = client.patch("/admin/users/2/name", json={"first_name": "X"})
    app.dependency_overrides.clear()
    assert resp.status_code == 403


def test_update_name_changes_fields_and_audits_each():
    user = _fake_user(2, first_name="Old", last_name="Name")
    session = _NameSession(user)

    async def _session():
        yield session

    app.dependency_overrides[get_session] = _session
    app.dependency_overrides[get_current_db_user] = lambda: _ctx(
        "super_admin", user_id=1
    )
    with TestClient(app) as client:
        resp = client.patch(
            "/admin/users/2/name",
            json={"first_name": "New", "last_name": "Person"},
        )
    app.dependency_overrides.clear()

    assert resp.status_code == 200
    body = resp.json()
    assert body["first_name"] == "New"
    assert body["last_name"] == "Person"
    assert user.first_name == "New"
    assert user.last_name == "Person"

    audits = [a for a in session.added if type(a).__name__ == "AuditLog"]
    assert len(audits) == 2
    by_field = {a.field_name: a for a in audits}
    assert by_field["first_name"].old_value == "Old"
    assert by_field["first_name"].new_value == "New"
    assert by_field["last_name"].old_value == "Name"
    assert by_field["last_name"].new_value == "Person"
    for a in audits:
        assert a.action_type == "update_user"
        assert a.entity_type == "user"
        assert a.entity_id == 2
        assert a.user_id == 1
    assert session.commits == 1


def test_update_name_only_changed_field_is_audited():
    user = _fake_user(2, first_name="Same", last_name="Name")
    session = _NameSession(user)

    async def _session():
        yield session

    app.dependency_overrides[get_session] = _session
    app.dependency_overrides[get_current_db_user] = lambda: _ctx(
        "super_admin", user_id=1
    )
    with TestClient(app) as client:
        # first_name unchanged, last_name changed -> only last_name audited.
        resp = client.patch(
            "/admin/users/2/name",
            json={"first_name": "Same", "last_name": "Changed"},
        )
    app.dependency_overrides.clear()

    assert resp.status_code == 200
    audits = [a for a in session.added if type(a).__name__ == "AuditLog"]
    assert len(audits) == 1
    assert audits[0].field_name == "last_name"
    assert session.commits == 1


def test_update_name_noop_is_idempotent():
    user = _fake_user(2, first_name="Test", last_name="User")
    session = _NameSession(user)

    async def _session():
        yield session

    app.dependency_overrides[get_session] = _session
    app.dependency_overrides[get_current_db_user] = lambda: _ctx(
        "super_admin", user_id=1
    )
    with TestClient(app) as client:
        # No fields sent -> nothing changes, no audit, no commit.
        resp = client.patch("/admin/users/2/name", json={})
    app.dependency_overrides.clear()

    assert resp.status_code == 200
    assert not any(type(a).__name__ == "AuditLog" for a in session.added)
    assert session.commits == 0


def test_update_name_rejects_active_field():
    # `active` belongs to the other PATCH endpoint; this one forbids it.
    user = _fake_user(2)
    session = _NameSession(user)

    async def _session():
        yield session

    app.dependency_overrides[get_session] = _session
    app.dependency_overrides[get_current_db_user] = lambda: _ctx(
        "super_admin", user_id=1
    )
    with TestClient(app) as client:
        resp = client.patch(
            "/admin/users/2/name", json={"first_name": "X", "active": False}
        )
    app.dependency_overrides.clear()
    assert resp.status_code == 422
