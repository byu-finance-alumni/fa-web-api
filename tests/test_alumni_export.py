"""Tests for the customizable alumni CSV export (#33).

* Auth + validation gating (no DB) — export is full_access and up (view_only
  AND student rejected); empty/unknown column lists are 422; missing token 401.
* Column catalog — static metadata, returns columns + a default selection.
* Happy paths (fake session) — CSV header uses column labels, rows project the
  chosen columns (incl. a bulk-loaded side table), bools render Yes/No, an
  over-cap result is 413, and every export writes an export_alumni audit row.
"""

import csv
import datetime
import io
import uuid
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app.api.dependencies.auth import get_current_db_user
from app.core.database import get_session
from app.main import app
from app.models.audit import AuditLog
from app.models.contact import AlumniContactInfo
from app.schemas.alumni_export import AlumniExportFilters
from app.schemas.auth import UserContext
from app.services.alumni_export import _filters_dict


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


# --- #160 "needs surveying" export filter ------------------------------------


def test_filters_dict_derives_survey_cutoff_when_needs_survey():
    # The export body carries only the flag; the service derives the 2-year
    # cutoff server-side (the body never supplies a trusted "now").
    out = _filters_dict(AlumniExportFilters(needs_survey=True))
    assert out["needs_survey"] is True
    cutoff = out["survey_due_before"]
    assert isinstance(cutoff, datetime.datetime)
    expected = datetime.datetime.now(datetime.UTC) - datetime.timedelta(days=365 * 2)
    assert abs((cutoff - expected).total_seconds()) < 60


def test_filters_dict_no_survey_cutoff_when_flag_unset():
    out = _filters_dict(AlumniExportFilters())
    assert "survey_due_before" not in out


class _FakeResult:
    def __init__(self, rows):
        self._rows = rows

    def scalars(self):
        return self

    def all(self):
        return list(self._rows)


class _FakeSession:
    """No-DB session: ``scalar`` returns the queued count; ``execute`` pops the
    next queued row set (export loads alumni first, then any selected side
    tables, in order)."""

    def __init__(self, *, count=0, execute_results=None):
        self._count = count
        self._results = list(execute_results or [])
        self.added = []
        self.committed = False

    async def scalar(self, stmt):
        return self._count

    async def execute(self, stmt):
        rows = self._results.pop(0) if self._results else []
        return _FakeResult(rows)

    def add(self, obj):
        self.added.append(obj)

    async def commit(self):
        self.committed = True

    @property
    def audits(self):
        return [a for a in self.added if isinstance(a, AuditLog)]


def _with_session(session):
    async def _override():
        yield session

    return _override


def _alum(**kw):
    base = dict(
        alumni_id=1,
        byu_id="123456789",
        net_id="jdoe1",
        first_name="Jane",
        last_name="Doe",
        preferred_first_name=None,
        graduation_year=2018,
        linkedin_url=None,
        deceased=False,
        is_alumni=True,
        notes=None,
        birth_date=None,
    )
    base.update(kw)
    return SimpleNamespace(**base)


# --- auth + validation gating (no DB) ----------------------------------------


def test_export_requires_auth(client):
    response = client.post("/alumni/export", json={"columns": ["first_name"]})
    assert response.status_code == 401


def test_export_forbidden_for_view_only(client):
    app.dependency_overrides[get_current_db_user] = lambda: _ctx("view_only")
    response = client.post("/alumni/export", json={"columns": ["first_name"]})
    assert response.status_code == 403


def test_export_forbidden_for_student(client):
    # student can edit alumni but bulk export is full_access and up.
    app.dependency_overrides[get_current_db_user] = lambda: _ctx("student")
    response = client.post("/alumni/export", json={"columns": ["first_name"]})
    assert response.status_code == 403


def test_export_columns_requires_auth(client):
    response = client.get("/alumni/export/columns")
    assert response.status_code == 401


def test_export_columns_forbidden_for_view_only(client):
    app.dependency_overrides[get_current_db_user] = lambda: _ctx("view_only")
    response = client.get("/alumni/export/columns")
    assert response.status_code == 403


def test_export_rejects_empty_columns(client):
    app.dependency_overrides[get_current_db_user] = lambda: _ctx("full_access")
    response = client.post("/alumni/export", json={"columns": []})
    assert response.status_code == 422


def test_export_rejects_unknown_column(client):
    app.dependency_overrides[get_current_db_user] = lambda: _ctx("full_access")
    response = client.post("/alumni/export", json={"columns": ["not_a_column"]})
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"


def test_export_rejects_unknown_filter_field(client):
    app.dependency_overrides[get_current_db_user] = lambda: _ctx("full_access")
    response = client.post(
        "/alumni/export",
        json={"columns": ["first_name"], "filters": {"bogus": 1}},
    )
    assert response.status_code == 422


