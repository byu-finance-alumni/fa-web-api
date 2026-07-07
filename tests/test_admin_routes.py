"""Authorization-gate tests for the user-administration routes.

User/role administration is super_admin-only — full_access and view_only must be
rejected with 403, and a missing token with 401, before any query runs.

The deactivate/reactivate tests drive the real route logic against a fake
in-memory session (no database), asserting the active flag flips, an audit row
is written, self-deactivation is rejected, and a no-op is idempotent.
"""

import uuid
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app.api.dependencies.auth import get_current_db_user
from app.core.database import get_session
from app.main import app
from app.schemas.auth import UserContext


def _ctx(*roles: str, user_id: int = 1) -> UserContext:
    return UserContext(
        user_id=user_id,
        auth_user_id=uuid.UUID("11111111-1111-1111-1111-111111111111"),
        roles=list(roles),
    )


async def _no_db_session():
    yield None


@pytest.fixture
def client():
    app.dependency_overrides[get_session] = _no_db_session
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


def _fake_user(user_id: int, *, active: bool, roles=("full_access",), locked_at=None):
    return SimpleNamespace(
        user_id=user_id,
        email=f"user{user_id}@byu.edu",
        first_name="Test",
        last_name="User",
        active=active,
        locked_at=locked_at,
        created_at=None,
        roles=[SimpleNamespace(role_name=r) for r in roles],
    )


class _FakeSession:
    """Minimal AsyncSession stand-in: ``scalar`` returns the seeded user, and
    ``add`` / ``commit`` record what the route attempted."""

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


@pytest.fixture
def active_client():
    """Client wired so the target user (user_id=2) exists and is active, with the
    actor being a *different* super_admin (user_id=1)."""
    user = _fake_user(2, active=True)
    session = _FakeSession(user)

    async def _session():
        yield session

    app.dependency_overrides[get_session] = _session
    app.dependency_overrides[get_current_db_user] = lambda: _ctx(
        "super_admin", user_id=1
    )
    with TestClient(app) as test_client:
        yield test_client, session
    app.dependency_overrides.clear()


def test_list_users_requires_auth(client):
    assert client.get("/admin/users").status_code == 401


@pytest.mark.parametrize("role", ["view_only", "full_access"])
def test_assign_role_forbidden_below_super_admin(client, role):
    app.dependency_overrides[get_current_db_user] = lambda: _ctx(role)
    response = client.post("/admin/users/2/roles", json={"role_name": "full_access"})
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "forbidden"


@pytest.mark.parametrize("role", ["view_only", "full_access"])
def test_remove_role_forbidden_below_super_admin(client, role):
    app.dependency_overrides[get_current_db_user] = lambda: _ctx(role)
    response = client.delete("/admin/users/2/roles/full_access")
    assert response.status_code == 403


def test_assign_role_rejects_unknown_role(client):
    # super_admin passes the guard; an invalid role value fails validation (422).
    app.dependency_overrides[get_current_db_user] = lambda: _ctx("super_admin")
    response = client.post("/admin/users/2/roles", json={"role_name": "wizard"})
    assert response.status_code == 422


def test_super_admin_cannot_grant_engineer(client):
    # Privilege ceiling: a super_admin (below engineer) may not mint an engineer.
    # The check runs before any DB access, so the no-db client is fine -> 403.
    app.dependency_overrides[get_current_db_user] = lambda: _ctx(
        "super_admin", user_id=1
    )
    response = client.post("/admin/users/2/roles", json={"role_name": "engineer"})
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "forbidden"


class _QueueSession:
    """Session stand-in returning a queued sequence of ``scalar`` results."""

    def __init__(self, results):
        self._results = list(results)
        self.added: list = []
        self.commits = 0

    async def scalar(self, _stmt):
        return self._results.pop(0)

    def add(self, obj):
        self.added.append(obj)

    async def delete(self, _obj):
        pass

    async def commit(self):
        self.commits += 1


def test_super_admin_cannot_remove_engineer(client):
    # Symmetric with assign: a super_admin may not demote an engineer. The
    # ceiling check runs before any DB access, so the no-db client is fine.
    app.dependency_overrides[get_current_db_user] = lambda: _ctx(
        "super_admin", user_id=1
    )
    resp = client.delete("/admin/users/2/roles/engineer")
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "forbidden"


