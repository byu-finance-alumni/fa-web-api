"""Tests for the dashboard KPI drill-down endpoints.

Auth-gating plus happy-path coverage using a stubbed session — no real
DATABASE_URL is required (CI has none). View access is granted to all three
roles, so reads have no 403 case. Mirrors tests/test_events_routes.py.
"""

import datetime
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app.api.dependencies.auth import get_current_db_user
from app.api.routes import dashboard as dashboard_routes
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
    session = _FakeSession([], scalars=[0] * 18)
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
    # piff_donors, willing_mentors, alumni_edited_this_month,
    # alumni_edited_this_year. The three execute() calls (cohort /
    # top_employers / by_state) fall back to the empty rows list.
    scalars = [100, 5, 2, 12, 9, 8, 60, 30, 10, 4, 3, 6, 7, 11, 2, 1, 0, 23, 91]
    app.dependency_overrides[get_session] = _with_session(
        _FakeSession([], scalars=scalars)
    )

    response = client.get("/dashboard/summary")
    assert response.status_code == 200
    body = response.json()
    assert body["events_this_month"] == 11
    assert body["guest_speakers_this_month"] == 2
    # #606/#645: the alumni-edited tile reads the LAST TWO scalars in handler
    # order — month first, then the year-to-date running total under it.
    assert body["alumni_edited_this_month"] == 23
    assert body["alumni_edited_this_year"] == 91


# --- #606: "alumni edited this month" KPI ------------------------------------


def test_summary_alumni_edited_this_month_counts_updated_at_in_calendar_month(
    client,
):
    # #606: the KPI must be a single aggregate COUNT with a WHERE on
    # alumni.updated_at — never a fetch-rows-and-count-in-Python (8,000+
    # records) — and it must apply the same active-alumni predicate as every
    # other alumni KPI so archived / friend-of-program rows can't inflate it.
    app.dependency_overrides[get_current_db_user] = lambda: _ctx("view_only")
    session = _FakeSession([], scalars=[0] * 18)
    app.dependency_overrides[get_session] = _with_session(session)

    response = client.get("/dashboard/summary")
    assert response.status_code == 200
    # Scalar #18 (index 17) — appended after willing_mentors in the handler.
    sql = _compiled(session.scalar_args[17])
    assert "count(*)" in sql
    assert "alumni.updated_at >=" in sql
    assert "archived" in sql
    assert "is_alumni" in sql
    # Aggregate only: no row selection / limit sneaking in.
    assert "LIMIT" not in sql


def test_summary_alumni_edited_month_boundary_is_first_of_month_utc(client):
    # The window is the CURRENT CALENDAR MONTH (1st 00:00 through now), NOT the
    # rolling 30-day window the contacted/attended KPIs use. The boundary is
    # computed from the server's UTC date, matching the other calendar-month
    # KPIs on this endpoint and the frontend's UTC date helpers.
    from sqlalchemy.dialects import postgresql

    app.dependency_overrides[get_current_db_user] = lambda: _ctx("view_only")
    session = _FakeSession([], scalars=[0] * 18)
    app.dependency_overrides[get_session] = _with_session(session)

    response = client.get("/dashboard/summary")
    assert response.status_code == 200
    params = session.scalar_args[17].compile(dialect=postgresql.dialect()).params
    bounds = [v for v in params.values() if isinstance(v, datetime.datetime)]
    assert len(bounds) == 1
    bound = bounds[0]
    today = datetime.datetime.now(datetime.UTC).date()
    assert bound == datetime.datetime.combine(
        today.replace(day=1), datetime.time.min, tzinfo=datetime.UTC
    )
    assert bound.tzinfo is not None
    assert bound.utcoffset() == datetime.timedelta(0)


# --- #645: "alumni edited this year" KPI (calendar year to date) -------------


def _freeze_dashboard_clock(monkeypatch, when: datetime.datetime) -> None:
    """Pin the dashboard module's clock to ``when`` (a tz-aware UTC datetime).

    Every window bound on /dashboard/summary is derived from
    ``datetime.datetime.now(datetime.UTC)``, so month/year boundaries otherwise
    depend on the day the suite happens to run — and "a previous month, same
    year" is unrepresentable in January. We swap the module's ``datetime``
    global for a shim that proxies the stdlib module and only overrides
    ``now()``; date/time/timedelta/UTC pass straight through, so
    ``combine``/``_months_before`` behave exactly as in production.
    """
    from app.api.routes import dashboard

    class _FrozenDatetime(datetime.datetime):
        @classmethod
        def now(cls, tz=None):  # noqa: D102 - test shim
            return when if tz is None else when.astimezone(tz)

    monkeypatch.setattr(
        dashboard,
        "datetime",
        SimpleNamespace(
            datetime=_FrozenDatetime,
            date=datetime.date,
            time=datetime.time,
            timedelta=datetime.timedelta,
            UTC=datetime.UTC,
        ),
    )


