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
from app.core import rate_limit
from app.core.database import get_session
from app.main import app
from app.models.alumni import Alumni
from app.models.audit import AuditLog
from app.models.crm import Interaction
from app.models.user import User
from app.schemas.auth import UserContext


def _ctx(*roles: str, user_id: int = 1) -> UserContext:
    return UserContext(
        user_id=user_id,
        auth_user_id=uuid.UUID("11111111-1111-1111-1111-111111111111"),
        roles=list(roles),
    )


async def _no_db_session():
    yield None


@pytest.fixture(autouse=True)
def _clear_rate_limit():
    """Each test starts with a clean in-process rate-limit window so the
    per-endpoint mutation limiters don't leak hits across tests."""
    rate_limit.reset()
    yield
    rate_limit.reset()


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

    def __init__(self, interaction=None, user=None, alumni=None):
        self._interaction = interaction
        self._user = user
        self._alumni = alumni
        self.added = []
        self.deleted = []
        self.committed = False

    async def get(self, model, pk):
        if model is Interaction:
            return self._interaction
        if model is User:
            return self._user
        if model is Alumni:
            return self._alumni
        return None

    def add(self, obj):
        # Mimic the DB assigning a primary key on insert so the response model
        # (which requires interaction_id) can validate without a real refresh.
        if isinstance(obj, Interaction) and obj.interaction_id is None:
            obj.interaction_id = 100
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


def test_add_interaction_allowed_for_view_only(client):
    # #129: a view_only ("Professor") may ADD interactions. The guard lets the
    # request through; an empty type then fails validation (422, not 403),
    # proving the role gate passed without needing a database.
    app.dependency_overrides[get_current_db_user] = lambda: _ctx("view_only")
    response = client.post("/alumni/1/interactions", json={"interaction_type": ""})
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"


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


def test_update_interaction_rejects_empty_type(client):
    app.dependency_overrides[get_current_db_user] = lambda: _ctx("full_access")
    response = client.patch("/alumni/1/interactions/9", json={"interaction_type": ""})
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"


