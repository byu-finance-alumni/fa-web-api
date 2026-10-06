"""Filter / sort / search oracles on the fields a non-editor gets NULLED.

``minimize_alumni_read`` nulls ``VIEW_ONLY_HIDDEN_FIELDS`` (net_id, byu_id,
gender, ...) for any caller without ``can_edit_alumni`` — today, ``view_only``.
Hiding the value in the response is not enough while a filter, a sort or the
free-text search still REACTS to it: ``?net_id=a`` -> 3 hits, ``?net_id=ab`` ->
1 hit recovers the Net ID a character at a time from ``total`` alone (2026-10-02
review). These pin the fix, which follows the ``email`` precedent on
``GET /alumni`` exactly: the param is silently ignored for that caller.

The structural tests read the hidden set and the live OpenAPI parameters, so a
filter or sort token added later for a hidden field fails here until it is
gated too.
"""

import asyncio
import uuid
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.dialects import postgresql

from app.api.dependencies import auth as auth_deps
from app.api.dependencies.auth import get_current_db_user
from app.api.routes import alumni as alumni_routes
from app.core.database import get_session
from app.main import app
from app.repositories.alumni import build_alumni_query
from app.repositories.alumni_search import _ID_COLUMNS
from app.schemas.alumni import VIEW_ONLY_HIDDEN_FIELDS
from app.schemas.auth import UserContext
from app.services import alumni_export


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


@pytest.fixture
def captured(monkeypatch):
    """The kwargs GET /alumni hands the query builder, plus the audit summary."""
    seen: dict = {"list": {}, "log": {}}

    async def _fake_list(session, **kwargs):
        seen["list"].update(kwargs)
        return [], 0

    async def _fake_log(session, **kwargs):
        seen["log"].update(kwargs["filters"])

    monkeypatch.setattr(alumni_routes.service, "list_alumni", _fake_list)
    monkeypatch.setattr(alumni_routes.service, "log_search", _fake_log)
    return seen


def _list_params() -> dict[str, dict]:
    """GET /alumni's query parameters, by name, from the live OpenAPI schema."""
    op = app.openapi()["paths"]["/alumni"]["get"]
    return {p["name"]: p for p in op["parameters"]}


def _sort_tokens() -> set[str]:
    schema = _list_params()["sort"]["schema"]
    options = schema.get("anyOf", [schema])
    return {token for option in options for token in option.get("enum", [])}


# A valid value for each hidden-field param, so a probe isn't 422'd before it
# reaches the gate. A future hidden-field filter falls back to "ab"; if that
# doesn't validate, the test fails loudly and this map needs an entry.
_PROBE_VALUES = {"gender": "F"}


def _hidden_filter_params() -> list[str]:
    return sorted(set(_list_params()) & VIEW_ONLY_HIDDEN_FIELDS)


def _hidden_sort_tokens() -> list[str]:
    return sorted(_sort_tokens() & VIEW_ONLY_HIDDEN_FIELDS)


# --- GET /alumni --------------------------------------------------------------


def test_the_oracle_surface_is_not_empty():
    # Guard against the parametrized tests below passing vacuously because the
    # schema lookup stopped finding anything.
    assert {"net_id", "gender"} <= set(_hidden_filter_params())
    assert "gender" in _hidden_sort_tokens()
    assert {c.key for c in _ID_COLUMNS} <= VIEW_ONLY_HIDDEN_FIELDS


@pytest.mark.parametrize("param", _hidden_filter_params())
def test_hidden_field_filter_is_ignored_for_view_only(client, captured, param):
    app.dependency_overrides[get_current_db_user] = lambda: _ctx("view_only")
    value = _PROBE_VALUES.get(param, "ab")
    response = client.get("/alumni", params={param: value})
    assert response.status_code == 200, response.text
    assert captured["list"][param] is None
    # The audit summary records the EFFECTIVE filter, like ``email``.
    assert captured["log"][param] is None