def _updated_at_bound(stmt) -> datetime.datetime:
    """The single datetime bound bound into an alumni-edited COUNT statement."""
    from sqlalchemy.dialects import postgresql

    params = stmt.compile(dialect=postgresql.dialect()).params
    bounds = [v for v in params.values() if isinstance(v, datetime.datetime)]
    assert len(bounds) == 1
    return bounds[0]


def _edited_bounds(session) -> tuple[datetime.datetime, datetime.datetime]:
    """(month_bound, year_bound) — scalars #18 and #19 (indexes 17 and 18) in
    handler order; the year count is appended directly after the month count."""
    return (
        _updated_at_bound(session.scalar_args[17]),
        _updated_at_bound(session.scalar_args[18]),
    )


def _summary_session(client, monkeypatch=None, when=None):
    app.dependency_overrides[get_current_db_user] = lambda: _ctx("view_only")
    if monkeypatch is not None and when is not None:
        _freeze_dashboard_clock(monkeypatch, when)
    session = _FakeSession([], scalars=[0] * 19)
    app.dependency_overrides[get_session] = _with_session(session)
    response = client.get("/dashboard/summary")
    assert response.status_code == 200
    return session, response


def test_summary_edited_previous_month_counts_for_year_not_month(
    client, monkeypatch
):
    # #645: an alumnus edited EARLIER THIS YEAR but in a previous month belongs
    # to the year running total and must NOT appear in the month figure — the
    # tile stacks "this month" over "this year" and they'd contradict each other
    # otherwise. Clock frozen to August so "a previous month, same year" exists
    # (in January there is no such month, which is exactly why we freeze).
    session, _ = _summary_session(
        client,
        monkeypatch,
        datetime.datetime(2026, 8, 6, 12, 0, tzinfo=datetime.UTC),
    )
    month_bound, year_bound = _edited_bounds(session)

    edited_in_march = datetime.datetime(2026, 3, 14, 9, 0, tzinfo=datetime.UTC)
    assert edited_in_march >= year_bound  # counted by the year KPI
    assert edited_in_march < month_bound  # excluded from the month KPI


def test_summary_alumni_edited_year_boundary_is_jan_1_utc(client, monkeypatch):
    # The year window is CALENDAR YEAR TO DATE: 1 January 00:00 UTC through now.
    # It is NOT a rolling 12 months, so 31 December of the prior year is out and
    # the count legitimately collapses to near zero every 1 January.
    session, _ = _summary_session(
        client,
        monkeypatch,
        datetime.datetime(2026, 8, 6, 12, 0, tzinfo=datetime.UTC),
    )
    _, year_bound = _edited_bounds(session)

    assert year_bound == datetime.datetime(2026, 1, 1, tzinfo=datetime.UTC)
    # UTC-anchored like every other date filter in the app.
    assert year_bound.tzinfo is not None
    assert year_bound.utcoffset() == datetime.timedelta(0)
    # A rolling-12-month window would have swept this in; year-to-date must not.
    assert (
        datetime.datetime(2025, 12, 31, 23, 59, tzinfo=datetime.UTC) < year_bound
    )


def test_summary_alumni_edited_year_boundary_tracks_live_clock(client):
    # Same assertion against the real server clock (no freeze), so the bound can
    # never drift away from 1 Jan of the CURRENT year.
    session, _ = _summary_session(client)
    _, year_bound = _edited_bounds(session)
    today = datetime.datetime.now(datetime.UTC).date()
    assert year_bound == datetime.datetime.combine(
        today.replace(month=1, day=1), datetime.time.min, tzinfo=datetime.UTC
    )