def test_engineer_can_grant_engineer():
    # An engineer actor passes the privilege ceiling and the grant succeeds.
    target = _fake_user(2, active=True, roles=("full_access",))
    engineer_role = SimpleNamespace(role_id=9, role_name="engineer")
    # assign_role scalars: _load_user -> Role lookup -> _load_user (re-serialize).
    session = _QueueSession([target, engineer_role, target])

    async def _session():
        yield session

    app.dependency_overrides[get_session] = _session
    app.dependency_overrides[get_current_db_user] = lambda: _ctx(
        "engineer", user_id=1
    )
    with TestClient(app) as test_client:
        response = test_client.post(
            "/admin/users/2/roles", json={"role_name": "engineer"}
        )
    app.dependency_overrides.clear()
    assert response.status_code == 200
    assert session.commits == 1
    assert any(type(a).__name__ == "UserRole" for a in session.added)


def test_remove_own_engineer_role_blocked():
    # An engineer removing their OWN engineer role would risk locking vocab/db
    # administration out of the system -> 409 ConflictError, no commit.
    actor_user = _fake_user(1, active=True, roles=("engineer",))
    engineer_role = SimpleNamespace(role_id=9, role_name="engineer")
    link = SimpleNamespace(user_id=1, role_id=9)
    # remove_role scalars in order: _load_user -> Role lookup -> UserRole link.
    session = _QueueSession([actor_user, engineer_role, link])

    async def _session():
        yield session

    app.dependency_overrides[get_session] = _session
    app.dependency_overrides[get_current_db_user] = lambda: _ctx(
        "engineer", user_id=1
    )
    with TestClient(app) as test_client:
        response = test_client.delete("/admin/users/1/roles/engineer")
    app.dependency_overrides.clear()
    assert response.status_code == 409
    assert session.commits == 0


# --- deactivate / reactivate --------------------------------------------------


def test_set_active_requires_auth(client):
    assert client.patch("/admin/users/2", json={"active": False}).status_code == 401


@pytest.mark.parametrize("role", ["view_only", "full_access"])
def test_set_active_forbidden_below_super_admin(client, role):
    app.dependency_overrides[get_current_db_user] = lambda: _ctx(role)
    response = client.patch("/admin/users/2", json={"active": False})
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "forbidden"


def test_set_active_rejects_unknown_field(client):
    app.dependency_overrides[get_current_db_user] = lambda: _ctx("super_admin")
    response = client.patch("/admin/users/2", json={"active": False, "x": 1})
    assert response.status_code == 422


def test_deactivate_user_flips_flag_and_audits(active_client):
    test_client, session = active_client
    response = test_client.patch("/admin/users/2", json={"active": False})
    assert response.status_code == 200
    assert response.json()["active"] is False
    assert session.commits == 1
    audit = next(a for a in session.added if type(a).__name__ == "AuditLog")
    assert audit.action_type == "deactivate_user"
    assert audit.entity_type == "user"
    assert audit.entity_id == 2
    assert audit.field_name == "active"
    assert audit.old_value == "True" and audit.new_value == "False"
    assert audit.user_id == 1  # the actor


def test_reactivate_user_flips_flag_and_audits():
    user = _fake_user(2, active=False)
    session = _FakeSession(user)

    async def _session():
        yield session

    app.dependency_overrides[get_session] = _session
    app.dependency_overrides[get_current_db_user] = lambda: _ctx(
        "super_admin", user_id=1
    )
    with TestClient(app) as test_client:
        response = test_client.patch("/admin/users/2", json={"active": True})
    app.dependency_overrides.clear()

    assert response.status_code == 200
    assert response.json()["active"] is True
    audit = next(a for a in session.added if type(a).__name__ == "AuditLog")
    assert audit.action_type == "activate_user"
    assert audit.new_value == "True"


def test_deactivate_self_is_rejected():
    # Actor and target are the same super_admin (user_id=2): 409, no write.
    user = _fake_user(2, active=True, roles=("super_admin",))
    session = _FakeSession(user)

    async def _session():
        yield session

    app.dependency_overrides[get_session] = _session
    app.dependency_overrides[get_current_db_user] = lambda: _ctx(
        "super_admin", user_id=2
    )
    with TestClient(app) as test_client:
        response = test_client.patch("/admin/users/2", json={"active": False})
    app.dependency_overrides.clear()

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "conflict"
    assert session.commits == 0


