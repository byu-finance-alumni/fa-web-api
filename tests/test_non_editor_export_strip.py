"""A non-editor holding ``alumni.export`` gets STRIPPED exports (2026-10-06).

``alumni.export`` is assignable in the permission matrix, and no non-editor has
it by default — but if one is granted it, every export route used to hand them
the fields their reads null (``VIEW_ONLY_HIDDEN_FIELDS`` on the core record, the
residence / ``best_contact`` on the contact row, the free-text notes). These pin
the fix: for ``can_edit_alumni == False`` those fields are dropped from

  * the ``POST /alumni/export`` column selection AND the column catalog,
  * ``GET /alumni/{id}/export`` (the same minimized aggregate the profile read
    gives them),
  * ``GET /alumni/import/update/export`` (the cohort template), and
  * ``GET /events/{id}/attendees/export`` (the Net ID column),

never refused — the exports keep working. Editors are unchanged.
"""

import asyncio
import csv as _csv
import io
import uuid
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app.api.dependencies import auth as auth_deps
from app.api.routes import alumni as alumni_routes
from app.core.database import get_session
from app.main import app
from app.models.alumni import Alumni
from app.models.contact import AlumniContactInfo
from app.schemas.alumni import VIEW_ONLY_HIDDEN_CONTACT_FIELDS, VIEW_ONLY_HIDDEN_FIELDS
from app.schemas.auth import UserContext
from app.services import alumni_export, import_csv
from app.services import profile as profile_service


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


def _as(role: str) -> None:
    """Hold ``alumni.export`` as ``role`` (a non-editor only via the matrix)."""
    app.dependency_overrides[auth_deps.require_alumni_export] = lambda: _ctx(role)


# The catalog keys a non-editor must never get: every column whose source field
# their reads null.
_HIDDEN_KEYS = {
    c.key
    for c in alumni_export.CATALOG
    if alumni_export.hidden_from_non_editor(c.source, c.attr)
}


def test_hidden_column_set_covers_the_read_minimizers():
    assert {"net_id", "byu_id", "mst_id", "gender", "birth_date", "notes"} <= _HIDDEN_KEYS
    assert {"spouse_first_name", "citizenship", "home_country"} <= _HIDDEN_KEYS
    # Residence + best_contact (the profile minimizer) and engagement notes.
    assert {"address_line_1", "zip", "city", "state", "best_contact"} <= _HIDDEN_KEYS
    assert "engagement_notes" in _HIDDEN_KEYS
    # Outreach contact and the WORK location stay (#166 / #283).
    for kept in ("personal_email", "work_email", "phone", "region", "current_city"):
        assert kept not in _HIDDEN_KEYS
    # Every alumni-row column that VIEW_ONLY_HIDDEN_FIELDS names is covered.
    for c in alumni_export.CATALOG:
        if c.source == "alumni" and c.attr in VIEW_ONLY_HIDDEN_FIELDS:
            assert c.key in _HIDDEN_KEYS
        if c.source == "contact" and c.attr in VIEW_ONLY_HIDDEN_CONTACT_FIELDS:
            assert c.key in _HIDDEN_KEYS


# --- column catalog -----------------------------------------------------------


def test_catalog_offers_no_hidden_column_to_a_non_editor(client):
    _as("view_only")
    body = client.get("/alumni/export/columns").json()
    keys = {c["key"] for c in body["columns"]}
    assert keys and not (keys & _HIDDEN_KEYS)
    assert not (set(body["default_selected"]) & _HIDDEN_KEYS)
    assert set(body["default_selected"]) <= keys


def test_catalog_is_whole_for_an_editor(client):
    _as("full_access")
    body = client.get("/alumni/export/columns").json()
    assert {c["key"] for c in body["columns"]} == {c.key for c in alumni_export.CATALOG}
    assert body["default_selected"] == alumni_export.DEFAULT_SELECTED


# --- POST /alumni/export ------------------------------------------------------


@pytest.fixture
def export_capture(monkeypatch):
    seen: dict = {}

    async def _count(session, filters, *, match_ids=True):
        return 0

    async def _csv(session, *, columns, filters, actor_user_id, match_ids=True):
        seen["columns"] = [c.key for c in columns]
        return "x\n"

    monkeypatch.setattr(alumni_export, "count_matching", _count)
    monkeypatch.setattr(alumni_export, "export_csv", _csv)
    return seen


_ASKED = ["first_name", "net_id", "byu_id", "gender", "birth_date", "city", "work_email"]


def test_export_strips_hidden_columns_for_a_non_editor(client, export_capture):
    _as("view_only")
    resp = client.post("/alumni/export", json={"columns": _ASKED})
    assert resp.status_code == 200, resp.text
    assert export_capture["columns"] == ["first_name", "work_email"]


