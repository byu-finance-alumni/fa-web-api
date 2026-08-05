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

from app.api.dependencies.auth import get_current_db_user, get_permission_config
from app.api.routes import admin as admin_routes
from app.core.capabilities import Capability
from app.core.database import get_session
from app.core.errors import ServiceError
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


def test_create_user_supabase_failure_is_502_and_writes_nothing(
    create_client, monkeypatch
):
    """If the Supabase auth create fails, the request is a 502 and NO users /
    user_roles rows are committed. delete_auth_user is not attempted because no
    auth identity was created in the first place."""
    test_client, session = create_client

    async def _failing_create_auth_user(email, password, email_confirm=True):
        raise ServiceError("upstream down")

    delete_calls: list = []

    async def _fake_delete_auth_user(auth_user_id):
        delete_calls.append(auth_user_id)

    monkeypatch.setattr(admin_routes, "create_auth_user", _failing_create_auth_user)
    monkeypatch.setattr(admin_routes, "delete_auth_user", _fake_delete_auth_user)

    resp = test_client.post("/admin/users", json={"email": "fail@byu.edu"})

    assert resp.status_code == 502
    assert resp.json()["error"]["code"] == "service_unavailable"
    # Nothing was committed, and no user / user_roles rows were written.
    assert session.commits == 0
    assert not any(isinstance(o, User) for o in session.added)
    assert not any(isinstance(o, UserRole) for o in session.added)
    # The auth identity was never created, so there is nothing to compensate.
    assert delete_calls == []


def test_create_user_db_failure_triggers_compensating_delete(
    create_client, monkeypatch
):
    """If the auth identity is created but the DB commit fails, the route
    best-effort deletes the orphaned auth user and re-raises the original error
    (502)."""
    test_client, session = create_client

    new_auth_id = uuid.UUID("55555555-5555-5555-5555-555555555555")

    async def _fake_create_auth_user(email, password, email_confirm=True):
        return new_auth_id

    async def _boom_commit():
        raise RuntimeError("db down")

    delete_calls: list = []

    async def _fake_delete_auth_user(auth_user_id):
        delete_calls.append(auth_user_id)

    monkeypatch.setattr(admin_routes, "create_auth_user", _fake_create_auth_user)
    monkeypatch.setattr(admin_routes, "delete_auth_user", _fake_delete_auth_user)
    monkeypatch.setattr(session, "commit", _boom_commit)

    # The ORIGINAL error must propagate (not be swallowed). The TestClient
    # re-raises server-side exceptions, so we assert the same RuntimeError
    # surfaces — and that the compensating delete was attempted first.
    with pytest.raises(RuntimeError, match="db down"):
        test_client.post("/admin/users", json={"email": "dbfail@byu.edu"})

    assert delete_calls == [new_auth_id]
    # The failed transaction was not committed.
    assert session.commits == 0


def test_create_user_rejects_role_above_actor_tier(create_client):
    """Privilege ceiling: a super_admin actor cannot create a user with a role
    ABOVE their tier (engineer) -> 403."""
    test_client, _ = create_client
    resp = test_client.post(
        "/admin/users", json={"email": "boss@byu.edu", "role_name": "engineer"}
    )
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "forbidden"


def test_create_user_allows_role_at_actor_tier(create_client, monkeypatch):
    """The relaxation: a super_admin actor CAN now create another super_admin
    (equal tier) — previously a hard 422. The ceiling guard passes and creation
    proceeds (an engineer could likewise create any role below engineer)."""
    test_client, _ = create_client

    async def _fake_create_auth_user(email, password, email_confirm=True):
        return uuid.UUID("44444444-4444-4444-4444-444444444444")

    monkeypatch.setattr(admin_routes, "create_auth_user", _fake_create_auth_user)
    resp = test_client.post(
        "/admin/users",
        json={"email": "peer@byu.edu", "role_name": "super_admin"},
    )
    assert resp.status_code == 201


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
        created_at=None,
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


# --- delete user (permanent) -------------------------------------------------


def _del_user(user_id=5, *, email=None, roles=("view_only",)):
    return SimpleNamespace(
        user_id=user_id,
        email=email or f"user{user_id}@byu.edu",
        first_name="Del",
        last_name="Ete",
        active=True,
        auth_user_id=uuid.UUID("22222222-2222-2222-2222-222222222222"),
        locked_at=None,
        created_at=None,
        roles=[SimpleNamespace(role_name=r) for r in roles],
    )


