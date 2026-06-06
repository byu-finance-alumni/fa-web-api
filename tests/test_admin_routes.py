"""Authorization-gate tests for the user-administration routes.

User/role administration is super_admin-only — full_access and view_only must be
rejected with 403, and a missing token with 401, before any query runs.
"""

import uuid

import pytest
from fastapi.testclient import TestClient

from app.api.dependencies.auth import get_current_db_user
from app.core.database import get_session
from app.main import app
from app.schemas.auth import UserContext


def _ctx(*roles: str) -> UserContext:
    return UserContext(
        user_id=1,
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
