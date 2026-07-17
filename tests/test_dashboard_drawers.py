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
        self.added: list = []
        self.commits = 0

    async def execute(self, stmt):
        self.execute_args.append(stmt)
        if self._executes:
            return _Result(self._executes.pop(0))
        return _Result(self._rows)

    async def scalar(self, stmt):
        self.scalar_args.append(stmt)
        return self._scalars.pop(0) if self._scalars else 0

    # The FERPA-audited drill-downs write a best-effort AuditLog after serving
    # the read; support add/commit/rollback so the audit path is exercised.
    def add(self, obj):
        self.added.append(obj)

    async def commit(self):
        self.commits += 1

    async def rollback(self):
        pass


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


def _alumni(preferred_first_name=None):
    # Real Alumni ORM rows always carry preferred_first_name; the drawer
    # serializers prefer it over first_name for the displayed alumni_name.
    return SimpleNamespace(
        alumni_id=7,
        first_name="Jane",
        last_name="Doe",
        preferred_first_name=preferred_first_name,
    )


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
    # FERPA: this drill-down now requires full_access (view_only gets 403).
    app.dependency_overrides[get_current_db_user] = lambda: _ctx("full_access")
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
            "by_user_id": 2,
        }
    ]


def test_contacted_this_month_list_excludes_archived_and_friends(client):
    # The drill-down list must apply the same active-alumni predicate as the
    # KPI count (archived=false AND is_alumni=true) so archived / friend-of-
    # program records never leak into the list and the row count reconciles
    # with the tile (#179). We assert on the compiled page-rows statement.
    session = _FakeSession([])
    app.dependency_overrides[get_current_db_user] = lambda: _ctx("full_access")
    app.dependency_overrides[get_session] = _with_session(session)

    response = client.get("/dashboard/contacted-this-month")
    assert response.status_code == 200
    sql = _compiled(session.execute_args[0])
    assert "archived" in sql
    assert "is_alumni" in sql


def test_summary_event_kpis_filter_active_alumni(client):
    # The attended-event (#12, index 11) and guest-speaker (#15, index 14) KPIs
    # must join Alumni and filter archived / is_alumni like every other alumni
    # KPI, so friends-of-program and archived records don't inflate them (#179).
    app.dependency_overrides[get_current_db_user] = lambda: _ctx("view_only")
    session = _FakeSession([], scalars=[0] * 17)
    app.dependency_overrides[get_session] = _with_session(session)

    response = client.get("/dashboard/summary")
    assert response.status_code == 200
    attended_sql = _compiled(session.scalar_args[11])
    assert "archived" in attended_sql
    assert "is_alumni" in attended_sql
    speaker_sql = _compiled(session.scalar_args[14])
    assert "archived" in speaker_sql
    assert "is_alumni" in speaker_sql


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
    # FERPA: the searchable activity feed now requires full_access.
    app.dependency_overrides[get_current_db_user] = lambda: _ctx("full_access")
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
            "by_user_id": 2,
        }
    ]


