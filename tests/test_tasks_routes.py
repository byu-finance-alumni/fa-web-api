"""Tests for the cross-alumni admin Tasks list (GET /tasks).

The endpoint is gated to full_access / super_admin: a missing token is 401, a
view_only user is 403, and full_access / super_admin get the paginated list.
Happy-path serialization runs against a stubbed in-memory session (CI has no
DATABASE_URL). Mirrors tests/test_dashboard_drawers.py.
"""

import datetime
import uuid
from types import SimpleNamespace

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


class _Result:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return self._rows


class _FakeSession:
    def __init__(self, rows, scalars=()):
        self._rows = rows
        self._scalars = list(scalars)
        self.execute_args = []
        self.scalar_args = []

    async def execute(self, stmt):
        self.execute_args.append(stmt)
        return _Result(self._rows)

    async def scalar(self, stmt):
        self.scalar_args.append(stmt)
        return self._scalars.pop(0) if self._scalars else 0


def _with_session(session):
    async def _override():
        yield session

    return _override


def _alumni():
    return SimpleNamespace(alumni_id=7, first_name="Jane", last_name="Doe")


def _user():
    return SimpleNamespace(
        user_id=2, first_name="Tanya", last_name="Harmon", email="th@byu.edu"
    )


def _task():
    return SimpleNamespace(
        follow_up_task_id=31,
        alumni_id=7,
        task_title="Call about mentoring",
        due_date=datetime.date(2026, 6, 10),
        completed=False,
        completed_at=None,
        task_notes="Discuss spring cohort.",
        assigned_to_user_id=2,
    )


# --- auth gating --------------------------------------------------------------


def test_tasks_requires_auth(client):
    response = client.get("/tasks")
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "unauthorized"


def test_tasks_forbidden_for_view_only(client):
    app.dependency_overrides[get_current_db_user] = lambda: _ctx("view_only")
    response = client.get("/tasks")
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "forbidden"


# --- happy path (stubbed session) ---------------------------------------------


@pytest.mark.parametrize("role", ["full_access", "super_admin"])
def test_tasks_returns_paginated_list(client, role):
    rows = [(_task(), _alumni(), _user())]
    app.dependency_overrides[get_current_db_user] = lambda: _ctx(role)
    app.dependency_overrides[get_session] = _with_session(
        _FakeSession(rows, scalars=[1])
    )

    response = client.get("/tasks?limit=25&offset=0")
    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 1
    assert body["limit"] == 25
    assert body["offset"] == 0
    assert body["items"] == [
        {
            "follow_up_task_id": 31,
            "alumni_id": 7,
            "alumni_name": "Jane Doe",
            "task_title": "Call about mentoring",
            "due_date": "2026-06-10",
            "completed": False,
            "completed_at": None,
            "task_notes": "Discuss spring cohort.",
            "assigned_to_user_id": 2,
            "assigned_to": "Tanya Harmon",
        }
    ]


def test_tasks_handles_unassigned_task(client):
    task = _task()
    task.assigned_to_user_id = None
    rows = [(task, _alumni(), None)]
    app.dependency_overrides[get_current_db_user] = lambda: _ctx("full_access")
    app.dependency_overrides[get_session] = _with_session(
        _FakeSession(rows, scalars=[1])
    )

    response = client.get("/tasks")
    assert response.status_code == 200
    item = response.json()["items"][0]
    assert item["assigned_to"] is None
    assert item["assigned_to_user_id"] is None


def _compiled(stmt) -> str:
    from sqlalchemy.dialects import postgresql

    return str(stmt.compile(dialect=postgresql.dialect()))


def test_tasks_default_filters_open_only(client):
    session = _FakeSession([], scalars=[0])
    app.dependency_overrides[get_current_db_user] = lambda: _ctx("full_access")
    app.dependency_overrides[get_session] = _with_session(session)

    response = client.get("/tasks")
    assert response.status_code == 200
    sql = _compiled(session.execute_args[0])
    assert "completed" in sql.lower()


def test_tasks_all_includes_every_state(client):
    session = _FakeSession([], scalars=[0])
    app.dependency_overrides[get_current_db_user] = lambda: _ctx("full_access")
    app.dependency_overrides[get_session] = _with_session(session)

    response = client.get("/tasks?all=true")
    assert response.status_code == 200
    # No completion predicate in the WHERE clause when all=true.
    sql = _compiled(session.execute_args[0])
    assert "follow_up_tasks.completed IS" not in sql


# --- new sort / filter params -------------------------------------------------


def _fresh_session(scalars=(1,)):
    """A fake session seeded so both the count and the row query succeed."""
    return _FakeSession([], scalars=list(scalars))