def test_summary_alumni_edited_year_is_always_at_least_the_month(
    client, monkeypatch
):
    # The year count is a strict SUPERSET of the month count. That is guaranteed
    # structurally rather than by arithmetic: both counts run the SAME query over
    # the SAME population and differ only in the updated_at lower bound, and the
    # year bound is never later than the month bound. Assert both halves.
    session, _ = _summary_session(
        client,
        monkeypatch,
        datetime.datetime(2026, 8, 6, 12, 0, tzinfo=datetime.UTC),
    )
    month_bound, year_bound = _edited_bounds(session)
    assert year_bound <= month_bound

    month_sql = _compiled(session.scalar_args[17])
    year_sql = _compiled(session.scalar_args[18])
    # Identical SQL (same table, same `active` predicate, same >= comparison) —
    # only the bound parameter's value differs.
    assert month_sql == year_sql
    assert "alumni.updated_at >=" in year_sql
    assert "archived" in year_sql
    assert "is_alumni" in year_sql

    # And in January, when the two bounds coincide, the counts are equal — the
    # expected "drops to near zero each January" behaviour, not a bug.
    session_jan, _ = _summary_session(
        client,
        monkeypatch,
        datetime.datetime(2026, 1, 9, 12, 0, tzinfo=datetime.UTC),
    )
    jan_month_bound, jan_year_bound = _edited_bounds(session_jan)
    assert jan_year_bound == jan_month_bound == datetime.datetime(
        2026, 1, 1, tzinfo=datetime.UTC
    )


def test_summary_alumni_edited_year_counts_records_not_changes(client):
    # THE key requirement: ten edits to one alumnus is ONE record. This holds by
    # construction — the KPI counts rows in the `alumni` table (one row per
    # alumnus) filtered on updated_at, so repeated writes only move that one
    # row's timestamp. Guard the shape so nobody rebuilds it on audit_logs,
    # which stores one row per changed FIELD and also carries
    # action_type='search'/'preview' rows (double inflation).
    session, _ = _summary_session(client)
    for index in (17, 18):
        sql = _compiled(session.scalar_args[index])
        assert "count(*)" in sql
        assert "FROM alumni" in sql
        assert "audit_logs" not in sql
        assert "action_type" not in sql
        assert "JOIN" not in sql
        # Aggregate only — never fetch rows and count in Python (8,000+ alumni).
        assert "LIMIT" not in sql


def test_summary_alumni_edited_year_in_response_body(client):
    # The exact field name the frontend tile reads, alongside the month figure.
    app.dependency_overrides[get_current_db_user] = lambda: _ctx("view_only")
    scalars = [0] * 17 + [23, 91]
    app.dependency_overrides[get_session] = _with_session(
        _FakeSession([], scalars=scalars)
    )

    response = client.get("/dashboard/summary")
    assert response.status_code == 200
    body = response.json()
    assert body["alumni_edited_this_month"] == 23
    assert body["alumni_edited_this_year"] == 91
    assert body["alumni_edited_this_year"] >= body["alumni_edited_this_month"]


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
    session = _FakeSession([], scalars=[0] * 18)
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
    session = _FakeSession([], scalars=[0] * 18)
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
    session = _FakeSession([], scalars=[0] * 18)
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
    # 19 scalar KPIs; the three distribution queries fall back to empty rows.
    session = _FakeSession([], scalars=[0] * 19)
    app.dependency_overrides[get_session] = _with_session(session)

    response = client.get("/dashboard/summary")
    assert response.status_code == 200
    body = response.json()
    model = DashboardSummary.model_validate(body)
    assert model.model_dump() == body


# --- #608: Military folds into "Other" ---------------------------------------


def _summary_breakdown(client, industry_rows):
    """Run GET /dashboard/summary with a stubbed industry aggregation.

    execute() calls in handler order: cohort, top_employers, industry_rows,
    by_state. ``total`` is the first scalar and drives the Unknown remainder.
    """
    app.dependency_overrides[get_current_db_user] = lambda: _ctx("view_only")
    session = _FakeSession(
        [],
        scalars=[100] + [0] * 17,
        executes=[[], [], industry_rows, []],
    )
    app.dependency_overrides[get_session] = _with_session(session)
    response = client.get("/dashboard/summary")
    assert response.status_code == 200
    return response.json()["industry_breakdown"]


def test_military_folds_into_other(client):
    """Jake (#608) kept the industry chart about FINANCE SECTORS, so Military
    gets no bar of its own — it lands in the "Other" catch-all like Law or FP&A.
    That is the default path for a non-wheel industry, deliberately not a special
    case."""
    breakdown = _summary_breakdown(
        client,
        [("Military", 7), ("Other", 3), ("Consulting", 5)],
    )
    assert breakdown["other"] == 10
    assert "military" not in breakdown