@pytest.mark.parametrize("role", ["full_access", "super_admin"])
def test_data_quality_returns_counts(client, role):
    # Data-quality is full-access only (matches the sidebar gate), like /tasks.
    app.dependency_overrides[get_current_db_user] = lambda: _ctx(role)
    # Scalars consumed in handler order: total, missing_email,
    # missing_employer, missing_phone, complete_alumni, duplicate_count.
    app.dependency_overrides[get_session] = _with_session(
        _FakeSession([], scalars=[100, 12, 9, 7, 80, 3])
    )

    response = client.get("/dashboard/data-quality")
    assert response.status_code == 200
    assert response.json() == {
        "total_alumni": 100,
        "complete_alumni": 80,
        "missing_email": 12,
        "missing_employer": 9,
        "missing_phone": 7,
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
    # FERPA: the activity feed now requires full_access.
    app.dependency_overrides[get_current_db_user] = lambda: _ctx("full_access")
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
    # FERPA: the activity feed now requires full_access.
    app.dependency_overrides[get_current_db_user] = lambda: _ctx("full_access")
    app.dependency_overrides[get_session] = _with_session(session)

    response = client.get("/dashboard/activity?type=Email")
    assert response.status_code == 200
    sql = _compiled(session.execute_args[0])
    assert "interaction_type ILIKE" in sql


def test_activity_date_range_bounds_full_day(client):
    session = _activity_session()
    # FERPA: the activity feed now requires full_access.
    app.dependency_overrides[get_current_db_user] = lambda: _ctx("full_access")
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
    # FERPA: the activity feed now requires full_access.
    app.dependency_overrides[get_current_db_user] = lambda: _ctx("full_access")
    app.dependency_overrides[get_session] = _with_session(session)

    response = client.get("/dashboard/activity")
    assert response.status_code == 200
    sql = _compiled(session.execute_args[0])
    assert "ILIKE" not in sql
    assert "interaction_date_time >=" not in sql
    # No actor predicate unless mine=true.
    assert "interactions.user_id =" not in sql


def test_activity_mine_filters_to_current_actor(client):
    session = _activity_session()
    # _ctx("full_access") resolves to user_id=1, so the actor predicate must
    # bind that id in both the page-rows and the count statements.
    app.dependency_overrides[get_current_db_user] = lambda: _ctx("full_access")
    app.dependency_overrides[get_session] = _with_session(session)

    response = client.get("/dashboard/activity?mine=true")
    assert response.status_code == 200
    sql = _compiled(session.execute_args[0])
    assert "interactions.user_id =" in sql
    # The count query is filtered too, so totals match the page.
    count_sql = _compiled(session.scalar_args[0])
    assert "interactions.user_id =" in count_sql


def test_activity_mine_returns_only_current_user_rows(client):
    # Two rows are returned by the stub; the SQL is what restricts to the actor,
    # so we assert the bound actor id is the current user (user_id=1), not the
    # row authors. Confirms the predicate binds actor.user_id, not a constant.
    when = datetime.datetime(2026, 5, 20, 9, 30, tzinfo=datetime.UTC)
    mine_row = (
        SimpleNamespace(
            interaction_id=12,
            alumni_id=7,
            interaction_type="Call",
            interaction_date_time=when,
            user_id=1,
        ),
        _alumni(),
        SimpleNamespace(
            user_id=1, first_name="Me", last_name="Self", email="me@byu.edu"
        ),
    )
    session = _FakeSession([], scalars=[1], executes=[[mine_row], [("Call",)]])
    app.dependency_overrides[get_current_db_user] = lambda: _ctx("full_access")
    app.dependency_overrides[get_session] = _with_session(session)

    response = client.get("/dashboard/activity?mine=1")
    assert response.status_code == 200
    body = response.json()
    assert body["items"][0]["by_user_id"] == 1
    # The bound parameter on the page query is the authenticated user's id.
    from sqlalchemy.dialects import postgresql

    compiled = session.execute_args[0].compile(dialect=postgresql.dialect())
    assert 1 in compiled.params.values()


def test_activity_mine_combines_with_other_filters(client):
    session = _activity_session()
    app.dependency_overrides[get_current_db_user] = lambda: _ctx("full_access")
    app.dependency_overrides[get_session] = _with_session(session)

    response = client.get("/dashboard/activity?mine=true&q=jane&type=Email")
    assert response.status_code == 200
    sql = _compiled(session.execute_args[0])
    # Actor predicate AND the text/type filters all present together.
    assert "interactions.user_id =" in sql
    assert sql.count("ILIKE") >= 4
    assert "interaction_type ILIKE" in sql


def test_summary_includes_this_month_kpis(client):
    app.dependency_overrides[get_current_db_user] = lambda: _ctx("view_only")
    # Scalars consumed in handler order: total, archived, deceased,
    # missing_email, missing_employer, contacted_this_month,
    # not_contacted_6mo, not_contacted_12mo, not_contacted_24mo,
    # upcoming_follow_ups, duplicate_count, attended_event_this_month,
    # upcoming_events, events_this_month, guest_speakers_this_month,
    # piff_donors, willing_mentors. The three execute() calls (cohort /
    # top_employers / by_state) fall back to the empty rows list.
    scalars = [100, 5, 2, 12, 9, 8, 60, 30, 10, 4, 3, 6, 7, 11, 2, 1, 0]
    app.dependency_overrides[get_session] = _with_session(
        _FakeSession([], scalars=scalars)
    )

    response = client.get("/dashboard/summary")
    assert response.status_code == 200
    body = response.json()
    assert body["events_this_month"] == 11
    assert body["guest_speakers_this_month"] == 2


def test_summary_industry_breakdown_separates_other_and_unknown(client):
    # #351/#352/#353: the industry breakdown lists EVERY canonical finance
    # industry (incl. zero-count), folds case variants, routes literal "Other"
    # and any non-vocab value into a separate "other" bucket, and reports
    # "unknown" (no industry on file) as its own count distinct from "other".
    from app.api.routes.dashboard import _FINANCE_INDUSTRIES

    app.dependency_overrides[get_current_db_user] = lambda: _ctx("view_only")
    # total = 100 (scalar #0); every other KPI scalar is 0.
    scalars = [100] + [0] * 16
    # execute() order in the handler: cohort, top_employers, industry, by_state.
    industry_rows = [
        ("Investment Banking", 30),
        ("investment banking", 5),  # case-insensitive fold into the canonical
        ("Other", 10),  # literal catch-all -> "other" bucket
        ("Underwater Basket Weaving", 3),  # non-vocab value -> "other" bucket
    ]
    session = _FakeSession(
        [], scalars=scalars, executes=[[], [], industry_rows, []]
    )
    app.dependency_overrides[get_session] = _with_session(session)

    response = client.get("/dashboard/summary")
    assert response.status_code == 200
    breakdown = response.json()["industry_breakdown"]
    # Every canonical finance industry appears, in canonical order.
    assert [r["industry"] for r in breakdown["industries"]] == list(
        _FINANCE_INDUSTRIES
    )
    counts = {r["industry"]: r["count"] for r in breakdown["industries"]}
    assert counts["Investment Banking"] == 35  # 30 + 5 folded together
    assert counts["Asset Management"] == 0  # zero-count industry still listed
    assert breakdown["other"] == 13  # literal "Other" (10) + non-vocab (3)
    # known = 35 + 13 = 48; unknown = active total (100) - known (48) = 52.
    assert breakdown["unknown"] == 52


def test_summary_top_employers_union_covers_history_and_current(client):
    # #355: Top employers now aggregates over the last 5 years — a UNION of the
    # current job and recent/ongoing employment_history, counted DISTINCT by
    # alumnus. Assert the compiled statement references BOTH source tables and
    # the recency predicate (start_year / is_current / end_year).
    app.dependency_overrides[get_current_db_user] = lambda: _ctx("view_only")
    session = _FakeSession([], scalars=[0] * 17)
    app.dependency_overrides[get_session] = _with_session(session)

    response = client.get("/dashboard/summary")
    assert response.status_code == 200
    # execute() #2 (index 1) is the top-employers union query.
    sql = _compiled(session.execute_args[1])
    assert "current_employment" in sql
    assert "employment_history" in sql
    assert "start_year" in sql
    assert "is_current" in sql


def test_summary_guest_speaker_signal_uses_attendance_status_ilike(client):
    app.dependency_overrides[get_current_db_user] = lambda: _ctx("view_only")
    session = _FakeSession([], scalars=[0] * 17)
    app.dependency_overrides[get_session] = _with_session(session)

    response = client.get("/dashboard/summary")
    assert response.status_code == 200
    # The guest-speaker KPI is scalar #15 (index 14) after the 3 not-contacted
    # counts were inserted earlier in the handler; assert it filters event
    # attendance by a "speaker" status (the chosen signal).
    speaker_sql = _compiled(session.scalar_args[14])
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
    # FERPA: only the recurring month+day is returned (never the birth year).
    assert response.json() == [
        {
            "id": 7,
            "first_name": "Jane",
            "last_name": "Doe",
            "current_employer": "Goldman Sachs",
            "graduation_year": 2019,
            "birth_month": 6,
            "birth_day": 3,
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
    # FERPA: the follow-ups drill-down now requires full_access.
    app.dependency_overrides[get_current_db_user] = lambda: _ctx("full_access")
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


# --- summary: not-contacted-in-N-months counts (#42) --------------------------


def test_summary_requires_auth(client):
    response = client.get("/dashboard/summary")
    assert response.status_code == 401


def test_summary_includes_not_contacted_counts(client):
    # Empty scalar queue -> every count resolves to 0; we only assert the new
    # keys are wired into the response as ints (exact values are DB semantics).
    session = _FakeSession(rows=[])
    app.dependency_overrides[get_session] = _with_session(session)
    app.dependency_overrides[get_current_db_user] = lambda: _ctx("view_only")
    response = client.get("/dashboard/summary")
    app.dependency_overrides.clear()
    assert response.status_code == 200
    body = response.json()
    for key in ("not_contacted_6mo", "not_contacted_12mo", "not_contacted_24mo"):
        assert key in body
        assert isinstance(body[key], int)


def test_summary_contacted_this_month_excludes_archived(client):
    # The contacted-this-month KPI (scalar #6, index 5) must join Alumni and
    # filter archived, matching every other count on the endpoint (#112).
    app.dependency_overrides[get_current_db_user] = lambda: _ctx("view_only")
    session = _FakeSession([], scalars=[0] * 17)
    app.dependency_overrides[get_session] = _with_session(session)
    response = client.get("/dashboard/summary")
    app.dependency_overrides.clear()
    assert response.status_code == 200
    sql = _compiled(session.scalar_args[5])
    assert "archived" in sql


# --- #187: responses validate against their declared response_model ----------


def test_data_quality_response_validates_against_model(client):
    # #187: /dashboard/data-quality now declares a concrete response_model
    # (DataQuality). The served body must validate cleanly against it AND
    # round-trip unchanged, proving the schema matches the real shape.
    from app.schemas.dashboard import DataQuality

    app.dependency_overrides[get_current_db_user] = lambda: _ctx("full_access")
    # Six scalars: total, missing_email, missing_employer, missing_phone,
    # complete_alumni, duplicate_count.
    session = _FakeSession([], scalars=[10, 3, 2, 5, 6, 1])
    app.dependency_overrides[get_session] = _with_session(session)

    response = client.get("/dashboard/data-quality")
    assert response.status_code == 200
    body = response.json()
    model = DataQuality.model_validate(body)
    assert model.total_alumni == 10
    assert model.duplicate_count == 1
    # No fields dropped or added by the response_model.
    assert model.model_dump() == body


def test_summary_response_validates_against_model(client):
    # #187: /dashboard/summary declares DashboardSummary; the served body must
    # validate against it with no missing/extra keys.
    from app.schemas.dashboard import DashboardSummary

    app.dependency_overrides[get_current_db_user] = lambda: _ctx("view_only")
    # 17 scalar KPIs; the three distribution queries fall back to empty rows.
    session = _FakeSession([], scalars=[0] * 17)
    app.dependency_overrides[get_session] = _with_session(session)

    response = client.get("/dashboard/summary")
    assert response.status_code == 200
    body = response.json()
    model = DashboardSummary.model_validate(body)
    assert model.model_dump() == body
