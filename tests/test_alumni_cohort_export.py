"""Offline tests for the cohort round-trip export (a FILLED update template).

Staff pick a graduation year and download that cohort as a CSV whose columns
EXACTLY match the import template, so it re-uploads cleanly through
``POST /alumni/import/update``. This exercises
``import_csv.build_cohort_update_csv`` with a small fake session (no Postgres),
mirroring the fake-session style of ``tests/test_alumni_import.py``.

The fake session serves the cohort count (``scalar``), the alumni list and each
1:1 side table (``execute``, routed by table name in the compiled SQL), and
captures the disclosure-audit ``add`` / ``commit``.
"""

import asyncio
import csv as _csv
import io

from sqlalchemy.dialects import postgresql

from app.models.alumni import Alumni
from app.models.contact import AlumniContactInfo
from app.models.employment import CurrentEmployment
from app.models.engagement import AlumniProgramEngagement
from app.services import alumni_export, import_csv

# --- Fake session ------------------------------------------------------------


class _Scalars:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return list(self._rows)


class _ExecResult:
    def __init__(self, rows):
        self._rows = rows

    def scalars(self):
        return _Scalars(self._rows)


class FakeExportSession:
    """Serves the cohort count + alumni list + each side table, captures audit.

    ``scalar`` (the ``SELECT count(*)`` over the filtered subquery) returns
    ``total`` — set it above the cap to exercise the over-cap error. ``execute``
    is routed by the side table's name appearing in the compiled SQL; the plain
    grad-year alumni query references no side table, so it falls through to the
    alumni list.
    """

    def __init__(
        self, alumni, *, contact=None, career=None, education=None,
        engagement=None, total=None,
    ):
        self._alumni = list(alumni)
        self._sections = {
            "alumni_contact_info": list(contact or []),
            "current_employment": list(career or []),
            "education_history": list(education or []),
            "alumni_program_engagement": list(engagement or []),
        }
        self._total = len(self._alumni) if total is None else total
        self.added = []
        self.committed = 0

    async def scalar(self, stmt):
        return self._total

    async def execute(self, stmt):
        sql = str(stmt.compile(dialect=postgresql.dialect()))
        for fragment, rows in self._sections.items():
            if fragment in sql:
                return _ExecResult(rows)
        return _ExecResult(self._alumni)

    def add(self, obj):
        self.added.append(obj)

    async def commit(self):
        self.committed += 1


def _run(coro):
    return asyncio.run(coro)


def _parse(text):
    reader = _csv.reader(io.StringIO(text))
    rows = list(reader)
    return rows[0], rows[1:]


def _cell(header_row, data_row, header):
    return data_row[header_row.index(header)]


# --- (a) headers == EXPECTED_HEADERS, one row per matching alumnus -----------


def test_cohort_export_headers_and_one_row_each():
    alumni = [
        Alumni(alumni_id=1, byu_id="123456789", net_id="jdoe",
               first_name="Jane", last_name="Doe", graduation_year=2018),
        Alumni(alumni_id=2, byu_id="987654321", net_id="jsmith",
               first_name="John", last_name="Smith", graduation_year=2018),
    ]
    session = FakeExportSession(alumni)
    text = _run(import_csv.build_cohort_update_csv(session, 2018, actor_user_id=7))

    header_row, data_rows = _parse(text)
    assert header_row == import_csv.EXPECTED_HEADERS
    assert len(data_rows) == 2
    # Disclosure audit written + committed.
    assert session.committed == 1
    assert len(session.added) == 1
    audit = session.added[0]
    assert audit.action_type == "export_alumni"
    assert "grad_year=2018" in audit.new_value
    assert "rows=2" in audit.new_value


# --- (b) match keys + section fields populate from the loaded records ---------


def test_cohort_export_populates_keys_and_section_fields():
    alumni = [
        Alumni(alumni_id=1, byu_id="123456789", net_id="jdoe",
               first_name="Jane", last_name="Doe", graduation_year=2018,
               deceased=False),
    ]
    session = FakeExportSession(
        alumni,
        contact=[AlumniContactInfo(alumni_id=1, personal_email="jane@x.com",
                                   phone="801-555-0100")],
        # "Current city" is the EMPLOYER's city (#287), so it round-trips out of
        # the career row — the same column the importer now binds there.
        career=[CurrentEmployment(alumni_id=1, current_employment_id=5,
                                  current_employer="Acme Corp",
                                  current_title="Analyst",
                                  current_city="Provo")],
        engagement=[AlumniProgramEngagement(alumni_id=1, mentor_willing=True)],
    )
    text = _run(import_csv.build_cohort_update_csv(session, 2018, actor_user_id=7))
    header_row, data_rows = _parse(text)
    row = data_rows[0]

    # Re-import match keys (core) MUST be populated.
    assert _cell(header_row, row, "BYU ID (9 digits)") == "123456789"
    assert _cell(header_row, row, "Net ID") == "jdoe"
    # Core name/year.
    assert _cell(header_row, row, "First name") == "Jane"
    assert _cell(header_row, row, "Graduation Year") == "2018"
    # Section fields pulled from the loaded side rows.
    assert _cell(header_row, row, "Personal Email") == "jane@x.com"
    assert _cell(header_row, row, "Current employer") == "Acme Corp"
    assert _cell(header_row, row, "Current title") == "Analyst"
    assert _cell(header_row, row, "Current city") == "Provo"
    # Bool -> Yes/No.
    assert _cell(header_row, row, "Willing to mentor (Yes/No)") == "Yes"
    assert _cell(header_row, row, "Deceased? (Yes/No)") == "No"
    # A leadership column (multi-row history, no clean source) is left blank.
    assert _cell(header_row, row, "Finance Leadership Position") == ""


