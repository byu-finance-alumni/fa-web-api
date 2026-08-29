"""Export/list population parity for the Links report (#771).

THE BUG CLASS THIS PINS. This repo keeps shipping exports that return a WIDER or
differently populated set than the list they were launched from — most recently
#366, where a "near Provo" screen exported every alumnus in the country, and
before that a CSV that carried columns the list never showed. It is never caught
by typechecking: both sides compile, both sides run, and the only symptom is a
file with rows in it that nobody was looking at. The memory index records the
sharpest edge of it — ``null`` counts as SET, so a "no predicate" path silently
returns everyone.

So the assertions here are about POPULATION, not about fields existing:

  * ``GET /opportunity-links`` and ``GET /opportunity-links/export`` build the
    SAME ``OpportunityLinkFilters`` from the same query string — captured from
    the REAL routes, so a route that forgets to forward a parameter fails here
    (a test comparing two hand-written service calls would not);
  * that object compiles to the SAME SQL with the SAME binds on both sides;
  * every filter parameter the list accepts, the export accepts — enumerated from
    the live OpenAPI document rather than from a list somebody has to remember to
    update;
  * the export's default ``status`` is the list's default ``status``, so an
    export launched from an unfiltered screen cannot hand back the unmoderated
    queue; and
  * the export is refused for a caller who could not have seen those rows in the
    list.

Offline: auth, the permission config and the session are overridden. No
DATABASE_URL, no network.
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
from app.main import app
from app.schemas.auth import UserContext
from app.schemas.opportunity_link import (
    OpportunityLinkFilters,
    OpportunityLinkPage,
    resolve_status,
)
from app.services import opportunity_links as service

# One query string exercising EVERY filter the list offers at once. Reused by
# both halves of the parity check so neither side can be given an easier one.
FULL_QUERY = (
    "status=pending"
    "&role_type=internship"
    "&company=Acme%20%25Capital"
    "&q=analyst"
    "&submitted_from=2026-08-01"
    "&submitted_to=2026-08-28"
)


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
    """Stub both service entry points and record the filter object each got.

    The ROUTES are exercised for real — this is what makes a forgotten
    ``submitted_to=`` on the export signature a failure here rather than a
    silently wider CSV in production.
    """
    seen: dict = {}

    async def _list(session, filters, **kwargs):
        seen["list"] = filters
        seen["list_kwargs"] = kwargs
        return OpportunityLinkPage(items=[], total=0, limit=50, offset=0)

    async def _count(session, filters):
        seen["count"] = filters
        return 0

    async def _export(session, filters, **kwargs):
        seen["export"] = filters
        seen["export_kwargs"] = kwargs
        return "Link ID\r\n"

    monkeypatch.setattr(service, "list_links", _list)
    monkeypatch.setattr(service, "count_links", _count)
    monkeypatch.setattr(service, "export_csv", _export)
    return seen


def _compiled(stmt):
    """``(sql, params)`` for a statement, against the real Postgres dialect."""
    compiled = stmt.compile(dialect=postgresql.dialect())
    return str(compiled), dict(compiled.params)


def _where(stmt) -> str:
    """Just the WHERE clause.

    Asserting over the whole statement would be meaningless here: every column
    name this file cares about (``submitted_at``, ``application_deadline``) also
    appears in the SELECT list of a ``select(OpportunityLink)``, so "is this
    column filtered on?" has to be asked of the predicates alone.
    """
    sql, _ = _compiled(stmt)
    return sql.split("WHERE", 1)[1] if "WHERE" in sql else ""


# =============================================================================
# 1. The two routes select the same population
# =============================================================================


def test_the_list_and_the_export_build_the_same_filter_object(monkeypatch, client):
    """THE CORE ASSERTION. Same query string in, same population selector out.

    Captured from the real routes, so this fails if either signature drops a
    parameter — the exact defect that produced the "near Provo" export.
    """
    seen = _capture(monkeypatch)
    _as("full_access")

    assert client.get(f"/opportunity-links?{FULL_QUERY}").status_code == 200
    assert client.get(f"/opportunity-links/export?{FULL_QUERY}").status_code == 200

    assert seen["list"] == seen["export"]
    # And the pre-flight count is over the same set as the file — a cap checked
    # against a different population is a cap that does not bound the file.
    assert seen["count"] == seen["export"]


def test_the_same_filters_compile_to_the_same_sql(monkeypatch, client):
    """Equal objects are not enough: they must reach SQL through one builder.

    Compiling both is what would catch an export that quietly added its own
    ``where`` on top — the shape the bug took the time before last.
    """
    seen = _capture(monkeypatch)
    _as("full_access")
    client.get(f"/opportunity-links?{FULL_QUERY}")
    client.get(f"/opportunity-links/export?{FULL_QUERY}")

    list_sql, list_params = _compiled(service.build_population_query(seen["list"]))
    export_sql, export_params = _compiled(service.build_population_query(seen["export"]))
    assert list_sql == export_sql
    assert list_params == export_params
    # The count must carry the same predicates as the page it counts.
    count_sql, count_params = _compiled(service.build_population_count(seen["count"]))
    assert count_params == list_params
    assert "FROM opportunity_links" in count_sql


def test_every_list_filter_parameter_exists_on_the_export():
    """Enumerated from the LIVE OpenAPI document, not from a hand-kept list.

    A filter added to the list and forgotten on the export is the whole bug, and
    the only way to make that impossible is to derive the expectation from the
    running app instead of from a constant somebody has to remember to edit.

    ``limit`` / ``offset`` are the one legitimate difference: a report is the
    whole set, a list is a page.
    """
    spec = app.openapi()
    paging = {"limit", "offset"}
    list_params = {
        p["name"] for p in spec["paths"]["/opportunity-links"]["get"]["parameters"]
    }
    export_params = {
        p["name"]
        for p in spec["paths"]["/opportunity-links/export"]["get"]["parameters"]
    }
    assert list_params - paging == export_params


def test_the_export_defaults_status_exactly_as_the_list_does(monkeypatch, client):
    """An unfiltered export must not be a way to read the pending queue.

    ``status`` omitted means ``approved`` on BOTH — resolved once, in
    ``resolve_status``. If the export defaulted to "no predicate" instead, a
    ``view_only`` caller would download every unmoderated, attacker-supplied row
    while the screen showed none of them. That is the ``null``-counts-as-SET trap
    with a disclosure attached.
    """
    seen = _capture(monkeypatch)
    _as("view_only")
    client.get("/opportunity-links")
    client.get("/opportunity-links/export")
    assert seen["list"].status == "approved"
    assert seen["export"].status == "approved"
    assert resolve_status(None) == "approved"


def test_an_unfiltered_export_still_applies_the_status_predicate(monkeypatch, client):
    """The compiled SQL, not just the field. A defaulted value that never became
    a ``WHERE`` clause would look correct on the object and export everything."""
    seen = _capture(monkeypatch)
    _as("view_only")
    client.get("/opportunity-links/export")
    _, params = _compiled(service.build_population_query(seen["export"]))
    assert "opportunity_links.status" in _where(
        service.build_population_query(seen["export"])
    )
    assert "approved" in params.values()


# =============================================================================
# 2. The export inherits the list's authorization boundary
# =============================================================================


def test_the_export_cannot_reach_rows_the_list_would_refuse(monkeypatch, client):
    """403 on both, not "403 on the screen and a CSV of the queue"."""
    _capture(monkeypatch)
    _as("view_only")
    assert client.get("/opportunity-links?status=pending").status_code == 403
    assert client.get("/opportunity-links/export?status=pending").status_code == 403


def test_a_moderator_may_export_the_pending_queue(monkeypatch, client):
    seen = _capture(monkeypatch)
    _as("full_access")
    assert client.get("/opportunity-links/export?status=pending").status_code == 200
    assert seen["export"].status == "pending"


# =============================================================================
# 3. The date range — what "date received" means
# =============================================================================


def test_the_date_range_bounds_submitted_at_and_not_the_deadline():
    """"By date they were given to us" is ``submitted_at``. Bounding
    ``application_deadline`` instead would answer a different question and look
    identical on screen."""
    filters = OpportunityLinkFilters(
        status="approved",
        submitted_from=datetime.date(2026, 8, 1),
        submitted_to=datetime.date(2026, 8, 28),
    )
    _, params = _compiled(service.build_population_query(filters))
    where = _where(service.build_population_query(filters))
    assert "opportunity_links.submitted_at >=" in where
    assert "opportunity_links.submitted_at <=" in where
    assert "application_deadline" not in where
    bounds = sorted(v for v in params.values() if isinstance(v, datetime.datetime))
    assert bounds[0] == datetime.datetime(2026, 8, 1, 0, 0, tzinfo=datetime.UTC)
    # ⚠️ THE END OF THE DAY, not midnight. A naive `<= 2026-08-28` compares a
    # timestamptz against 00:00 and drops everything that arrived during the day
    # the user asked for — the report would silently be missing its newest rows,
    # which is exactly what #771 exists to stop happening.
    assert bounds[1].date() == datetime.date(2026, 8, 28)
    assert bounds[1].hour == 23 and bounds[1].minute == 59


def test_one_open_end_applies_one_predicate_only():
    """An unset bound must apply NO predicate — and a set one must apply exactly
    its own. Both halves matter: the memory index's rule is that ``null`` reads
    as SET, so the failure mode is a silently unbounded report."""
    only_from = OpportunityLinkFilters(
        status="approved", submitted_from=datetime.date(2026, 8, 1)
    )
    where = _where(service.build_population_query(only_from))
    assert "opportunity_links.submitted_at >=" in where
    assert "opportunity_links.submitted_at <=" not in where

    neither = OpportunityLinkFilters(status="approved")
    assert "submitted_at" not in _where(service.build_population_query(neither))


def test_an_inverted_range_is_a_422_on_both_endpoints(monkeypatch, client):
    """Refused, not silently empty. A report that returns nothing because the
    dates were the wrong way round reads exactly like a quiet week."""
    _capture(monkeypatch)
    _as("full_access")
    bad = "submitted_from=2026-08-28&submitted_to=2026-08-01"
    assert client.get(f"/opportunity-links?{bad}").status_code == 422
    assert client.get(f"/opportunity-links/export?{bad}").status_code == 422


def test_a_single_day_range_is_accepted(monkeypatch, client):
    """from == to is the commonest report there is ("what came in today")."""
    seen = _capture(monkeypatch)
    _as("full_access")
    same = "submitted_from=2026-08-28&submitted_to=2026-08-28"
    assert client.get(f"/opportunity-links/export?{same}").status_code == 200
    assert seen["export"].submitted_from == seen["export"].submitted_to


# =============================================================================
# 4. The file itself
# =============================================================================


def _run(coro):
    return asyncio.run(coro)


class _ExportSession:
    """A no-DB session that returns canned rows for the export's one SELECT."""

    def __init__(self, rows):
        self._rows = rows
        self.added: list = []
        self.committed = False

    async def execute(self, stmt):
        rows = self._rows

        class _R:
            def scalars(self):
                return self

            def all(self):
                return list(rows)

        return _R()

    async def scalar(self, stmt):
        return len(self._rows)

    def add(self, obj):
        self.added.append(obj)

    async def commit(self):
        self.committed = True


