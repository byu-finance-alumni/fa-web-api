"""Semester grouping, filtering and the export picker for the Links tab (#876).

What is pinned here:

  * the ``semester`` query parameter is strict (422 on garbage, out-of-range
    years, too many values) on BOTH the list and the export, and normalised the
    same way on both;
  * it compiles to an OR of half-open ``submitted_at`` ranges in SQL — not a
    Python-side filter — and ANDs with the date-received range;
  * no semesters means no predicate (today's behaviour, unchanged);
  * every read carries its semester, and the CSV has a Semester column;
  * the page envelope's ``previous_semester`` lets headings continue across a
    page boundary;
  * the semester facet (``GET /opportunity-links/semesters``) comes from the
    data, ignores the semester selection itself, and keeps the list's status
    gate.

Offline: auth, permissions and the session are overridden. No DATABASE_URL.
"""

from __future__ import annotations

import asyncio
import datetime
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.dialects import postgresql

from app.api.dependencies.auth import get_current_db_user
from app.core.database import get_session
from app.core.semesters import MAX_SEMESTERS, Semester
from app.main import app
from app.models.opportunity_link import OpportunityLink
from app.schemas.auth import UserContext
from app.schemas.opportunity_link import (
    OpportunityLinkFilters,
    OpportunityLinkPage,
    OpportunityLinkRead,
    OpportunityLinkSemesters,
)
from app.services import opportunity_links as service

UTC = datetime.UTC


def _ctx(*roles: str) -> UserContext:
    return UserContext(
        user_id=7,
        auth_user_id=uuid.UUID("11111111-1111-1111-1111-111111111111"),
        roles=list(roles) or ["full_access"],
    )


@pytest.fixture
def client():
    async def _no_db_session():
        yield None

    app.dependency_overrides[get_session] = _no_db_session
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


def _as(role: str) -> None:
    app.dependency_overrides[get_current_db_user] = lambda: _ctx(role)


def _capture(monkeypatch) -> dict:
    seen: dict = {}

    async def _list(session, filters, **kwargs):
        seen["list"] = filters
        return OpportunityLinkPage(items=[], total=0, limit=50, offset=0)

    async def _count(session, filters):
        seen["count"] = filters
        return 0

    async def _export(session, filters, **kwargs):
        seen["export"] = filters
        return "Link ID\r\n"

    async def _semesters(session, filters):
        seen["semesters"] = filters
        return OpportunityLinkSemesters(items=[])

    monkeypatch.setattr(service, "list_links", _list)
    monkeypatch.setattr(service, "count_links", _count)
    monkeypatch.setattr(service, "export_csv", _export)
    monkeypatch.setattr(service, "list_semesters", _semesters)
    return seen


def _compiled(stmt):
    compiled = stmt.compile(dialect=postgresql.dialect())
    return str(compiled), dict(compiled.params)


def _where(stmt) -> str:
    sql, _ = _compiled(stmt)
    return sql.split("WHERE", 1)[1] if "WHERE" in sql else ""


def _run(coro):
    return asyncio.run(coro)


# =============================================================================
# 1. The query parameter
# =============================================================================


def test_semesters_reach_the_list_and_the_export_identically(monkeypatch, client):
    seen = _capture(monkeypatch)
    _as("full_access")
    qs = "semester=Winter-2026&semester=Fall-2026&semester=Winter-2026"
    assert client.get(f"/opportunity-links?{qs}").status_code == 200
    assert client.get(f"/opportunity-links/export?{qs}").status_code == 200
    # De-duplicated and newest first, on both sides.
    assert seen["list"].semesters == ("Fall-2026", "Winter-2026")
    assert seen["export"] == seen["list"]
    assert seen["count"] == seen["list"]


def test_no_semester_means_no_restriction(monkeypatch, client):
    seen = _capture(monkeypatch)
    _as("full_access")
    client.get("/opportunity-links")
    client.get("/opportunity-links/export")
    assert seen["list"].semesters == ()
    assert seen["export"].semesters == ()


