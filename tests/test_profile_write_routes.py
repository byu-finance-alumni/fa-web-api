"""Authorization-gating tests for the profile write routes (no database).

Per the CRUD security invariant, every write path must reject view_only before
any query runs. These overrides assert the guard fires (403) and that a missing
token is 401 — neither reaches a real database.
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


def test_add_interaction_requires_auth(client):
    response = client.post("/alumni/1/interactions", json={"interaction_type": "Call"})
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "unauthorized"


def test_add_interaction_forbidden_for_view_only(client):
    app.dependency_overrides[get_current_db_user] = lambda: _ctx("view_only")
    response = client.post("/alumni/1/interactions", json={"interaction_type": "Call"})
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "forbidden"


def test_add_task_forbidden_for_view_only(client):
    app.dependency_overrides[get_current_db_user] = lambda: _ctx("view_only")
    response = client.post("/alumni/1/tasks", json={"task_title": "Follow up"})
    assert response.status_code == 403


def test_update_task_forbidden_for_view_only(client):
    app.dependency_overrides[get_current_db_user] = lambda: _ctx("view_only")
    response = client.patch("/alumni/1/tasks/5", json={"completed": True})
    assert response.status_code == 403


def test_add_interaction_rejects_empty_type(client):
    # full_access passes the guard; blank type fails validation (422, not 403).
    app.dependency_overrides[get_current_db_user] = lambda: _ctx("full_access")
    response = client.post("/alumni/1/interactions", json={"interaction_type": ""})
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"


def test_add_task_rejects_unknown_field(client):
    app.dependency_overrides[get_current_db_user] = lambda: _ctx("full_access")
    response = client.post(
        "/alumni/1/tasks", json={"task_title": "x", "not_a_field": 1}
    )
    assert response.status_code == 422


def test_update_employment_forbidden_for_view_only(client):
    app.dependency_overrides[get_current_db_user] = lambda: _ctx("view_only")
    response = client.patch(
        "/alumni/1/employment/5", json={"employer_name": "Acme"}
    )
    assert response.status_code == 403


def test_delete_employment_forbidden_for_view_only(client):
    app.dependency_overrides[get_current_db_user] = lambda: _ctx("view_only")
    response = client.delete("/alumni/1/employment/5")
    assert response.status_code == 403


def test_add_education_forbidden_for_view_only(client):
    app.dependency_overrides[get_current_db_user] = lambda: _ctx("view_only")
    response = client.post("/alumni/1/education", json={"university": "BYU"})
    assert response.status_code == 403


def test_update_education_forbidden_for_view_only(client):
    app.dependency_overrides[get_current_db_user] = lambda: _ctx("view_only")
    response = client.patch("/alumni/1/education/5", json={"university": "BYU"})
    assert response.status_code == 403


def test_delete_education_forbidden_for_view_only(client):
    app.dependency_overrides[get_current_db_user] = lambda: _ctx("view_only")
    response = client.delete("/alumni/1/education/5")
    assert response.status_code == 403


def test_add_leadership_forbidden_for_view_only(client):
    app.dependency_overrides[get_current_db_user] = lambda: _ctx("view_only")
    response = client.post(
        "/alumni/1/leadership", json={"leadership_role": "President"}
    )
    assert response.status_code == 403


def test_update_leadership_forbidden_for_view_only(client):
    app.dependency_overrides[get_current_db_user] = lambda: _ctx("view_only")
    response = client.patch(
        "/alumni/1/leadership/5", json={"leadership_role": "President"}
    )
    assert response.status_code == 403


def test_delete_leadership_forbidden_for_view_only(client):
    app.dependency_overrides[get_current_db_user] = lambda: _ctx("view_only")
    response = client.delete("/alumni/1/leadership/5")
    assert response.status_code == 403


def test_add_leadership_rejects_empty_role(client):
    app.dependency_overrides[get_current_db_user] = lambda: _ctx("full_access")
    response = client.post("/alumni/1/leadership", json={"leadership_role": ""})
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"


def test_add_education_rejects_unknown_field(client):
    app.dependency_overrides[get_current_db_user] = lambda: _ctx("full_access")
    response = client.post(
        "/alumni/1/education", json={"university": "BYU", "not_a_field": 1}
    )
    assert response.status_code == 422


def test_super_admin_passes_guard(client):
    # super_admin satisfies full_access; blank title then fails validation (422),
    # proving the guard let it through rather than 403-ing.
    app.dependency_overrides[get_current_db_user] = lambda: _ctx("super_admin")
    response = client.post("/alumni/1/tasks", json={"task_title": ""})
    assert response.status_code == 422