class _DeleteSession:
    """Fake session for the delete-user route.

    ``scalar`` is call-order driven: call 1 is ``_load_user`` (the target). If
    the target holds a top role, the last-holder guard then issues a Role lookup
    followed by a holders COUNT per top role — modelled here as alternating
    role/``holders`` returns. ``delete`` and ``add``/``commit`` are recorded.
    """

    def __init__(self, user, *, role=None, holders=2):
        self.user = user
        self._role = role
        self._holders = holders
        self.added: list = []
        self.deleted: list = []
        self.commits = 0
        self._calls = 0

    async def scalar(self, _stmt):
        self._calls += 1
        if self._calls == 1:
            return self.user  # _load_user
        # guard pairs: even call -> Role lookup, odd call -> holders COUNT
        return self._role if self._calls % 2 == 0 else self._holders

    def add(self, obj):
        self.added.append(obj)

    async def delete(self, obj):
        self.deleted.append(obj)

    async def commit(self):
        self.commits += 1


class _RoleCountSession:
    """Fake session that answers the delete guard's holder-count queries per role.

    Unlike ``_DeleteSession`` (call-order driven, one holder count for all roles),
    this inspects each statement so the last-holder guard — which now counts
    super_admin AND engineer holders independently — can be modelled precisely. A
    ``User`` select returns the target; a ``Role`` select returns a Role for the
    requested ``role_name``; a COUNT returns ``holders_by_role`` for the role_id
    resolved from that Role.
    """

    _ROLE_IDS = {"engineer": 1, "super_admin": 2}

    def __init__(self, user, *, holders_by_role):
        self.user = user
        self.holders_by_role = holders_by_role
        self._id_to_name = {v: k for k, v in self._ROLE_IDS.items()}
        self.added: list = []
        self.deleted: list = []
        self.commits = 0

    async def scalar(self, stmt):
        desc = stmt.column_descriptions
        entity = desc[0]["entity"]
        params = stmt.compile().params
        if entity is not None and entity.__name__ == "User":
            return self.user
        if entity is not None and entity.__name__ == "Role":
            role_name = params.get("role_name_1")
            role_id = self._ROLE_IDS.get(role_name)
            if role_id is None:
                return None
            return SimpleNamespace(role_id=role_id, role_name=role_name)
        # COUNT query: resolve the role from the role_id bound param.
        role_name = self._id_to_name.get(params.get("role_id_1"))
        return self.holders_by_role.get(role_name, 0)

    def add(self, obj):
        self.added.append(obj)

    async def delete(self, obj):
        self.deleted.append(obj)

    async def commit(self):
        self.commits += 1


def test_delete_user_requires_auth():
    app.dependency_overrides[get_session] = _no_db_session
    with TestClient(app) as client:
        resp = client.delete("/admin/users/5")
    app.dependency_overrides.clear()
    assert resp.status_code == 401


@pytest.mark.parametrize("role", ["view_only", "full_access", "student"])
def test_delete_user_forbidden_below_super_admin(role):
    app.dependency_overrides[get_session] = _no_db_session
    app.dependency_overrides[get_current_db_user] = lambda: _ctx(role)
    with TestClient(app) as client:
        resp = client.delete("/admin/users/5")
    app.dependency_overrides.clear()
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "forbidden"


def test_delete_user_happy_path(monkeypatch):
    """super_admin deletes a view_only user: DB row deleted + audited, and the
    Supabase auth identity is removed."""
    user = _del_user(5, email="gone@byu.edu")
    session = _DeleteSession(user)

    deleted_auth: list = []

    async def _fake_delete_auth_user(auth_user_id):
        deleted_auth.append(auth_user_id)

    monkeypatch.setattr(admin_routes, "delete_auth_user", _fake_delete_auth_user)

    async def _session():
        yield session

    app.dependency_overrides[get_session] = _session
    app.dependency_overrides[get_current_db_user] = lambda: _ctx(
        "super_admin", user_id=1
    )
    with TestClient(app) as client:
        resp = client.delete("/admin/users/5")
    app.dependency_overrides.clear()

    assert resp.status_code == 200
    body = resp.json()
    assert body == {"deleted": True, "user_id": 5, "email": "gone@byu.edu"}
    # The user row was deleted, the auth identity removed, and a delete_user
    # audit (recording the email) was written attributed to the actor.
    assert session.deleted == [user]
    assert deleted_auth == [user.auth_user_id]
    audit = next(a for a in session.added if a.action_type == "delete_user")
    assert audit.user_id == 1
    assert audit.entity_id == 5
    assert audit.old_value == "gone@byu.edu"