def test_tasks_default_sort_orders_by_due(client):
    session = _fresh_session()
    app.dependency_overrides[get_current_db_user] = lambda: _ctx("full_access")
    app.dependency_overrides[get_session] = _with_session(session)

    response = client.get("/tasks")
    assert response.status_code == 200
    # Default sort: open before completed, then soonest due (nulls last).
    sql = _compiled(session.execute_args[0]).lower()
    order = sql.split("order by", 1)[1]
    assert "completed asc" in order
    assert "due_date asc" in order


def test_tasks_sort_due_desc_reverses_due(client):
    session = _fresh_session()
    app.dependency_overrides[get_current_db_user] = lambda: _ctx("full_access")
    app.dependency_overrides[get_session] = _with_session(session)

    response = client.get("/tasks?sort=due_desc")
    assert response.status_code == 200
    order = _compiled(session.execute_args[0]).lower().split("order by", 1)[1]
    assert "due_date desc" in order


def test_tasks_sort_alumni_orders_by_name(client):
    session = _fresh_session()
    app.dependency_overrides[get_current_db_user] = lambda: _ctx("full_access")
    app.dependency_overrides[get_session] = _with_session(session)

    response = client.get("/tasks?sort=alumni")
    assert response.status_code == 200
    order = _compiled(session.execute_args[0]).lower().split("order by", 1)[1]
    assert "lower" in order
    assert "first_name" in order


def test_tasks_sort_created_orders_by_id(client):
    session = _fresh_session()
    app.dependency_overrides[get_current_db_user] = lambda: _ctx("full_access")
    app.dependency_overrides[get_session] = _with_session(session)

    response = client.get("/tasks?sort=created")
    assert response.status_code == 200
    order = _compiled(session.execute_args[0]).lower().split("order by", 1)[1]
    assert "follow_up_task_id desc" in order
    # created sorts purely by id — no due_date in the ORDER BY.
    assert "due_date" not in order


def test_tasks_unknown_sort_falls_back_to_due(client):
    session = _fresh_session()
    app.dependency_overrides[get_current_db_user] = lambda: _ctx("full_access")
    app.dependency_overrides[get_session] = _with_session(session)

    response = client.get("/tasks?sort=bogus")
    assert response.status_code == 200
    order = _compiled(session.execute_args[0]).lower().split("order by", 1)[1]
    assert "due_date asc" in order


def test_tasks_overdue_filters_due_and_open(client):
    session = _fresh_session()
    app.dependency_overrides[get_current_db_user] = lambda: _ctx("full_access")
    app.dependency_overrides[get_session] = _with_session(session)

    response = client.get("/tasks?overdue=true&all=true")
    assert response.status_code == 200
    sql = _compiled(session.execute_args[0]).lower()
    # Overdue adds a due_date < today predicate and forces not-completed.
    assert "due_date <" in sql
    assert "follow_up_tasks.completed is false" in sql


def test_tasks_q_searches_title_and_alumni_name(client):
    session = _fresh_session()
    app.dependency_overrides[get_current_db_user] = lambda: _ctx("full_access")
    app.dependency_overrides[get_session] = _with_session(session)

    response = client.get("/tasks?q=mentor")
    assert response.status_code == 200
    sql = _compiled(session.execute_args[0]).lower()
    assert "ilike" in sql
    assert "task_title" in sql
    assert "first_name" in sql
    assert "last_name" in sql


def test_tasks_assignee_filters_by_user_id(client):
    session = _fresh_session()
    app.dependency_overrides[get_current_db_user] = lambda: _ctx("full_access")
    app.dependency_overrides[get_session] = _with_session(session)

    response = client.get("/tasks?assignee=2&all=true")
    assert response.status_code == 200
    sql = _compiled(session.execute_args[0]).lower()
    assert "assigned_to_user_id =" in sql


def test_tasks_assignee_unassigned_filters_null(client):
    session = _fresh_session()
    app.dependency_overrides[get_current_db_user] = lambda: _ctx("full_access")
    app.dependency_overrides[get_session] = _with_session(session)

    response = client.get("/tasks?assignee=unassigned&all=true")
    assert response.status_code == 200
    sql = _compiled(session.execute_args[0]).lower()
    assert "assigned_to_user_id is null" in sql


def test_tasks_assignee_invalid_is_422(client):
    app.dependency_overrides[get_current_db_user] = lambda: _ctx("full_access")
    app.dependency_overrides[get_session] = _with_session(_fresh_session())

    response = client.get("/tasks?assignee=not-a-number")
    assert response.status_code == 422