@pytest.mark.parametrize(
    "bad",
    [
        "semester=fall-2026",
        "semester=Fall%202026",
        "semester=Fall-26",
        "semester=Autumn-2026",
        "semester=Fall-1999",
        "semester=Fall-2101",
        "semester=",
        "semester=Fall-2026%0A",
        "semester=Fall-2026&semester=nonsense",
    ],
)
def test_garbage_semesters_are_a_422_on_both_endpoints(monkeypatch, client, bad):
    _capture(monkeypatch)
    _as("full_access")
    assert client.get(f"/opportunity-links?{bad}").status_code == 422
    assert client.get(f"/opportunity-links/export?{bad}").status_code == 422


def test_too_many_semesters_is_a_422(monkeypatch, client):
    _capture(monkeypatch)
    _as("full_access")
    many = "&".join(
        f"semester={t}-{y}"
        for y in range(2020, 2025)
        for t in ("Winter", "Spring", "Summer", "Fall")
    )
    assert many.count("semester=") > MAX_SEMESTERS
    assert client.get(f"/opportunity-links?{many}").status_code == 422
    assert client.get(f"/opportunity-links/export?{many}").status_code == 422


def test_the_status_gate_is_unchanged_with_semesters(monkeypatch, client):
    _capture(monkeypatch)
    _as("view_only")
    assert client.get("/opportunity-links?semester=Fall-2026").status_code == 200
    assert (
        client.get("/opportunity-links/export?semester=Fall-2026&status=pending").status_code
        == 403
    )


# =============================================================================
# 2. The SQL
# =============================================================================


def test_semesters_compile_to_half_open_submitted_at_ranges():
    filters = OpportunityLinkFilters(
        status="approved", semesters=("Fall-2026", "Spring-2026")
    )
    stmt = service.build_population_query(filters)
    where = _where(stmt)
    _, params = _compiled(stmt)
    assert "opportunity_links.submitted_at >=" in where
    assert "opportunity_links.submitted_at <" in where
    assert " OR " in where
    # No per-row time-zone function in the predicate: the bounds are constants.
    assert "timezone" not in where.lower()
    bounds = {v for v in params.values() if isinstance(v, datetime.datetime)}
    assert set(Semester("Fall", 2026).bounds()) <= bounds
    assert set(Semester("Spring", 2026).bounds()) <= bounds


def test_semesters_and_the_date_range_combine_with_and():
    filters = OpportunityLinkFilters(
        status="approved",
        submitted_from=datetime.date(2026, 10, 1),
        submitted_to=datetime.date(2026, 10, 31),
        semesters=("Fall-2026",),
    )
    _, params = _compiled(service.build_population_query(filters))
    bounds = sorted(v for v in params.values() if isinstance(v, datetime.datetime))
    # Two date-range bounds + two semester bounds, all applied.
    assert len(bounds) == 4
    assert datetime.datetime(2026, 10, 1, tzinfo=UTC) in bounds
    assert Semester("Fall", 2026).bounds()[0] in bounds


def test_an_empty_selection_adds_no_predicate():
    plain = OpportunityLinkFilters(status="approved")
    assert _compiled(service.build_population_query(plain)) == _compiled(
        service.build_population_query(plain.model_copy(update={"semesters": ()}))
    )
    assert "submitted_at" not in _where(service.build_population_query(plain))


def test_the_count_carries_the_semester_predicate_too():
    filters = OpportunityLinkFilters(status="approved", semesters=("Fall-2026",))
    _, list_params = _compiled(service.build_population_query(filters))
    _, count_params = _compiled(service.build_population_count(filters))
    assert list_params == count_params


# =============================================================================
# 3. What each link carries
# =============================================================================


def _read(submitted_at: datetime.datetime) -> OpportunityLinkRead:
    return OpportunityLinkRead(
        opportunity_link_id=1,
        alumni_id=1,
        is_own_company=False,
        company_name="Acme",
        url="https://acme.example/jobs",
        role_type="internship",
        status="approved",
        source="staff",
        submitted_at=submitted_at,
    )