def test_delete_user_cannot_delete_self():
    session = _DeleteSession(_del_user(1))

    async def _session():
        yield session

    app.dependency_overrides[get_session] = _session
    app.dependency_overrides[get_current_db_user] = lambda: _ctx(
        "super_admin", user_id=1
    )
    with TestClient(app) as client:
        resp = client.delete("/admin/users/1")
    app.dependency_overrides.clear()
    assert resp.status_code == 409
    assert session.deleted == []  # nothing removed


def test_delete_user_engineer_requires_engineer():
    """A super_admin cannot delete a user who holds the engineer role."""
    user = _del_user(5, roles=("engineer",))
    session = _DeleteSession(user)

    async def _session():
        yield session

    app.dependency_overrides[get_session] = _session
    app.dependency_overrides[get_current_db_user] = lambda: _ctx(
        "super_admin", user_id=1
    )
    with TestClient(app) as client:
        resp = client.delete("/admin/users/5")
    app.dependency_overrides.clear()
    assert resp.status_code == 403
    assert session.deleted == []


def test_delete_user_last_super_admin_blocked_when_no_engineer():
    """Deleting the final super_admin is refused ONLY when no engineer remains to
    administer users (engineer ⊇ super_admin)."""
    user = _del_user(5, roles=("super_admin",))
    # 1 super_admin (the target), 0 engineers -> deleting it locks admin out.
    session = _RoleCountSession(
        user, holders_by_role={"super_admin": 1, "engineer": 0}
    )

    async def _session():
        yield session

    app.dependency_overrides[get_session] = _session
    # Actor is a super_admin (there is no engineer in this system).
    app.dependency_overrides[get_current_db_user] = lambda: _ctx(
        "super_admin", user_id=1
    )
    with TestClient(app) as client:
        resp = client.delete("/admin/users/5")
    app.dependency_overrides.clear()
    assert resp.status_code == 409
    assert session.deleted == []


def test_delete_user_engineer_deletes_last_super_admin_allowed(monkeypatch):
    """The bug fix: an engineer (top role, retains every super_admin capability)
    CAN delete the sole super_admin — administration is not locked out because the
    engineer tier still administers users."""
    user = _del_user(5, roles=("super_admin",), email="onlysa@byu.edu")
    # 1 super_admin (the target) but an engineer remains -> deletion is safe.
    session = _RoleCountSession(
        user, holders_by_role={"super_admin": 1, "engineer": 1}
    )

    deleted_auth: list = []

    async def _fake_delete_auth_user(auth_user_id):
        deleted_auth.append(auth_user_id)

    monkeypatch.setattr(admin_routes, "delete_auth_user", _fake_delete_auth_user)

    async def _session():
        yield session

    app.dependency_overrides[get_session] = _session
    app.dependency_overrides[get_current_db_user] = lambda: _ctx(
        "engineer", user_id=1
    )
    with TestClient(app) as client:
        resp = client.delete("/admin/users/5")
    app.dependency_overrides.clear()

    assert resp.status_code == 200
    assert resp.json() == {
        "deleted": True,
        "user_id": 5,
        "email": "onlysa@byu.edu",
    }
    assert session.deleted == [user]
    assert deleted_auth == [user.auth_user_id]


def test_delete_user_last_engineer_blocked():
    """The last engineer is always protected — the engineer holds unique
    vocab/database powers no other role can, so it is irreplaceable."""
    user = _del_user(5, roles=("engineer",))
    session = _RoleCountSession(user, holders_by_role={"engineer": 1})

    async def _session():
        yield session

    app.dependency_overrides[get_session] = _session
    app.dependency_overrides[get_current_db_user] = lambda: _ctx(
        "engineer", user_id=1
    )
    with TestClient(app) as client:
        resp = client.delete("/admin/users/5")
    app.dependency_overrides.clear()
    assert resp.status_code == 409
    assert session.deleted == []


def test_delete_user_engineer_deletes_super_admin(monkeypatch):
    """An engineer may permanently delete a super_admin (top role deletes the
    tier below it), as long as it is not the last super_admin."""
    user = _del_user(5, roles=("super_admin",), email="sa@byu.edu")
    role = SimpleNamespace(role_id=2, role_name="super_admin")
    session = _DeleteSession(user, role=role, holders=2)

    deleted_auth: list = []

    async def _fake_delete_auth_user(auth_user_id):
        deleted_auth.append(auth_user_id)

    monkeypatch.setattr(admin_routes, "delete_auth_user", _fake_delete_auth_user)

    async def _session():
        yield session

    app.dependency_overrides[get_session] = _session
    app.dependency_overrides[get_current_db_user] = lambda: _ctx(
        "engineer", user_id=1
    )
    with TestClient(app) as client:
        resp = client.delete("/admin/users/5")
    app.dependency_overrides.clear()

    assert resp.status_code == 200
    assert resp.json() == {"deleted": True, "user_id": 5, "email": "sa@byu.edu"}
    assert session.deleted == [user]
    assert deleted_auth == [user.auth_user_id]