def test_export_keeps_every_column_for_an_editor(client, export_capture):
    _as("full_access")
    resp = client.post("/alumni/export", json={"columns": _ASKED})
    assert resp.status_code == 200, resp.text
    assert set(export_capture["columns"]) == set(_ASKED)


def test_export_of_only_hidden_columns_is_422_for_a_non_editor(client, export_capture):
    _as("view_only")
    resp = client.post("/alumni/export", json={"columns": ["net_id", "byu_id"]})
    assert resp.status_code == 422, resp.text
    assert "columns" not in export_capture


def test_export_csv_body_carries_no_hidden_value():
    """End to end through the real CSV builder: the stripped selection yields a
    file with neither the header nor the value."""

    class _Session:
        async def execute(self, _stmt):
            alum = Alumni(alumni_id=1, first_name="Jane", last_name="Doe", net_id="jdoe9")
            return SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: [alum]))

        def add(self, _obj):
            pass

        async def commit(self):
            pass

    async def _query(session, filters, *, match_ids=True):
        from sqlalchemy import select

        return select(Alumni)

    cols = alumni_export.visible_columns(
        alumni_export.validate_columns(["first_name", "net_id"]), can_edit=False
    )
    orig = alumni_export.build_export_query
    alumni_export.build_export_query = _query
    try:
        text = asyncio.run(
            alumni_export.export_csv(
                _Session(),
                columns=cols,
                filters=alumni_export.AlumniExportFilters(),
                actor_user_id=1,
            )
        )
    finally:
        alumni_export.build_export_query = orig
    assert "jdoe9" not in text and "Net ID" not in text
    assert text.splitlines() == ["First name", "Jane"]


# --- GET /alumni/{id}/export --------------------------------------------------


@pytest.mark.parametrize(
    ("role", "can_edit", "amounts"),
    [("view_only", False, False), ("full_access", True, True)],
)
def test_profile_export_route_scopes_like_the_profile_read(
    client, monkeypatch, role, can_edit, amounts
):
    """Through the real route and the real ``export_profile``/``get_profile``
    (on the profile fake session from test_ferpa_alumni): a non-editor gets the
    view_only-minimized aggregate, an editor the whole record."""
    from tests.test_ferpa_alumni import _alumni_model, _ProfileFakeSession

    session = _ProfileFakeSession(_alumni_model())
    seen: dict = {}
    real_export = profile_service.export_profile

    async def _export(sess, alumni_id, **kwargs):
        seen.update(kwargs)
        return await real_export(sess, alumni_id, **kwargs)

    async def _session():
        yield session

    monkeypatch.setattr(alumni_routes.profile_service, "export_profile", _export)
    app.dependency_overrides[get_session] = _session
    _as(role)
    resp = client.get("/alumni/1/export")
    assert resp.status_code == 200, resp.text
    assert seen["can_edit"] is can_edit
    assert seen["show_pay_it_forward_amounts"] is amounts
    core = resp.json()["alumni"]
    if can_edit:
        assert (core["net_id"], core["byu_id"], core["gender"]) == (
            "jdoe12",
            "123456789",
            "Female",
        )
    else:
        for field in VIEW_ONLY_HIDDEN_FIELDS & set(core):
            assert core[field] is None, field
    assert core["first_name"] == "Jane"


def test_export_profile_builds_the_view_only_minimized_aggregate(monkeypatch):
    """A non-editor's profile export is built exactly like their profile read:
    ``can_edit=False`` (-> ``_minimize_profile_for_view_only``) and no tasks."""
    seen: dict = {}

    class _Profile:
        def model_dump(self, **_kw):
            return {"ok": True}

    async def _get_profile(session, alumni_id, **kwargs):
        seen.update(kwargs)
        return _Profile()

    monkeypatch.setattr(profile_service, "get_profile", _get_profile)
    out = asyncio.run(
        profile_service.export_profile(
            None, 5, actor_user_id=None, can_edit=False, show_pay_it_forward_amounts=False
        )
    )
    assert out == {"ok": True}
    assert seen == {
        "include_tasks": False,
        "can_edit": False,
        "show_pay_it_forward_amounts": False,
    }


def test_profile_minimizer_nulls_every_hidden_contact_field():
    """The shared constant IS what the profile minimizer nulls."""
    contact = SimpleNamespace(
        model_copy=lambda update: update,
    )
    profile = SimpleNamespace(
        contact=contact,
        interactions=[],
        surveys=[],
        engagement_notes=[],
        program_engagement=None,
        alumni=SimpleNamespace(model_copy=lambda update: update),
        model_copy=lambda update: update,
    )
    out = profile_service._minimize_profile_for_view_only(profile)
    assert out["contact"] == {f: None for f in VIEW_ONLY_HIDDEN_CONTACT_FIELDS}