@pytest.mark.parametrize("token", _hidden_sort_tokens())
def test_hidden_field_sort_is_ignored_for_view_only(client, captured, token):
    app.dependency_overrides[get_current_db_user] = lambda: _ctx("view_only")
    response = client.get("/alumni", params={"sort": token})
    assert response.status_code == 200, response.text
    # Falls back to the default order (name / relevance), as if omitted.
    assert captured["list"]["sort"] is None


def test_free_text_search_skips_id_columns_for_view_only(client, captured):
    app.dependency_overrides[get_current_db_user] = lambda: _ctx("view_only")
    response = client.get("/alumni", params={"q": "ab12"})
    assert response.status_code == 200
    assert captured["list"]["q"] == "ab12"  # the name/employer search still runs
    assert captured["list"]["match_ids"] is False


@pytest.mark.parametrize("role", ["engineer", "super_admin", "full_access", "student"])
def test_editors_keep_every_hidden_field_filter(client, captured, role):
    """Every role that RECEIVES these fields may still filter / sort / search on
    them — the gate follows ``can_edit_alumni``, nothing wider."""
    app.dependency_overrides[get_current_db_user] = lambda: _ctx(role)
    response = client.get(
        "/alumni",
        params={"net_id": "ab", "gender": "F", "sort": "gender", "q": "ab12"},
    )
    assert response.status_code == 200
    assert captured["list"]["net_id"] == "ab"
    assert captured["list"]["gender"] == "F"
    assert captured["list"]["sort"] == "gender"
    assert captured["list"]["match_ids"] is True


def test_friend_id_in_net_id_box_still_works_for_view_only(client, captured):
    # A friend id names a primary key (visible to everyone), not a Net ID, so
    # the #538 lookup keeps working for a view_only caller.
    app.dependency_overrides[get_current_db_user] = lambda: _ctx("view_only")
    response = client.get("/alumni", params={"net_id": "FRIEND-00042"})
    assert response.status_code == 200
    assert captured["list"]["net_id"] == "FRIEND-00042"


# --- query builder ------------------------------------------------------------


def _where(stmt) -> str:
    # The WHERE clause only: the SELECT list names every column, net_id included.
    return str(stmt.whereclause.compile(dialect=postgresql.dialect()))


def _sql(**kwargs) -> str:
    return _where(build_alumni_query(**kwargs))


def test_match_ids_false_drops_the_id_columns_from_q():
    # The name/id legs run over an ``alumni_1`` alias, so match on the column.
    assert ".net_id" in _sql(q="ab12")
    assert ".byu_id" in _sql(q="ab12")
    gated = _sql(q="ab12", match_ids=False)
    assert ".net_id" not in gated
    assert ".byu_id" not in gated
    # The rest of the search is untouched (name columns still match).
    assert ".last_name" in gated


def test_match_ids_false_keeps_a_typed_friend_id():
    # The friend-id leg (#538) is a primary-key match, not an id-column match.
    assert "alumni.alumni_id" in _sql(q="FRIEND-00042", match_ids=False)


# --- POST /alumni/export ------------------------------------------------------
#
# Full_access by default, but ``alumni.export`` is assignable in the permission
# matrix, so the same gate applies if a non-editor is ever granted it.


@pytest.fixture
def export_capture(monkeypatch):
    seen: dict = {}

    async def _count(session, filters, *, match_ids=True):
        seen["filters"] = filters
        seen["match_ids"] = match_ids
        return 0

    async def _csv(session, *, columns, filters, actor_user_id, match_ids=True):
        seen["csv_filters"] = filters
        seen["csv_match_ids"] = match_ids
        return "first_name\n"

    monkeypatch.setattr(alumni_export, "count_matching", _count)
    monkeypatch.setattr(alumni_export, "export_csv", _csv)
    return seen


