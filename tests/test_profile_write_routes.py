"""Authorization-gating tests for the profile write routes (no database).

Per the CRUD security invariant, every write path must reject view_only before
any query runs. These overrides assert the guard fires (403) and that a missing
token is 401 — neither reaches a real database.
"""

import datetime
import uuid
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app.api.dependencies.auth import get_current_db_user
from app.core.database import get_session
from app.main import app
from app.models.audit import AuditLog
from app.models.crm import Interaction
from app.models.user import User
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


class _FakeSession:
    """Stub session for interaction edit/delete happy paths (CI has no DB).

    ``get`` dispatches by model: an Interaction returns the seeded row (or None
    to force a 404), a User returns the seeded actor (resolves ``logged_by``).
    Mutations are recorded so tests can assert the audit row was written and the
    row was deleted."""

    def __init__(self, interaction=None, user=None):
        self._interaction = interaction
        self._user = user
        self.added = []
        self.deleted = []
        self.committed = False

    async def get(self, model, pk):
        if model is Interaction:
            return self._interaction
        if model is User:
            return self._user
        return None

    def add(self, obj):
        self.added.append(obj)

    async def delete(self, obj):
        self.deleted.append(obj)

    async def commit(self):
        self.committed = True

    async def refresh(self, obj):
        return None

    @property
    def audit_actions(self):
        return [a.action_type for a in self.added if isinstance(a, AuditLog)]


def _with_session(session):
    async def _override():
        yield session

    return _override


def _interaction(alumni_id=1):
    return SimpleNamespace(
        interaction_id=9,
        alumni_id=alumni_id,
        user_id=2,
        interaction_type="Call",
        interaction_date_time=datetime.datetime(
            2026, 6, 1, 12, 0, tzinfo=datetime.UTC
        ),
        interaction_notes="Caught up.",
    )


def _actor():
    return SimpleNamespace(
        user_id=2, first_name="Tanya", last_name="Harmon", email="th@byu.edu"
    )


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


# --- interaction edit/delete (#38) --------------------------------------------


def test_update_interaction_requires_auth(client):
    response = client.patch(
        "/alumni/1/interactions/9", json={"interaction_type": "Email"}
    )
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "unauthorized"


def test_update_interaction_forbidden_for_view_only(client):
    app.dependency_overrides[get_current_db_user] = lambda: _ctx("view_only")
    response = client.patch(
        "/alumni/1/interactions/9", json={"interaction_type": "Email"}
    )
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "forbidden"


def test_update_interaction_rejects_empty_type(client):
    app.dependency_overrides[get_current_db_user] = lambda: _ctx("full_access")
    response = client.patch("/alumni/1/interactions/9", json={"interaction_type": ""})
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"


def test_update_interaction_happy_path(client):
    session = _FakeSession(interaction=_interaction(alumni_id=1), user=_actor())
    app.dependency_overrides[get_current_db_user] = lambda: _ctx("full_access")
    app.dependency_overrides[get_session] = _with_session(session)

    response = client.patch(
        "/alumni/1/interactions/9",
        json={"interaction_type": "Email", "interaction_notes": "Followed up."},
    )
    assert response.status_code == 200
    body = response.json()
    assert body == {
        "interaction_id": 9,
        "interaction_type": "Email",
        "interaction_date_time": "2026-06-01T12:00:00Z",
        "interaction_notes": "Followed up.",
        "logged_by": "Tanya Harmon",
    }
    assert session.committed
    assert session.audit_actions == ["update_interaction"]


def test_update_interaction_404_for_other_alumni(client):
    # Row exists but belongs to a different alumnus -> 404, no audit/commit.
    session = _FakeSession(interaction=_interaction(alumni_id=99), user=_actor())
    app.dependency_overrides[get_current_db_user] = lambda: _ctx("full_access")
    app.dependency_overrides[get_session] = _with_session(session)

    response = client.patch(
        "/alumni/1/interactions/9", json={"interaction_type": "Email"}
    )
    assert response.status_code == 404
    assert session.audit_actions == []


def test_delete_interaction_requires_auth(client):
    response = client.delete("/alumni/1/interactions/9")
    assert response.status_code == 401


def test_delete_interaction_forbidden_for_view_only(client):
    app.dependency_overrides[get_current_db_user] = lambda: _ctx("view_only")
    response = client.delete("/alumni/1/interactions/9")
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "forbidden"


def test_delete_interaction_happy_path(client):
    row = _interaction(alumni_id=1)
    session = _FakeSession(interaction=row, user=_actor())
    app.dependency_overrides[get_current_db_user] = lambda: _ctx("full_access")
    app.dependency_overrides[get_session] = _with_session(session)

    response = client.delete("/alumni/1/interactions/9")
    assert response.status_code == 204
    assert session.deleted == [row]
    assert session.committed
    assert session.audit_actions == ["delete_interaction"]


def test_delete_interaction_404_when_missing(client):
    session = _FakeSession(interaction=None)
    app.dependency_overrides[get_current_db_user] = lambda: _ctx("full_access")
    app.dependency_overrides[get_session] = _with_session(session)

    response = client.delete("/alumni/1/interactions/9")
    assert response.status_code == 404
    assert session.deleted == []
    assert session.audit_actions == []