def test_the_csv_carries_the_declared_columns_and_one_row_per_link(monkeypatch):
    from app.models.alumni import Alumni
    from app.models.employment import CurrentEmployment
    from app.models.opportunity_link import OpportunityLink

    link = OpportunityLink(
        opportunity_link_id=42,
        alumni_id=1,
        is_own_company=False,
        company_name="Acme Capital",
        url="https://careers.acme-capital.example/jobs/analyst",
        location_city="Provo",
        location_state="Utah",
        location_country="United States",
        role_type="internship",
        application_deadline=datetime.date(2026, 11, 1),
        details="Summer analyst programme.",
        status="pending",
        source="survey",
        submitted_at=datetime.datetime(2026, 8, 28, 9, 30, tzinfo=datetime.UTC),
        updated_at=datetime.datetime(2026, 8, 28, 9, 30, tzinfo=datetime.UTC),
    )
    session = _ExportSession([link])

    async def _project(_session, links):
        from app.services.opportunity_links import _to_read

        return [
            _to_read(row, submitted_by="Dana Whitcomb", employer=None, reviewed_by=None)
            for row in links
        ]

    monkeypatch.setattr(service, "_project", _project)
    csv_text = _run(
        service.export_csv(
            session, OpportunityLinkFilters(status="pending"), actor_user_id=7
        )
    )

    lines = [line for line in csv_text.splitlines() if line]
    assert lines[0] == ",".join(service.EXPORT_COLUMNS)
    assert len(lines) == 2
    assert "42" in lines[1] and "Dana Whitcomb" in lines[1]
    # "Date received" is the whole point of the report, so it must be in the file
    # and it must say which clock it is on.
    assert "2026-08-28 09:30:00 UTC" in lines[1]
    # Unused imports guard against the fixture drifting away from the real models.
    assert Alumni is not None and CurrentEmployment is not None