def test_military_is_not_merged_into_the_unknown_data_gap_bar(client):
    """Folding into Other is not the same as folding into Unknown: an alumnus
    who told us they serve is not a missing-industry record."""
    breakdown = _summary_breakdown(client, [("Military", 7)])
    assert breakdown["unknown"] == 93  # the blank remainder only
    assert breakdown["other"] == 7


def test_military_is_not_a_wheel_bar(client):
    """It must not appear among the finance industries the wheel lists."""
    breakdown = _summary_breakdown(client, [("Military", 7)])
    assert "Military" not in {row["industry"] for row in breakdown["industries"]}


def test_graduate_student_and_unknown_buckets_are_unaffected(client):
    """Regression guard: Military must not disturb the two values that ARE
    special-cased out of the Other fold."""
    breakdown = _summary_breakdown(
        client,
        [("Graduate Student", 2), ("Unknown", 3), ("Military", 4), ("Law", 1)],
    )
    assert breakdown["graduate_student"] == 2
    # Military + Law both fold into Other.
    assert breakdown["other"] == 5
    # Explicit "Unknown" merges into the blank-industry data-gap bar.
    assert breakdown["unknown"] == (100 - 10) + 3


# ============================== DISTINCT COMPANIES (2026-08-20) ==============


def _scalar_sql_mentioning(session, needle: str) -> str:
    """The one compiled scalar statement containing ``needle``.

    Positional indexing into `scalar_args` was how these started, and it broke
    the moment another count was added to the handler — silently, by pointing
    the assertion at a different query. Asserting there is EXACTLY one match
    also catches the opposite mistake: two queries that should have been one.
    """
    hits = [
        sql
        for sql in (_compiled(stmt) for stmt in session.scalar_args)
        if needle in sql.lower()
    ]
    assert len(hits) == 1, f"expected one statement mentioning {needle}, got {len(hits)}"
    return hits[0].lower()

def test_summary_distinct_employers_in_response_body(client):
    # The fourth KPI tile and its sub-line. All three are appended after the
    # pre-existing scalars — see the note in the route about the positional stub.
    app.dependency_overrides[get_current_db_user] = lambda: _ctx("view_only")
    scalars = [0] * 19 + [147, 12, 6]
    app.dependency_overrides[get_session] = _with_session(
        _FakeSession([], scalars=scalars)
    )

    response = client.get("/dashboard/summary")
    assert response.status_code == 200
    body = response.json()
    assert body["distinct_employers"] == 147
    assert body["employer_states"] == 12
    # #754: the sub-line is "Across N states and M countries" — BOTH halves come
    # from this response, so the frontend never has to invent the second number.
    assert body["employer_countries"] == 6


def test_distinct_employers_folds_case_and_whitespace_before_counting(client):
    """⚠️ THE NORMALISATION IS THE WHOLE NUMBER.

    `current_employer` is free text with no write validation, so without
    `lower(trim(...))` inside the DISTINCT, "Goldman Sachs", "goldman sachs" and
    a trailing space are three companies and the tile silently reports data-entry
    variety as market breadth.
    """
    app.dependency_overrides[get_current_db_user] = lambda: _ctx("view_only")
    session = _FakeSession([], scalars=[0] * 21)
    app.dependency_overrides[get_session] = _with_session(session)

    client.get("/dashboard/summary")
    sql = _scalar_sql_mentioning(session, "distinct(lower(trim(current_employment.current_employer")

    assert "count(distinct" in sql
    assert "lower(" in sql
    assert "trim(" in sql


def test_distinct_employers_excludes_the_same_placeholders_as_the_chart(client):
    """The KPI and the Top-employers panel under it must describe ONE set of
    companies. "unknown" / "n/a" / "none" / "graduate student" are not firms, the
    chart already refuses to rank them, and a tile that counted them would
    disagree with the panel directly beneath it — the parity bug class this file
    keeps getting bitten by."""
    app.dependency_overrides[get_current_db_user] = lambda: _ctx("view_only")
    session = _FakeSession([], scalars=[0] * 21)
    app.dependency_overrides[get_session] = _with_session(session)

    client.get("/dashboard/summary")
    sql = _scalar_sql_mentioning(session, "distinct(lower(trim(current_employment.current_employer")

    # The values are BOUND, not inlined, so the SQL string only shows the shape.
    assert "not in" in sql

    # The parity claim itself is structural: the query must use the very tuple
    # the chart uses, not a second copy of the same four strings that can drift.
    source = Path(dashboard_routes.__file__).read_text(encoding="utf-8")
    start = source.index("# DISTINCT COMPANIES")
    block = source[start : source.index("return {", start)]
    assert "_NON_EMPLOYER_VALUES" in block