def test_every_read_carries_its_semester():
    link = _read(datetime.datetime(2026, 10, 21, 18, 0, tzinfo=UTC))
    assert link.semester == "Fall-2026"
    assert link.semester_label == "Fall 2026"
    dumped = link.model_dump()
    assert dumped["semester"] == "Fall-2026"
    assert dumped["semester_label"] == "Fall 2026"


def test_the_semester_is_read_on_the_mountain_clock():
    # 02:00 UTC on 1 Sep = 8pm MDT on 31 Aug: still Summer.
    assert _read(datetime.datetime(2026, 9, 1, 2, 0, tzinfo=UTC)).semester == "Summer-2026"


def test_the_response_schema_exposes_semester():
    schema = app.openapi()["components"]["schemas"]
    read = schema.get("OpportunityLinkRead-Output") or schema["OpportunityLinkRead"]
    assert "semester" in read["properties"]
    assert "semester_label" in read["properties"]
    page = schema.get("OpportunityLinkPage-Output") or schema["OpportunityLinkPage"]
    assert "previous_semester" in page["properties"]


class _ExportSession:
    def __init__(self, rows):
        self._rows = rows
        self.added: list = []

    async def execute(self, stmt):
        rows = self._rows

        class _R:
            def scalars(self):
                return self

            def all(self):
                return list(rows)

        return _R()

    def add(self, obj):
        self.added.append(obj)

    async def commit(self):
        pass


def test_the_csv_has_a_semester_column(monkeypatch):
    assert "Semester" in service.EXPORT_COLUMNS
    idx = service.EXPORT_COLUMNS.index("Semester")
    item = _read(datetime.datetime(2027, 1, 1, 6, 0, tzinfo=UTC))  # 11pm MST 31 Dec

    async def _project(_session, links, *, redact_archived=False):
        return [item]

    async def _emails(_session, alumni_ids, *, redact_archived=False):
        return {}

    monkeypatch.setattr(service, "_project", _project)
    monkeypatch.setattr(service, "_submitter_emails", _emails)
    csv_text = _run(
        service.export_csv(
            _ExportSession([object()]),
            OpportunityLinkFilters(status="approved"),
            actor_user_id=7,
        )
    )
    import csv
    import io

    rows = list(csv.reader(io.StringIO(csv_text)))
    assert rows[0][idx] == "Semester"
    assert rows[1][idx] == "Fall 2026"


def test_the_export_audit_records_the_semesters():
    text = service.describe_filters(
        OpportunityLinkFilters(status="approved", semesters=("Fall-2026", "Winter-2026"))
    )
    assert "semester=Fall-2026,Winter-2026" in text
    assert "semester" not in service.describe_filters(OpportunityLinkFilters(status="approved"))


# =============================================================================
# 4. Headings across a page boundary
# =============================================================================


class _PageSession:
    """Answers the list's count, page and previous-row probe."""

    def __init__(self, *, rows, previous_at, total):
        self._rows = rows
        self._previous_at = previous_at
        self._total = total
        self.scalars_asked: list = []

    async def scalar(self, stmt):
        self.scalars_asked.append(stmt)
        name = stmt.column_descriptions[0].get("name")
        if name == "submitted_at":
            return self._previous_at
        return self._total

    async def execute(self, stmt):
        rows = self._rows if stmt.column_descriptions[0]["entity"] is OpportunityLink else []

        class _R:
            def scalars(self):
                return self

            def all(self):
                return list(rows)

        return _R()


def _link_row(submitted_at):
    return OpportunityLink(
        opportunity_link_id=5,
        alumni_id=1,
        is_own_company=False,
        company_name="Acme",
        url="https://acme.example/jobs",
        role_type="internship",
        status="approved",
        source="staff",
        submitted_at=submitted_at,
    )