# --- (b2) an alumnus with no side rows leaves those cells blank ---------------


def test_cohort_export_blank_when_no_section_row():
    alumni = [Alumni(alumni_id=1, byu_id="123456789", first_name="Jane",
                     last_name="Doe", graduation_year=2018)]
    session = FakeExportSession(alumni)  # no contact/career/engagement rows
    text = _run(import_csv.build_cohort_update_csv(session, 2018, actor_user_id=7))
    header_row, data_rows = _parse(text)
    row = data_rows[0]
    assert _cell(header_row, row, "Personal Email") == ""
    assert _cell(header_row, row, "Current employer") == ""


# --- (c) round-trip: exported cells re-parse to the same values --------------


def test_cohort_export_round_trips_through_import_parser():
    alumni = [
        Alumni(alumni_id=1, byu_id="123456789", net_id="jdoe",
               first_name="Jane", last_name="Doe", graduation_year=2018,
               graduate_degree="MBA", graduate_school="Harvard Business School",
               graduate_graduation_year=2022),
    ]
    session = FakeExportSession(
        alumni,
        contact=[AlumniContactInfo(alumni_id=1, personal_email="jane@x.com")],
        career=[CurrentEmployment(alumni_id=1, current_employment_id=5,
                                  current_employer="Acme Corp")],
        engagement=[AlumniProgramEngagement(alumni_id=1, mentor_willing=True)],
    )
    text = _run(import_csv.build_cohort_update_csv(session, 2018, actor_user_id=7))

    # Feed the exported CSV straight back through the import parser/mapper.
    rows, header_errors = import_csv.parse_and_map(text.encode("utf-8"))
    assert header_errors == []
    assert len(rows) == 1
    payload = rows[0]["payload"]
    assert rows[0]["error"] is None

    # Core keys + values round-trip.
    assert payload["byu_id"] == "123456789"
    assert payload["net_id"] == "jdoe"
    assert payload["first_name"] == "Jane"
    assert payload["graduation_year"] == 2018  # int coerced back
    # Graduate program columns (the newly-added template fields) round-trip.
    assert payload["graduate_degree"] == "MBA"
    assert payload["graduate_school"] == "Harvard Business School"
    assert payload["graduate_graduation_year"] == 2022  # int coerced back
    # Section values round-trip.
    assert payload["contact"]["personal_email"] == "jane@x.com"
    assert payload["career"]["current_employer"] == "Acme Corp"
    assert payload["engagement"]["mentor_willing"] is True  # "Yes" -> True


# --- (d) the export row cap raises the expected error ------------------------


def test_cohort_export_over_cap_raises():
    session = FakeExportSession([], total=alumni_export.MAX_EXPORT_ROWS + 1)
    try:
        _run(import_csv.build_cohort_update_csv(session, 2018, actor_user_id=7))
    except import_csv.CohortTooLargeError as exc:
        assert exc.total == alumni_export.MAX_EXPORT_ROWS + 1
        assert "narrow" in str(exc).lower()
    else:  # pragma: no cover - the cap must trip
        raise AssertionError("expected CohortTooLargeError")
    # Nothing was disclosed / committed once the cap tripped.
    assert session.added == []
    assert session.committed == 0


# --- Route-level (query param) coverage --------------------------------------


def _override_user(roles):
    import uuid

    from app.schemas.auth import UserContext

    return lambda: UserContext(
        user_id=1,
        auth_user_id=uuid.UUID("11111111-1111-1111-1111-111111111111"),
        roles=roles,
    )


def test_route_cohort_export_forbidden_for_view_only():
    from fastapi.testclient import TestClient

    from app.api.dependencies.auth import get_current_db_user
    from app.main import app

    app.dependency_overrides[get_current_db_user] = _override_user(["view_only"])
    with TestClient(app, raise_server_exceptions=False) as c:
        resp = c.get("/alumni/import/update/export", params={"grad_year": 2018})
    app.dependency_overrides.clear()
    assert resp.status_code == 403


def test_route_cohort_export_returns_csv_attachment():
    from fastapi.testclient import TestClient

    from app.api.dependencies.auth import get_current_db_user
    from app.core.database import get_session
    from app.main import app

    alumni = [Alumni(alumni_id=1, byu_id="123456789", net_id="jdoe",
                     first_name="Jane", last_name="Doe", graduation_year=2018)]
    session = FakeExportSession(alumni)

    async def _override():
        yield session

    app.dependency_overrides[get_session] = _override
    app.dependency_overrides[get_current_db_user] = _override_user(["full_access"])
    with TestClient(app, raise_server_exceptions=False) as c:
        resp = c.get("/alumni/import/update/export", params={"grad_year": 2018})
    app.dependency_overrides.clear()

    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/csv")
    assert 'filename="alumni_cohort_2018.csv"' in resp.headers["content-disposition"]
    header_row, data_rows = _parse(resp.text)
    assert header_row == import_csv.EXPECTED_HEADERS
    assert len(data_rows) == 1


def test_route_cohort_export_rejects_out_of_range_year():
    from fastapi.testclient import TestClient

    from app.api.dependencies.auth import get_current_db_user
    from app.core.database import get_session
    from app.main import app

    # Override get_session too: without a DATABASE_URL (CI) the real session
    # dependency raises during setup and 500s before the 1800 -> 422 query
    # validation is reached. The fake is never used (422 fires first).
    async def _override():
        yield FakeExportSession([])

    app.dependency_overrides[get_session] = _override
    app.dependency_overrides[get_current_db_user] = _override_user(["full_access"])
    with TestClient(app, raise_server_exceptions=False) as c:
        resp = c.get("/alumni/import/update/export", params={"grad_year": 1800})
    app.dependency_overrides.clear()
    assert resp.status_code == 422