def test_employer_states_counts_only_resolvable_us_states(client):
    """The Companies tile's sub-line: how many states those firms are in.

    ⚠️ THIS TILE SHIPPED READING "Across 70 states" (#754). There are 51 possible
    values. `lower(trim(...))` before DISTINCT — which is all it used to do —
    folds casing and whitespace and NOTHING ELSE, so "UT" and "Utah" counted
    twice, and nothing at all restricted the free-text column to the US, so
    "Ontario" counted as a state. The count must now go through the crosswalk
    expression, which yields NULL for anything that is not a US state and is
    therefore incapable of exceeding 51. (What the expression actually returns
    for real rows is pinned against a real database in tests/test_us_states.py —
    a compiled-SQL assertion like this one could never have caught the bug.)
    """
    app.dependency_overrides[get_current_db_user] = lambda: _ctx("view_only")
    session = _FakeSession([], scalars=[0] * 22)
    app.dependency_overrides[get_session] = _with_session(session)

    client.get("/dashboard/summary")
    sql = _scalar_sql_mentioning(session, "count(distinct(case when")

    assert "current_employment.current_state" in sql
    # The old, broken shape: a bare case/whitespace fold with no US restriction.
    assert "distinct(lower(trim(current_employment.current_state" not in sql
    # The state names are BOUND, not inlined, so assert on the count of branches
    # instead: one WHEN per code AND one per full name, for all 51.
    assert sql.count("when") == 102

    # Parity with the drill-down beneath the tile is structural, not a
    # coincidence of two authors matching: the handler must build the fold ONCE
    # and share the one object between the count, the by-state breakdown, and
    # the "is this alumnus abroad" test. A second call site is the drift.
    source = Path(dashboard_routes.__file__).read_text(encoding="utf-8")
    assert source.count("us_state_full_name_expr(CurrentEmployment.current_state)") == 1


def test_by_state_groups_on_the_folded_state_not_the_raw_column(client):
    """The panel under the tile reads the SAME free-text column, so it had the
    SAME bug: grouping on the raw value ranked "UT" and "Utah" as separate bars
    and let "Ontario" onto a list of states — and because the LIMIT 8 was applied
    to those raw groups, a real state could be pushed off the list by its own
    alternate spelling. Folding has to happen in the GROUP BY key, before the
    ranking, not after it."""
    app.dependency_overrides[get_current_db_user] = lambda: _ctx("view_only")
    session = _FakeSession([], scalars=[0] * 22)
    app.dependency_overrides[get_session] = _with_session(session)

    client.get("/dashboard/summary")
    # cohort, top_employers, industry_rows, by_state — by_state is the last.
    sql = _compiled(session.execute_args[3]).lower()

    assert "group by case when" in sql
    assert "group by current_employment.current_state" not in sql
    # Folded to NULL == not a US state == not a bar on a chart of states.
    assert "is not null" in sql
    assert "limit" in sql


def test_employer_countries_counts_only_alumni_outside_the_us(client):
    """The second half of the sub-line (#754).

    Two things make the number mean what the label says. It is keyed off the
    STATE not resolving (the same expression, yielding NULL) — so the states and
    countries halves partition the population instead of double-counting an
    alumnus. And US spellings are excluded, so a domestic record with a junk
    state cannot make "United States" the 1 in "and 1 country"."""
    app.dependency_overrides[get_current_db_user] = lambda: _ctx("view_only")
    session = _FakeSession([], scalars=[0] * 22)
    app.dependency_overrides[get_session] = _with_session(session)

    client.get("/dashboard/summary")
    sql = _scalar_sql_mentioning(
        session, "count(distinct(upper(trim(current_employment.current_country"
    )

    # Only alumni whose work state is NOT a US state.
    assert "case when" in sql
    assert "is null" in sql
    # US spellings excluded (values are bound, so only the shape shows).
    assert "not in" in sql
