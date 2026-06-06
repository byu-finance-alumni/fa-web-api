"""Tests for the audit log filter query builder and routes.

The query-builder tests are pure unit tests: ``build_audit_query`` is compiled
to Postgres SQL and the clauses are asserted — no database needed (mirrors
``test_alumni_search``). The route tests cover the ``GET /audit/options``
endpoint's auth gate and serialization with a stubbed session (mirrors
``tests/test_events_routes.py``).
"""

import datetime
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.dialects import postgresql

from app.api.dependencies.auth import get_current_db_user
from app.core.database import get_session
from app.main import app
from app.repositories.audit import build_audit_query
from app.schemas.auth import UserContext


def _sql(stmt) -> str:
    return str(stmt.compile(dialect=postgresql.dialect()))


def test_default_no_where_newest_first():
    sql = _sql(build_audit_query())
    assert "WHERE" not in sql
    # always chronological, newest first
    assert "ORDER BY audit_logs.created_at DESC" in sql
    # actor email is joined in (left join so NULL-actor rows survive)
    assert "LEFT OUTER JOIN users" in sql


def test_action_type_exact():
    sql = _sql(build_audit_query(action_type="update"))
    assert "audit_logs.action_type =" in sql


def test_entity_type_exact():
    sql = _sql(build_audit_query(entity_type="alumni"))
    assert "audit_logs.entity_type =" in sql


def test_user_email_ilike():
    sql = _sql(build_audit_query(user="tanya"))
    assert "users.email ILIKE" in sql


def test_date_range_bounds_created_at():
    sql = _sql(
        build_audit_query(
            date_from=datetime.datetime(2026, 1, 1, tzinfo=datetime.UTC),
            date_to=datetime.datetime(2026, 1, 31, tzinfo=datetime.UTC),
        )
    )
    assert "audit_logs.created_at >=" in sql
    assert "audit_logs.created_at <=" in sql


def test_filters_combine():
    sql = _sql(
        build_audit_query(
            action_type="archive",
            entity_type="alumni",
            user="lee",
            date_from=datetime.datetime(2026, 6, 1, tzinfo=datetime.UTC),
        )
    )
    assert "audit_logs.action_type =" in sql
    assert "audit_logs.entity_type =" in sql
    assert "users.email ILIKE" in sql
    assert "audit_logs.created_at >=" in sql


# --- GET /audit/options route -------------------------------------------------


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
    """Returns queued result sets in order — one per execute() call."""

    def __init__(self, *result_sets):
        self._queue = list(result_sets)

    async def execute(self, _stmt):
        return _Result(self._queue.pop(0) if self._queue else [])


def _with_session(session):
    async def _override():
        yield session

    return _override


def test_audit_options_requires_auth(client):
    response = client.get("/audit/options")
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "unauthorized"


@pytest.mark.parametrize("path", ["/audit", "/audit/options"])
@pytest.mark.parametrize("role", ["view_only", "full_access"])
def test_audit_rejects_non_super_admin(client, path, role):
    """The audit trail (old/new values may contain alumni PII) is super_admin
    only — view and full access must both get 403."""
    app.dependency_overrides[get_current_db_user] = lambda: _ctx(role)
    response = client.get(path)
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "forbidden"


def test_audit_options_returns_distinct_action_and_entity_types(client):
    app.dependency_overrides[get_current_db_user] = lambda: _ctx("super_admin")
    # First execute() -> action types, second -> entity types.
    app.dependency_overrides[get_session] = _with_session(
        _FakeSession(
            [("activate_user",), ("archive",), ("create",)],
            [("alumni",), ("event",), ("user",)],
        )
    )
    response = client.get("/audit/options")
    assert response.status_code == 200
    assert response.json() == {
        "action_types": ["activate_user", "archive", "create"],
        "entity_types": ["alumni", "event", "user"],
    }