# --- catalog ------------------------------------------------------------------


def test_export_columns_catalog(client):
    app.dependency_overrides[get_current_db_user] = lambda: _ctx("full_access")
    response = client.get("/alumni/export/columns")
    assert response.status_code == 200
    body = response.json()
    keys = {c["key"] for c in body["columns"]}
    assert {"first_name", "current_employer", "personal_email"} <= keys
    # Default selection is a non-empty subset of the catalog.
    assert body["default_selected"]
    assert set(body["default_selected"]) <= keys
    # Sensitive PII is NOT default-checked.
    assert "byu_id" not in body["default_selected"]
    assert "notes" not in body["default_selected"]


# --- happy paths --------------------------------------------------------------


def _parse_csv(text):
    return list(csv.reader(io.StringIO(text)))


def test_export_alumni_only_columns(client):
    session = _FakeSession(
        count=2,
        execute_results=[
            [
                _alum(),
                _alum(alumni_id=2, first_name="John", last_name="Smith", graduation_year=2020),
            ]
        ],
    )
    app.dependency_overrides[get_current_db_user] = lambda: _ctx("full_access")
    app.dependency_overrides[get_session] = _with_session(session)

    response = client.post(
        "/alumni/export",
        json={"columns": ["first_name", "last_name", "graduation_year"]},
    )
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/csv")
    assert "attachment" in response.headers["content-disposition"]
    rows = _parse_csv(response.text)
    assert rows[0] == ["First name", "Last name", "Graduation year"]
    assert rows[1] == ["Jane", "Doe", "2018"]
    assert rows[2] == ["John", "Smith", "2020"]
    # Disclosure audit written + committed.
    assert [a.action_type for a in session.audits] == ["export_alumni"]
    assert "rows=2" in session.audits[0].new_value
    assert session.committed


def test_export_renders_bool_as_yes_no_and_blank_none(client):
    session = _FakeSession(
        count=1,
        execute_results=[[_alum(deceased=True, preferred_first_name=None)]],
    )
    app.dependency_overrides[get_current_db_user] = lambda: _ctx("full_access")
    app.dependency_overrides[get_session] = _with_session(session)

    response = client.post(
        "/alumni/export",
        json={"columns": ["preferred_first_name", "deceased"]},
    )
    assert response.status_code == 200
    rows = _parse_csv(response.text)
    assert rows[0] == ["Preferred first name", "Deceased?"]
    assert rows[1] == ["", "Yes"]  # None -> blank, True -> Yes


def test_export_includes_side_table_column(client):
    # Selecting a contact column triggers a second execute (bulk contact load).
    alumni_rows = [_alum(alumni_id=1)]
    contact_rows = [AlumniContactInfo(alumni_id=1, personal_email="jane@example.com")]
    session = _FakeSession(count=1, execute_results=[alumni_rows, contact_rows])
    app.dependency_overrides[get_current_db_user] = lambda: _ctx("super_admin")
    app.dependency_overrides[get_session] = _with_session(session)

    response = client.post(
        "/alumni/export",
        json={"columns": ["first_name", "personal_email"]},
    )
    assert response.status_code == 200
    rows = _parse_csv(response.text)
    assert rows[0] == ["First name", "Personal email"]
    assert rows[1] == ["Jane", "jane@example.com"]


def test_export_neutralizes_csv_formula_injection(client):
    # A free-text field starting with = / + / - / @ must be tab-prefixed so Excel
    # doesn't execute it as a formula on open.
    session = _FakeSession(
        count=1,
        execute_results=[[_alum(first_name='=HYPERLINK("http://evil")')]],
    )
    app.dependency_overrides[get_current_db_user] = lambda: _ctx("full_access")
    app.dependency_overrides[get_session] = _with_session(session)

    response = client.post("/alumni/export", json={"columns": ["first_name"]})
    assert response.status_code == 200
    rows = _parse_csv(response.text)
    assert rows[1][0].startswith("\t=HYPERLINK")  # neutralized, value preserved


def test_export_over_cap_returns_413(client):
    from app.services import alumni_export

    session = _FakeSession(count=alumni_export.MAX_EXPORT_ROWS + 1)
    app.dependency_overrides[get_current_db_user] = lambda: _ctx("full_access")
    app.dependency_overrides[get_session] = _with_session(session)

    response = client.post("/alumni/export", json={"columns": ["first_name"]})
    assert response.status_code == 413
    assert response.json()["error"]["code"] == "payload_too_large"
    # Nothing exported / audited when over the cap.
    assert session.audits == []