def test_a_later_page_reports_the_semester_it_continues():
    async def _body():
        session = _PageSession(
            rows=[_link_row(datetime.datetime(2026, 9, 3, tzinfo=UTC))],
            previous_at=datetime.datetime(2026, 9, 20, tzinfo=UTC),
            total=120,
        )
        page = await service.list_links(
            session, OpportunityLinkFilters(status="approved"), limit=50, offset=50
        )
        assert page.previous_semester == "Fall-2026"
        assert page.items[0].semester == "Fall-2026"
        probe = session.scalars_asked[-1]
        sql, params = _compiled(probe)
        # One row back, in the list's own order, over the same population.
        assert "ORDER BY opportunity_links.submitted_at DESC" in sql
        assert 49 in params.values() and 1 in params.values()

    _run(_body())


def test_the_first_page_has_no_previous_semester():
    async def _body():
        session = _PageSession(
            rows=[_link_row(datetime.datetime(2026, 9, 3, tzinfo=UTC))],
            previous_at=datetime.datetime(2026, 9, 20, tzinfo=UTC),
            total=1,
        )
        page = await service.list_links(session, OpportunityLinkFilters(status="approved"))
        assert page.previous_semester is None
        # No probe is made on page one: only the count.
        assert len(session.scalars_asked) == 1

    _run(_body())


# =============================================================================
# 5. The semester facet (the checkboxes)
# =============================================================================


def test_the_facet_ignores_the_semester_selection_but_keeps_every_other_filter():
    filters = OpportunityLinkFilters(
        status="pending",
        role_type="internship",
        semesters=("Fall-2026",),
    )
    where = _where(service.build_semester_facet(filters))
    assert "opportunity_links.status" in where
    assert "opportunity_links.role_type" in where
    assert "submitted_at <" not in where
    sql, params = _compiled(service.build_semester_facet(filters))
    # Postgres converts to Mountain time and aggregates; Python only folds months.
    assert "timezone(" in sql.lower()
    assert "America/Denver" in params.values()
    assert "GROUP BY" in sql


class _FacetSession:
    def __init__(self, rows):
        self._rows = rows

    async def execute(self, stmt):
        rows = self._rows

        class _R:
            def all(self):
                return list(rows)

        return _R()


def test_the_facet_folds_months_into_terms_newest_first():
    async def _body():
        rows = [
            (2026, 9, 3), (2026, 10, 4),   # Fall 2026 = 7
            (2026, 5, 1), (2026, 6, 2),    # Spring 2026 = 3
            (2027, 1, 5),                  # Winter 2027 = 5
            (2026.0, 4.0, 2),              # Winter 2026 = 2 (Postgres numerics)
        ]
        result = await service.list_semesters(
            _FacetSession(rows), OpportunityLinkFilters(status="approved")
        )
        assert [(i.semester, i.label, i.count) for i in result.items] == [
            ("Winter-2027", "Winter 2027", 5),
            ("Fall-2026", "Fall 2026", 7),
            ("Spring-2026", "Spring 2026", 3),
            ("Winter-2026", "Winter 2026", 2),
        ]

    _run(_body())


def test_the_facet_route_keeps_the_list_status_gate(monkeypatch, client):
    seen = _capture(monkeypatch)
    _as("view_only")
    assert client.get("/opportunity-links/semesters").status_code == 200
    assert seen["semesters"].status == "approved"
    assert client.get("/opportunity-links/semesters?status=pending").status_code == 403
    _as("full_access")
    assert client.get("/opportunity-links/semesters?status=pending").status_code == 200
    assert seen["semesters"].status == "pending"


def test_the_facet_route_takes_the_list_filters_minus_semester_and_paging():
    spec = app.openapi()
    names = lambda path: {p["name"] for p in spec["paths"][path]["get"]["parameters"]}  # noqa: E731
    assert names("/opportunity-links/semesters") == names("/opportunity-links") - {
        "limit",
        "offset",
        "semester",
    }


def test_the_facet_route_requires_a_login(client):
    app.dependency_overrides.pop(get_current_db_user, None)
    assert client.get("/opportunity-links/semesters").status_code in (401, 403)