def test_deactivate_already_inactive_is_idempotent():
    user = _fake_user(2, active=False)
    session = _FakeSession(user)

    async def _session():
        yield session

    app.dependency_overrides[get_session] = _session
    app.dependency_overrides[get_current_db_user] = lambda: _ctx(
        "super_admin", user_id=1
    )
    with TestClient(app) as test_client:
        response = test_client.patch("/admin/users/2", json={"active": False})
    app.dependency_overrides.clear()

    assert response.status_code == 200
    assert response.json()["active"] is False
    assert session.commits == 0  # no change -> no commit, no audit
    assert not any(type(a).__name__ == "AuditLog" for a in session.added)


# --- engineer-action oversight log (GET /admin/engineer-actions, #199/#200) ---
#
# The tamper-resistant engineer-action log is oversight *of* the engineer, so its
# read gate is ROLE-based super_admin and explicitly DENIES engineer -- unlike the
# capability-based RequireSuperAdmin, which an engineer also satisfies. There is no
# delete/purge route for the table.


class _EngineerActionsSession:
    """Session stand-in for GET /admin/engineer-actions: ``scalar`` returns the
    total count, ``scalars`` returns a page of rows, and add/commit are recorded."""

    def __init__(self, total, rows):
        self._total = total
        self._rows = rows
        self.added: list = []
        self.commits = 0

    async def scalar(self, _stmt):
        return self._total

    async def scalars(self, _stmt):
        return SimpleNamespace(all=lambda: self._rows)

    def add(self, obj):
        self.added.append(obj)

    async def commit(self):
        self.commits += 1


def test_engineer_actions_requires_auth(client):
    assert client.get("/admin/engineer-actions").status_code == 401


@pytest.mark.parametrize("role", ["view_only", "full_access", "student", "engineer"])
def test_engineer_actions_forbidden_for_non_super_admin(client, role):
    # The strict role gate rejects every role that is not super_admin -- crucially
    # including engineer (the audited party), even though it holds every capability.
    app.dependency_overrides[get_current_db_user] = lambda: _ctx(role)
    response = client.get("/admin/engineer-actions")
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "forbidden"


def test_engineer_actions_forbidden_when_engineer_and_super_admin():
    # Holding super_admin does NOT rescue an engineer: any actor with the engineer
    # role is denied so the audited party can never read its own oversight log.
    app.dependency_overrides[get_current_db_user] = lambda: _ctx(
        "super_admin", "engineer", user_id=1
    )
    with TestClient(app) as test_client:
        response = test_client.get("/admin/engineer-actions")
    app.dependency_overrides.clear()
    assert response.status_code == 403


def test_engineer_actions_super_admin_reads_and_audits():
    row = SimpleNamespace(
        engineer_action_log_id=5,
        actor_user_id=9,
        actor_email="eng@byu.edu",
        action_type="delete_user",
        entity_type="user",
        entity_id=3,
        field_name="email",
        old_value="victim@byu.edu",
        new_value=None,
        occurred_at=__import__("datetime").datetime(2026, 7, 6, 12, 0, 0),
    )
    session = _EngineerActionsSession(total=1, rows=[row])

    async def _session():
        yield session

    app.dependency_overrides[get_session] = _session
    app.dependency_overrides[get_current_db_user] = lambda: _ctx(
        "super_admin", user_id=1
    )
    with TestClient(app) as test_client:
        response = test_client.get("/admin/engineer-actions")
    app.dependency_overrides.clear()

    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 1
    assert body["items"][0]["engineer_action_log_id"] == 5
    assert body["items"][0]["actor_email"] == "eng@byu.edu"
    assert body["items"][0]["action_type"] == "delete_user"
    # The read itself is audited (actor + page window); rows are not logged.
    audit = next(a for a in session.added if type(a).__name__ == "AuditLog")
    assert audit.action_type == "read_engineer_action_log"
    assert audit.user_id == 1


def test_engineer_actions_has_no_delete_route(client):
    # The table is append-only by design: there must be NO delete/purge route.
    # A DELETE to the collection path is not registered -> 405 Method Not Allowed.
    app.dependency_overrides[get_current_db_user] = lambda: _ctx(
        "super_admin", user_id=1
    )
    response = client.delete("/admin/engineer-actions")
    assert response.status_code == 405
