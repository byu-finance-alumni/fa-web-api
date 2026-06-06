"""Authorization-gating tests for the alumni routes (no database).

The DB-user dependency is overridden so we can assert role gating and request
validation without a live database — these paths reject before any query runs.
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
    """Stand-in for get_session so these auth/validation tests don't require a
    real DATABASE_URL (CI has none). No test here reaches a real query."""
    yield None


@pytest.fixture
def client():
    app.dependency_overrides[get_session] = _no_db_session
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


def test_create_requires_auth(client):
    response = client.post("/alumni", json={"last_name": "Smith"})
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "unauthorized"


def test_create_forbidden_for_view_only(client):
    app.dependency_overrides[get_current_db_user] = lambda: _ctx("view_only")
    response = client.post("/alumni", json={"last_name": "Smith"})
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "forbidden"


def test_patch_forbidden_for_view_only(client):
    app.dependency_overrides[get_current_db_user] = lambda: _ctx("view_only")
    response = client.patch("/alumni/1", json={"last_name": "Smith"})
    assert response.status_code == 403


def test_delete_forbidden_for_view_only(client):
    app.dependency_overrides[get_current_db_user] = lambda: _ctx("view_only")
    response = client.delete("/alumni/1")
    assert response.status_code == 403


def test_restore_forbidden_for_view_only(client):
    app.dependency_overrides[get_current_db_user] = lambda: _ctx("view_only")
    response = client.post("/alumni/1/restore")
    assert response.status_code == 403


def test_create_rejects_empty_identifier(client):
    # full_access passes the guard; the body fails the "at least one identifier"
    # rule, so this is a 422 (validation), not a 403.
    app.dependency_overrides[get_current_db_user] = lambda: _ctx("full_access")
    response = client.post("/alumni", json={"gender": "F"})
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"


def test_create_rejects_unknown_field(client):
    app.dependency_overrides[get_current_db_user] = lambda: _ctx("full_access")
    response = client.post(
        "/alumni", json={"last_name": "Smith", "not_a_field": "x"}
    )
    assert response.status_code == 422