# --- GET /alumni/import/update/export (cohort template) -----------------------


class _CohortSession:
    def __init__(self, alumni, contact):
        self._alumni = alumni
        self._contact = contact
        self.added: list = []

    async def scalar(self, _stmt):
        return len(self._alumni)

    async def execute(self, stmt):
        # Routed by side-table name, like tests/test_alumni_cohort_export.py: the
        # plain cohort query names no side table, so it falls through to alumni.
        from sqlalchemy.dialects import postgresql

        sql = str(stmt.compile(dialect=postgresql.dialect()))
        if "alumni_contact_info" in sql:
            rows = self._contact
        elif any(
            t in sql
            for t in (
                "current_employment",
                "education_history",
                "alumni_program_engagement",
            )
        ):
            rows = []
        else:
            rows = self._alumni
        return SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: list(rows)))

    def add(self, obj):
        self.added.append(obj)

    async def commit(self):
        pass


def _cohort(can_edit: bool):
    alumni = [
        Alumni(
            alumni_id=1, byu_id="123456789", net_id="jdoe9", gender="F",
            first_name="Jane", last_name="Doe", graduation_year=2018,
            spouse_first_name="Sam", spouse_last_name="Doe",
        )
    ]
    contact = [
        AlumniContactInfo(
            alumni_id=1, personal_email="jane@personal.com", city="Orem",
            address_line_1="1 Home St",
        )
    ]
    text = asyncio.run(
        import_csv.build_cohort_update_csv(
            _CohortSession(alumni, contact),
            graduation_year=2018,
            actor_user_id=7,
            can_edit=can_edit,
        )
    )
    rows = list(_csv.reader(io.StringIO(text)))
    return rows[0], rows[1:], text


def test_cohort_template_drops_hidden_columns_for_a_non_editor():
    header, rows, text = _cohort(can_edit=False)
    for gone in (
        "Net ID", "BYU ID (9 digits)", "MSTID (from OneAccord)", "Gender",
        "Birthday (YYYY-MM-DD)", "Spouse Name", "Notes", "Residence city",
        "Address line 1",
    ):
        assert gone not in header
    for value in ("jdoe9", "123456789", "Sam Doe", "Orem", "1 Home St"):
        assert value not in text
    # The rest of the template is intact and filled.
    assert header == [h for h in import_csv.TEMPLATE_HEADERS if h in header]
    assert rows[0][header.index("First name")] == "Jane"
    assert rows[0][header.index("Personal Email")] == "jane@personal.com"


def test_cohort_template_is_whole_for_an_editor():
    header, rows, _text = _cohort(can_edit=True)
    assert header == import_csv.TEMPLATE_HEADERS
    assert rows[0][header.index("Net ID")] == "jdoe9"


@pytest.mark.parametrize(("role", "can_edit"), [("view_only", False), ("student", True)])
def test_cohort_route_passes_the_callers_edit_right(client, monkeypatch, role, can_edit):
    seen: dict = {}

    async def _build(session, **kwargs):
        seen.update(kwargs)
        return "x\n"

    monkeypatch.setattr(alumni_routes.import_csv, "build_cohort_update_csv", _build)
    _as(role)
    resp = client.get("/alumni/import/update/export", params={"grad_year": 2018})
    assert resp.status_code == 200, resp.text
    assert seen["can_edit"] is can_edit


# --- GET /events/{id}/attendees/export ----------------------------------------


class _AttendeeSession:
    def __init__(self):
        self.added: list = []

    async def get(self, _model, _pk):
        return SimpleNamespace(event_name="Night")

    async def execute(self, _stmt):
        alum = SimpleNamespace(
            first_name="Jane", preferred_first_name=None, last_name="Doe", net_id="jdoe9"
        )
        return SimpleNamespace(all=lambda: [(alum, "jane@personal.com", None)])

    def add(self, obj):
        self.added.append(obj)

    async def commit(self):
        pass


@pytest.mark.parametrize(
    ("role", "header", "row", "cols"),
    [
        ("view_only", "Name,Email", "Jane Doe,jane@personal.com", "name,email"),
        (
            "full_access",
            "Name,Email,Net ID",
            "Jane Doe,jane@personal.com,jdoe9",
            "name,email,net_id",
        ),
    ],
)
def test_attendee_export_drops_net_id_for_a_non_editor(client, role, header, row, cols):
    session = _AttendeeSession()

    async def _session():
        yield session

    app.dependency_overrides[get_session] = _session
    _as(role)
    resp = client.get("/events/7/attendees/export")
    assert resp.status_code == 200, resp.text
    assert resp.text.splitlines() == [header, row]
    assert f"columns={cols};" in session.added[0].new_value