def test_delete_user_404_when_missing():
    session = _DeleteSession(None)

    async def _session():
        yield session

    app.dependency_overrides[get_session] = _session
    app.dependency_overrides[get_current_db_user] = lambda: _ctx(
        "super_admin", user_id=1
    )
    with TestClient(app) as client:
        resp = client.delete("/admin/users/5")
    app.dependency_overrides.clear()
    assert resp.status_code == 404


# --- delegated USER_ADMIN cannot self-escalate (#178) ------------------------
#
# The role-mutation route guard is the USER_ADMIN capability, which an engineer
# can delegate to a lower role in the permission editor. The tier ceiling in the
# route body must still stop that role from granting/removing a role above its
# own tier. We model the delegation by overriding the live permission config so
# ``full_access`` holds USER_ADMIN, then assert the escalation is refused.

_DELEGATED_USER_ADMIN = {
    "full_access": frozenset(
        {
            Capability.VIEW,
            Capability.ALUMNI_EDIT,
            Capability.USER_ADMIN,
        }
    ),
}


def test_delegated_user_admin_cannot_assign_super_admin():
    """A full_access actor granted USER_ADMIN passes the route guard but is
    blocked by the tier ceiling from minting a super_admin -> 403 (before any
    DB access)."""
    app.dependency_overrides[get_session] = _no_db_session
    app.dependency_overrides[get_permission_config] = lambda: _DELEGATED_USER_ADMIN
    app.dependency_overrides[get_current_db_user] = lambda: _ctx(
        "full_access", user_id=1
    )
    with TestClient(app) as client:
        resp = client.post(
            "/admin/users/2/roles", json={"role_name": "super_admin"}
        )
    app.dependency_overrides.clear()
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "forbidden"


def test_delegated_user_admin_cannot_remove_super_admin():
    """Symmetric with assign: the delegated actor cannot strip a super_admin
    role either -> 403 before any DB access."""
    app.dependency_overrides[get_session] = _no_db_session
    app.dependency_overrides[get_permission_config] = lambda: _DELEGATED_USER_ADMIN
    app.dependency_overrides[get_current_db_user] = lambda: _ctx(
        "full_access", user_id=1
    )
    with TestClient(app) as client:
        resp = client.delete("/admin/users/2/roles/super_admin")
    app.dependency_overrides.clear()
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "forbidden"


def test_delegated_user_admin_cannot_delete_super_admin():
    """The delegated actor cannot permanently delete a user who holds a role
    above its tier (super_admin) -> 403."""
    user = _del_user(5, roles=("super_admin",))
    session = _DeleteSession(user)

    async def _session():
        yield session

    app.dependency_overrides[get_session] = _session
    app.dependency_overrides[get_permission_config] = lambda: _DELEGATED_USER_ADMIN
    app.dependency_overrides[get_current_db_user] = lambda: _ctx(
        "full_access", user_id=1
    )
    with TestClient(app) as client:
        resp = client.delete("/admin/users/5")
    app.dependency_overrides.clear()
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "forbidden"
    assert session.deleted == []


def test_delete_user_auth_failure_still_succeeds(monkeypatch):
    """If the DB row is gone but the Supabase delete fails, the request still
    succeeds (best-effort) — the account is already removed from the app."""
    user = _del_user(5)
    session = _DeleteSession(user)

    async def _failing_delete_auth_user(auth_user_id):
        raise ServiceError("auth down")

    monkeypatch.setattr(
        admin_routes, "delete_auth_user", _failing_delete_auth_user
    )

    async def _session():
        yield session

    app.dependency_overrides[get_session] = _session
    app.dependency_overrides[get_current_db_user] = lambda: _ctx(
        "super_admin", user_id=1
    )
    with TestClient(app) as client:
        resp = client.delete("/admin/users/5")
    app.dependency_overrides.clear()
    assert resp.status_code == 200
    assert resp.json()["deleted"] is True
    assert session.deleted == [user]  # DB delete committed despite auth failure