def test_update_interaction_rejects_null_type(client):
    # Explicit null must NOT silently clear the type (it bypassed min_length).
    app.dependency_overrides[get_current_db_user] = lambda: _ctx("full_access")
    response = client.patch(
        "/alumni/1/interactions/9", json={"interaction_type": None}
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"


def test_update_interaction_rejects_whitespace_type(client):
    app.dependency_overrides[get_current_db_user] = lambda: _ctx("full_access")
    response = client.patch(
        "/alumni/1/interactions/9", json={"interaction_type": "   "}
    )
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
    # One audit row per CHANGED field, each capturing old -> new (FERPA trail).
    audits = [a for a in session.added if isinstance(a, AuditLog)]
    assert [a.action_type for a in audits] == [
        "update_interaction",
        "update_interaction",
    ]
    by_field = {a.field_name: (a.old_value, a.new_value) for a in audits}
    assert by_field["interaction_type"] == ("Call", "Email")
    assert by_field["interaction_notes"] == ("Caught up.", "Followed up.")


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
    # The deleted content is snapshotted into the audit row (hard delete would
    # otherwise lose the note text irrecoverably).
    audit = next(a for a in session.added if isinstance(a, AuditLog))
    assert audit.field_name == "interaction"
    assert "Caught up." in (audit.old_value or "")


def test_delete_interaction_404_when_missing(client):
    session = _FakeSession(interaction=None)
    app.dependency_overrides[get_current_db_user] = lambda: _ctx("full_access")
    app.dependency_overrides[get_session] = _with_session(session)

    response = client.delete("/alumni/1/interactions/9")
    assert response.status_code == 404
    assert session.deleted == []
    assert session.audit_actions == []


# --- view_only ("Professor") interaction permissions (#129) -------------------
#
# Permission matrix:
#   * view_only may ADD interactions and EDIT/DELETE only their OWN rows.
#   * Editing/deleting another user's interaction is 403.
#   * Edit-tier roles (engineer/super_admin/full_access/student) stay
#     unrestricted (may edit/delete ANY interaction) — covered by the happy-path
#     tests above plus the student case below.


def test_add_interaction_happy_path_for_view_only(client):
    # A professor adds an interaction; the row is stamped with THEIR user id so
    # ownership can later gate edit/delete.
    session = _FakeSession(
        alumni=SimpleNamespace(alumni_id=1, archived=False), user=_actor()
    )
    app.dependency_overrides[get_current_db_user] = lambda: _ctx(
        "view_only", user_id=2
    )
    app.dependency_overrides[get_session] = _with_session(session)

    response = client.post(
        "/alumni/1/interactions",
        json={
            "interaction_type": "Call",
            "interaction_date_time": "2026-06-01T12:00:00Z",
            "interaction_notes": "Reached out.",
        },
    )
    assert response.status_code == 201
    assert session.committed
    assert session.audit_actions == ["add_interaction"]
    created = next(a for a in session.added if isinstance(a, Interaction))
    assert created.user_id == 2  # stamped with the professor's id


def test_update_own_interaction_allowed_for_view_only(client):
    # Professor (user_id=2) owns the interaction (user_id=2) -> may edit it.
    session = _FakeSession(interaction=_interaction(alumni_id=1), user=_actor())
    app.dependency_overrides[get_current_db_user] = lambda: _ctx(
        "view_only", user_id=2
    )
    app.dependency_overrides[get_session] = _with_session(session)

    response = client.patch(
        "/alumni/1/interactions/9", json={"interaction_type": "Email"}
    )
    assert response.status_code == 200
    assert session.committed
    assert "update_interaction" in session.audit_actions


def test_update_others_interaction_forbidden_for_view_only(client):
    # Professor (user_id=7) does NOT own the interaction (user_id=2) -> 403, and
    # nothing is committed.
    session = _FakeSession(interaction=_interaction(alumni_id=1), user=_actor())
    app.dependency_overrides[get_current_db_user] = lambda: _ctx(
        "view_only", user_id=7
    )
    app.dependency_overrides[get_session] = _with_session(session)

    response = client.patch(
        "/alumni/1/interactions/9", json={"interaction_type": "Email"}
    )
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "forbidden"
    assert session.audit_actions == []
    assert not session.committed


def test_delete_own_interaction_allowed_for_view_only(client):
    row = _interaction(alumni_id=1)  # user_id=2
    session = _FakeSession(interaction=row, user=_actor())
    app.dependency_overrides[get_current_db_user] = lambda: _ctx(
        "view_only", user_id=2
    )
    app.dependency_overrides[get_session] = _with_session(session)

    response = client.delete("/alumni/1/interactions/9")
    assert response.status_code == 204
    assert session.deleted == [row]
    assert session.audit_actions == ["delete_interaction"]


def test_delete_others_interaction_forbidden_for_view_only(client):
    row = _interaction(alumni_id=1)  # user_id=2
    session = _FakeSession(interaction=row, user=_actor())
    app.dependency_overrides[get_current_db_user] = lambda: _ctx(
        "view_only", user_id=7
    )
    app.dependency_overrides[get_session] = _with_session(session)

    response = client.delete("/alumni/1/interactions/9")
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "forbidden"
    assert session.deleted == []
    assert session.audit_actions == []
    assert not session.committed


def test_update_others_interaction_allowed_for_edit_tier(client):
    # An edit-tier role (student, user_id=7) may edit an interaction logged by a
    # DIFFERENT user (user_id=2) -> unrestricted, no ownership gate.
    session = _FakeSession(interaction=_interaction(alumni_id=1), user=_actor())
    app.dependency_overrides[get_current_db_user] = lambda: _ctx(
        "student", user_id=7
    )
    app.dependency_overrides[get_session] = _with_session(session)

    response = client.patch(
        "/alumni/1/interactions/9", json={"interaction_type": "Email"}
    )
    assert response.status_code == 200
    assert session.committed


def test_delete_others_interaction_allowed_for_edit_tier(client):
    # An edit-tier role (full_access, user_id=7) may delete another user's row.
    row = _interaction(alumni_id=1)  # user_id=2
    session = _FakeSession(interaction=row, user=_actor())
    app.dependency_overrides[get_current_db_user] = lambda: _ctx(
        "full_access", user_id=7
    )
    app.dependency_overrides[get_session] = _with_session(session)

    response = client.delete("/alumni/1/interactions/9")
    assert response.status_code == 204
    assert session.deleted == [row]


def test_update_interaction_404_takes_precedence_over_ownership(client):
    # A view_only user editing a row that belongs to a DIFFERENT alumnus gets a
    # 404 (existence/parent check first), never a 403 — so the error can't be
    # used to probe for interactions on other alumni.
    session = _FakeSession(interaction=_interaction(alumni_id=99), user=_actor())
    app.dependency_overrides[get_current_db_user] = lambda: _ctx(
        "view_only", user_id=2
    )
    app.dependency_overrides[get_session] = _with_session(session)

    response = client.patch(
        "/alumni/1/interactions/9", json={"interaction_type": "Email"}
    )
    assert response.status_code == 404
    assert session.audit_actions == []


# --- #112a: per-endpoint rate limits on mutation routes ----------------------
#
# The interaction/task/employment write routes are throttled per actor (30 /
# minute). We exhaust the budget with cheap requests that PASS the limiter
# dependency but then fail body validation (422) — the limiter runs first (it's a
# dependency), so each request still counts. Once the budget is gone the route
# returns 429 before validation, proving the brake fires.

_MUTATION_LIMIT = rate_limit._MUTATION_LIMIT


def test_interaction_write_rate_limited_returns_429(client):
    # view_only may add interactions (#129); a blank type 422s but the limiter
    # already counted the hit, so the (limit+1)-th request is 429.
    app.dependency_overrides[get_current_db_user] = lambda: _ctx(
        "view_only", user_id=1
    )
    seen = [
        client.post(
            "/alumni/1/interactions", json={"interaction_type": ""}
        ).status_code
        for _ in range(_MUTATION_LIMIT + 1)
    ]
    assert seen[:_MUTATION_LIMIT] == [422] * _MUTATION_LIMIT
    assert seen[-1] == 429
    # The 429 body carries the rate_limited error code (FastAPI nests a raw
    # HTTPException detail under "detail").
    blocked = client.post("/alumni/1/interactions", json={"interaction_type": ""})
    assert blocked.status_code == 429
    assert blocked.json()["detail"]["error"]["code"] == "rate_limited"
    assert blocked.headers.get("Retry-After") == "60"


def test_task_write_rate_limited_returns_429(client):
    app.dependency_overrides[get_current_db_user] = lambda: _ctx(
        "full_access", user_id=1
    )
    seen = [
        client.post("/alumni/1/tasks", json={"task_title": ""}).status_code
        for _ in range(_MUTATION_LIMIT + 1)
    ]
    assert seen[:_MUTATION_LIMIT] == [422] * _MUTATION_LIMIT
    assert seen[-1] == 429


def test_employment_write_rate_limited_returns_429(client):
    app.dependency_overrides[get_current_db_user] = lambda: _ctx(
        "full_access", user_id=1
    )
    # Blank employer_name fails validation (422) but the limiter counts each hit.
    seen = [
        client.post("/alumni/1/employment", json={"employer_name": ""}).status_code
        for _ in range(_MUTATION_LIMIT + 1)
    ]
    assert seen[:_MUTATION_LIMIT] == [422] * _MUTATION_LIMIT
    assert seen[-1] == 429


def test_mutation_rate_limit_is_per_actor(client):
    # Actor 1 exhausts the interaction budget; actor 2 still has a full one.
    app.dependency_overrides[get_current_db_user] = lambda: _ctx(
        "full_access", user_id=1
    )
    statuses = [
        client.post(
            "/alumni/1/interactions", json={"interaction_type": ""}
        ).status_code
        for _ in range(_MUTATION_LIMIT + 1)
    ]
    assert statuses[-1] == 429

    app.dependency_overrides[get_current_db_user] = lambda: _ctx(
        "full_access", user_id=2
    )
    assert (
        client.post(
            "/alumni/1/interactions", json={"interaction_type": ""}
        ).status_code
        != 429
    )


def test_mutation_limiters_have_independent_buckets(client):
    # Exhausting the interaction bucket must NOT throttle the task route — each
    # resource has its own per-actor window.
    app.dependency_overrides[get_current_db_user] = lambda: _ctx(
        "full_access", user_id=1
    )
    for _ in range(_MUTATION_LIMIT + 1):
        client.post("/alumni/1/interactions", json={"interaction_type": ""})
    # Task bucket is still fresh -> validation 422, not 429.
    assert (
        client.post("/alumni/1/tasks", json={"task_title": ""}).status_code == 422
    )


# --- QA hardening: interaction required fields + no future date (H1/H2) -------


def test_add_interaction_rejects_empty_payload(client):
    # H1: an empty body must be a 422, never a silently-defaulted record.
    app.dependency_overrides[get_current_db_user] = lambda: _ctx("full_access")
    response = client.post("/alumni/1/interactions", json={})
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"


def test_add_interaction_requires_date(client):
    # H1: type alone is no longer enough — the interaction date is required.
    app.dependency_overrides[get_current_db_user] = lambda: _ctx("full_access")
    response = client.post(
        "/alumni/1/interactions", json={"interaction_type": "Call"}
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"


def test_add_interaction_rejects_future_date(client):
    # H2: an interaction records the past; a future timestamp is a 422.
    app.dependency_overrides[get_current_db_user] = lambda: _ctx("full_access")
    future = (
        datetime.datetime.now(datetime.UTC) + datetime.timedelta(days=3)
    ).isoformat()
    response = client.post(
        "/alumni/1/interactions",
        json={"interaction_type": "Call", "interaction_date_time": future},
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"


def test_update_interaction_rejects_future_date(client):
    # H2: editing an interaction's date to the future is also rejected. Guard
    # passes (full_access); validation fires before any DB work.
    app.dependency_overrides[get_current_db_user] = lambda: _ctx("full_access")
    future = (
        datetime.datetime.now(datetime.UTC) + datetime.timedelta(days=3)
    ).isoformat()
    response = client.patch(
        "/alumni/1/interactions/9",
        json={"interaction_date_time": future},
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"


def test_add_interaction_accepts_valid_past_date(client):
    # Sanity: a well-formed past interaction still creates (201).
    session = _FakeSession(
        alumni=SimpleNamespace(alumni_id=1, archived=False), user=_actor()
    )
    app.dependency_overrides[get_current_db_user] = lambda: _ctx(
        "full_access", user_id=2
    )
    app.dependency_overrides[get_session] = _with_session(session)
    response = client.post(
        "/alumni/1/interactions",
        json={
            "interaction_type": "Call",
            "interaction_date_time": "2026-06-01T12:00:00Z",
        },
    )
    assert response.status_code == 201
    assert session.committed