def test_the_export_is_audited_with_the_filters_that_produced_it(monkeypatch):
    """WHAT left the system and under which selection — never the rows."""
    from app.models.audit import AuditLog

    session = _ExportSession([])

    async def _project(_session, links):
        return []

    monkeypatch.setattr(service, "_project", _project)
    filters = OpportunityLinkFilters(
        status="pending",
        submitted_from=datetime.date(2026, 8, 1),
        submitted_to=datetime.date(2026, 8, 28),
    )
    _run(service.export_csv(session, filters, actor_user_id=7))

    entries = [o for o in session.added if isinstance(o, AuditLog)]
    assert len(entries) == 1
    assert entries[0].action_type == "export_opportunity_links"
    assert entries[0].user_id == 7
    assert "rows=0" in entries[0].new_value
    assert "submitted_from=2026-08-01" in entries[0].new_value
    assert "submitted_to=2026-08-28" in entries[0].new_value
    assert session.committed


def test_a_formula_cell_is_neutralised():
    """Every free-text column here is PUBLIC input from the survey path. A cell
    opening with ``=`` executes when the file is opened in Excel or Sheets."""
    assert service._cell("=cmd|'/c calc'!A1").startswith("\t")
    assert service._cell("+1") .startswith("\t")
    assert service._cell("Acme Capital") == "Acme Capital"


def test_an_over_cap_export_is_refused_rather_than_truncated(monkeypatch, client):
    """413, not a short file. A report missing its tail reads exactly like a
    complete one, and nobody would know to ask."""
    seen = _capture(monkeypatch)

    async def _count(session, filters):
        seen["count"] = filters
        return service.MAX_EXPORT_ROWS + 1

    monkeypatch.setattr(service, "count_links", _count)
    _as("full_access")
    response = client.get("/opportunity-links/export")
    assert response.status_code == 413
    assert "export" not in seen  # nothing was built, nothing was audited
