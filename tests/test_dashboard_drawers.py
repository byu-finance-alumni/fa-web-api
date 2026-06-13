"""Tests for the dashboard KPI drill-down endpoints.

Auth-gating plus happy-path coverage using a stubbed session — no real
DATABASE_URL is required (CI has none). View access is granted to all three
roles, so reads have no 403 case. Mirrors tests/test_events_routes.py.
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
    def __init__(self, rows, scalars=(), executes=None):
        self._rows = rows
        self._scalars = list(scalars)
        # Optional queue of per-execute() result row lists, consumed in call
        # order. Falls back to ``rows`` for every call when not supplied — and
        # once the queue is drained.
        self._executes = list(executes) if executes is not None else None
        self.execute_args = []
        self.scalar_args = []

    async def execute(self, stmt):
        self.execute_args.append(stmt)
        if self._executes:
            return _Result(self._executes.pop(0))
        return _Result(self._rows)

    async def scalar(self, stmt):
        self.scalar_args.append(stmt)
        return self._scalars.pop(0) if self._scalars else 0


def _with_session(session):
    async def _override():
        yield session

    return _override


# --- auth gating --------------------------------------------------------------


@pytest.mark.parametrize(
    "path",
    [
        "/dashboard/contacted-this-month",
        "/dashboard/follow-ups",
        "/dashboard/activity",
        "/dashboard/data-quality",
        "/dashboard/birthdays",
    ],
)
def test_drilldowns_require_auth(client, path):
    response = client.get(path)
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "unauthorized"


# --- happy path (stubbed session) ---------------------------------------------


def _alumni():
    return SimpleNamespace(alumni_id=7, first_name="Jane", last_name="Doe")


def _user():
    return SimpleNamespace(
        user_id=2, first_name="Tanya", last_name="Harmon", email="th@byu.edu"
    )


def test_contacted_this_month_serializes_rows(client):
    when = datetime.datetime(2026, 6, 1, 12, 0, tzinfo=datetime.UTC)
    interaction = SimpleNamespace(
        interaction_id=11,
        alumni_id=7,
        interaction_type="Email",
        interaction_date_time=when,
        user_id=2,
    )
    rows = [(interaction, _alumni(), _user())]
    app.dependency_overrides[get_current_db_user] = lambda: _ctx("view_only")
    app.dependency_overrides[get_session] = _with_session(_FakeSession(rows))

    response = client.get("/dashboard/contacted-this-month")
    assert response.status_code == 200
    body = response.json()
    assert body == [
        {
            "interaction_id": 11,
            "alumni_id": 7,
            "alumni_name": "Jane Doe",
            "type": "Email",
            "when": when.isoformat(),
            "by": "Tanya Harmon",
        }
    ]


def test_activity_feed_paginates_and_serializes(client):
    when = datetime.datetime(2026, 5, 20, 9, 30, tzinfo=datetime.UTC)
    interaction = SimpleNamespace(
        interaction_id=12,
        alumni_id=7,
        interaction_type="Call",
        interaction_date_time=when,
        user_id=2,
    )
    rows = [(interaction, _alumni(), _user())]
    app.dependency_overrides[get_current_db_user] = lambda: _ctx("view_only")
    # execute() is called twice: the page rows, then the distinct-types query.
    app.dependency_overrides[get_session] = _with_session(
        _FakeSession(
            rows, scalars=[37], executes=[rows, [("Call",), ("Email",)]]
        )
    )

    response = client.get("/dashboard/activity?limit=25&offset=25")
    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 37
    assert body["limit"] == 25
    assert body["offset"] == 25
    assert body["types"] == ["Call", "Email"]
    assert body["items"] == [
        {
            "interaction_id": 12,
            "alumni_id": 7,
            "alumni_name": "Jane Doe",
            "type": "Call",
            "when": when.isoformat(),
            "by": "Tanya Harmon",
        }
    ]


@pytest.mark.parametrize("role", ["full_access", "super_admin"])
def test_data_quality_returns_counts(client, role):
    # Data-quality is full-access only (matches the sidebar gate), like /tasks.
    app.dependency_overrides[get_current_db_user] = lambda: _ctx(role)
    # Scalars consumed in handler order: total, missing_email,
    # missing_employer, duplicate_count.
    app.dependency_overrides[get_session] = _with_session(
        _FakeSession([], scalars=[100, 12, 9, 3])
    )

    response = client.get("/dashboard/data-quality")
    assert response.status_code == 200
    assert response.json() == {
        "total_alumni": 100,
        "missing_email": 12,
        "missing_employer": 9,
        "duplicate_count": 3,
    }


def test_data_quality_forbidden_for_view_only(client):
    # view_only callers get 403 (the sidebar already hides the link).
    app.dependency_overrides[get_current_db_user] = lambda: _ctx("view_only")
    app.dependency_overrides[get_session] = _with_session(_FakeSession([]))

    response = client.get("/dashboard/data-quality")
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "forbidden"


# --- activity feed filtering (SQL assertions on the compiled statement) -------


def _activity_session():
    """A fake session that records the rows/count statements and feeds the
    distinct-types query an empty list."""
    return _FakeSession([], scalars=[0], executes=[[], []])


def _compiled(stmt) -> str:
    from sqlalchemy.dialects import postgresql

    return str(stmt.compile(dialect=postgresql.dialect()))


def test_activity_q_searches_names_and_type_with_ilike(client):
    session = _activity_session()
    app.dependency_overrides[get_current_db_user] = lambda: _ctx("view_only")
    app.dependency_overrides[get_session] = _with_session(session)

    response = client.get("/dashboard/activity?q=jane")
    assert response.status_code == 200
    # The page-rows statement carries the filter predicates.
    sql = _compiled(session.execute_args[0])
    # first / last / preferred name + interaction_type -> 4 ILIKE clauses.
    assert sql.count("ILIKE") == 4
    assert "first_name" in sql
    assert "last_name" in sql
    assert "preferred_first_name" in sql
    assert "interaction_type" in sql
    # The count query is filtered too, so totals match the page.
    count_sql = _compiled(session.scalar_args[0])
    assert "ILIKE" in count_sql


def test_activity_type_filters_exact_ilike(client):
    session = _activity_session()
    app.dependency_overrides[get_current_db_user] = lambda: _ctx("view_only")
    app.dependency_overrides[get_session] = _with_session(session)

    response = client.get("/dashboard/activity?type=Email")
    assert response.status_code == 200
    sql = _compiled(session.execute_args[0])
    assert "interaction_type ILIKE" in sql


def test_activity_date_range_bounds_full_day(client):
    session = _activity_session()
    app.dependency_overrides[get_current_db_user] = lambda: _ctx("view_only")
    app.dependency_overrides[get_session] = _with_session(session)

    response = client.get(
        "/dashboard/activity?date_from=2026-01-01&date_to=2026-01-31"
    )
    assert response.status_code == 200
    sql = _compiled(session.execute_args[0])
    assert "interaction_date_time >=" in sql
    assert "interaction_date_time <=" in sql


def test_activity_no_filters_has_no_where(client):
    session = _activity_session()
    app.dependency_overrides[get_current_db_user] = lambda: _ctx("view_only")
    app.dependency_overrides[get_session] = _with_session(session)

    response = client.get("/dashboard/activity")
    assert response.status_code == 200
    sql = _compiled(session.execute_args[0])
    assert "ILIKE" not in sql
    assert "interaction_date_time >=" not in sql


def test_summary_includes_this_month_kpis(client):
    app.dependency_overrides[get_current_db_user] = lambda: _ctx("view_only")
    # Scalars consumed in handler order: total, archived, deceased,
    # missing_email, missing_employer, contacted_this_month,
    # upcoming_follow_ups, duplicate_count, attended_event_this_month,
    # upcoming_events, events_this_month, guest_speakers_this_month,
    # piff_donors, willing_mentors. The three execute() calls (cohort /
    # top_employers / by_state) fall back to the empty rows list.
    scalars = [100, 5, 2, 12, 9, 8, 4, 3, 6, 7, 11, 2, 1, 0]
    app.dependency_overrides[get_session] = _with_session(
        _FakeSession([], scalars=scalars)
    )

    response = client.get("/dashboard/summary")
    assert response.status_code == 200
    body = response.json()
    assert body["events_this_month"] == 11
    assert body["guest_speakers_this_month"] == 2


def test_summary_guest_speaker_signal_uses_attendance_status_ilike(client):
    app.dependency_overrides[get_current_db_user] = lambda: _ctx("view_only")
    session = _FakeSession([], scalars=[0] * 14)
    app.dependency_overrides[get_session] = _with_session(session)

    response = client.get("/dashboard/summary")
    assert response.status_code == 200
    # The 12th scalar query (index 11) is the guest-speaker KPI; assert it
    # filters event attendance by a "speaker" status (the chosen signal).
    speaker_sql = _compiled(session.scalar_args[11])
    assert "attendance_status ILIKE" in speaker_sql
    assert "event_date" in speaker_sql


def test_birthdays_serializes_rows_matching_contract(client):
    # The handler selects (Alumni, current_employer) rows.
    alum = SimpleNamespace(
        alumni_id=7,
        first_name="Jane",
        last_name="Doe",
        graduation_year=2019,
        birth_date=datetime.date(1997, 6, 3),
    )
    rows = [(alum, "Goldman Sachs")]
    app.dependency_overrides[get_current_db_user] = lambda: _ctx("view_only")
    app.dependency_overrides[get_session] = _with_session(_FakeSession(rows))

    response = client.get("/dashboard/birthdays")
    assert response.status_code == 200
    assert response.json() == [
        {
            "id": 7,
            "first_name": "Jane",
            "last_name": "Doe",
            "current_employer": "Goldman Sachs",
            "graduation_year": 2019,
            "birth_date": "1997-06-03",
        }
    ]


def test_birthdays_handles_null_employer(client):
    alum = SimpleNamespace(
        alumni_id=8,
        first_name="John",
        last_name="Smith",
        graduation_year=None,
        birth_date=datetime.date(2000, 6, 15),
    )
    rows = [(alum, None)]
    app.dependency_overrides[get_current_db_user] = lambda: _ctx("view_only")
    app.dependency_overrides[get_session] = _with_session(_FakeSession(rows))

    response = client.get("/dashboard/birthdays")
    assert response.status_code == 200
    body = response.json()
    assert body[0]["current_employer"] is None
    assert body[0]["graduation_year"] is None


def test_birthdays_filters_current_month_and_orders_by_day(client):
    session = _FakeSession([])
    app.dependency_overrides[get_current_db_user] = lambda: _ctx("view_only")
    app.dependency_overrides[get_session] = _with_session(session)

    response = client.get("/dashboard/birthdays")
    assert response.status_code == 200
    sql = _compiled(session.execute_args[0])
    # Month-filter on birth_date and day-of-month ordering both present.
    assert "EXTRACT(month FROM alumni.birth_date)" in sql
    assert "ORDER BY EXTRACT(day FROM alumni.birth_date)" in sql
    # Only active (non-archived) alumni with a birth_date on file.
    assert "birth_date IS NOT NULL" in sql
    assert "archived" in sql


def test_follow_ups_serializes_rows_and_handles_missing_user(client):
    task = SimpleNamespace(
        follow_up_task_id=31,
        alumni_id=7,
        task_title="Call about mentoring",
        due_date=datetime.date(2026, 6, 10),
        assigned_to_user_id=None,
    )
    rows = [(task, _alumni(), None)]
    app.dependency_overrides[get_current_db_user] = lambda: _ctx("view_only")
    app.dependency_overrides[get_session] = _with_session(_FakeSession(rows))

    response = client.get("/dashboard/follow-ups")
    assert response.status_code == 200
    body = response.json()
    assert body == [
        {
            "task_id": 31,
            "alumni_id": 7,
            "alumni_name": "Jane Doe",
            "title": "Call about mentoring",
            "due_date": "2026-06-10",
            "assigned_to": None,
        }
    ]