def _export(client, role: str, filters: dict):
    app.dependency_overrides[auth_deps.require_alumni_export] = lambda: _ctx(role)
    return client.post(
        "/alumni/export", json={"columns": ["first_name"], "filters": filters}
    )


def test_export_drops_hidden_filters_for_a_non_editor(client, export_capture):
    response = _export(
        client, "view_only", {"q": "ab12", "net_id": "ab", "gender": "F"}
    )
    assert response.status_code == 200, response.text
    filters = export_capture["filters"]
    # DROPPED, not set to None: under ``exclude_unset`` an explicit None counts
    # as set and would override a builder default.
    assert filters.model_fields_set == {"q"}
    assert export_capture["match_ids"] is False
    # The CSV runs over the very same gated population the count checked.
    assert export_capture["csv_filters"] is filters
    assert export_capture["csv_match_ids"] is False


def test_export_keeps_hidden_filters_for_an_editor(client, export_capture):
    response = _export(
        client, "full_access", {"q": "ab12", "net_id": "ab", "gender": "F"}
    )
    assert response.status_code == 200, response.text
    filters = export_capture["filters"]
    assert (filters.net_id, filters.gender) == ("ab", "F")
    assert export_capture["match_ids"] is True


def test_export_keeps_a_friend_id_for_a_non_editor(client, export_capture):
    response = _export(client, "view_only", {"net_id": "FRIEND-00042"})
    assert response.status_code == 200, response.text
    assert export_capture["filters"].net_id == "FRIEND-00042"


def test_export_match_ids_reaches_the_query_builder():
    """``build_export_query`` forwards the switch, so the export can't quietly
    keep matching ids the list stopped matching."""
    from app.schemas.alumni_export import AlumniExportFilters
    from app.services.alumni_export import build_export_query

    stmt = asyncio.run(
        build_export_query(None, AlumniExportFilters(q="ab12"), match_ids=False)
    )
    assert ".net_id" not in _where(stmt)


# --- headshots: archived rows -------------------------------------------------


class _HeadshotSession:
    def __init__(self, rows=(), one=None):
        self._rows = list(rows)
        self._one = one
        self.statements: list = []

    async def scalars(self, stmt):
        self.statements.append(stmt)
        return SimpleNamespace(all=lambda: list(self._rows))

    async def scalar(self, stmt):
        self.statements.append(stmt)
        return self._one


def _headshot_client(session, role):
    async def _override():
        yield session

    app.dependency_overrides[get_session] = _override
    app.dependency_overrides[get_current_db_user] = lambda: _ctx(role)
    return TestClient(app)


@pytest.fixture
def signed(monkeypatch):
    async def _sign(bucket, path, **kwargs):
        return f"https://storage.test/sign/{bucket}/{path}?token=abc"

    monkeypatch.setattr(alumni_routes.supabase_storage, "create_signed_url", _sign)


@pytest.mark.parametrize(
    ("role", "filtered"),
    [("view_only", True), ("student", True), ("full_access", False)],
)
def test_headshot_batch_excludes_archived_below_full_access(signed, role, filtered):
    session = _HeadshotSession()
    try:
        with _headshot_client(session, role) as c:
            resp = c.get("/alumni/headshots/urls?alumni_ids=5")
    finally:
        app.dependency_overrides.clear()
    assert resp.status_code == 200
    sql = str(session.statements[0].compile(dialect=postgresql.dialect()))
    assert ("alumni.archived IS false" in sql) is filtered


@pytest.mark.parametrize(("role", "status"), [("view_only", 404), ("full_access", 200)])
def test_single_headshot_404s_archived_below_full_access(signed, role, status):
    archived = SimpleNamespace(alumni_id=5, net_id="jdoe12", archived=True)
    try:
        with _headshot_client(_HeadshotSession(one=archived), role) as c:
            resp = c.get("/alumni/5/headshot")
    finally:
        app.dependency_overrides.clear()
    assert resp.status_code == status
