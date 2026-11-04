"""The alumni list's "left a comment" filter (fa-web-app#923).

Two options, one choice, off by default: ``survey_comment`` (an APPLIED response
carrying a comment) and ``any_note`` (any note on the PROFILE, staff's included).
Jake, 2026-11-04: a survey comment counts only after approval.

The SQL meaning of each predicate is pinned here structurally; list/export
equality is pinned in ``test_alumni_export_filter_parity.py``.
"""

import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.dialects import postgresql

from app.api.dependencies import auth as auth_deps
from app.api.dependencies.auth import get_current_db_user
from app.api.routes import alumni as alumni_routes
from app.core.database import get_session
from app.main import app
from app.repositories.alumni import build_alumni_query
from app.schemas.auth import UserContext
from app.services import alumni_export


def _ctx(*roles: str) -> UserContext:
    return UserContext(
        user_id=1,
        auth_user_id=uuid.UUID("11111111-1111-1111-1111-111111111111"),
        roles=list(roles),
    )


def _sql(**kwargs) -> str:
    return str(
        build_alumni_query(**kwargs).compile(
            dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}
        )
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
    seen: dict = {"list": {}, "log": {}}

    async def _fake_list(session, **kwargs):
        seen["list"].update(kwargs)
        return [], 0

    async def _fake_log(session, **kwargs):
        seen["log"].update(kwargs["filters"])

    monkeypatch.setattr(alumni_routes.service, "list_alumni", _fake_list)
    monkeypatch.setattr(alumni_routes.service, "log_search", _fake_log)
    return seen


@pytest.fixture
def export_capture(monkeypatch):
    seen: dict = {}

    async def _count(session, filters, *, match_ids=True):
        seen["filters"] = filters
        return 0

    async def _csv(session, *, columns, filters, actor_user_id, match_ids=True):
        seen["csv_filters"] = filters
        return "first_name\n"

    monkeypatch.setattr(alumni_export, "count_matching", _count)
    monkeypatch.setattr(alumni_export, "export_csv", _csv)
    return seen


def _export(client, role: str, filters: dict):
    app.dependency_overrides[auth_deps.require_alumni_export] = lambda: _ctx(role)
    return client.post(
        "/alumni/export", json={"columns": ["first_name"], "filters": filters}
    )


# --- what each option means ---------------------------------------------------


def test_off_by_default_adds_no_predicate():
    assert _sql() == _sql(comment_filter=None)
    assert "FROM notes" not in _sql() and "FROM survey_responses" not in _sql()


def test_any_note_reads_profile_notes_only():
    sql = _sql(comment_filter="any_note")
    assert "notes.alumni_id = alumni.alumni_id" in sql
    # The Notes tab reads entity_type=alumni. A note on an interaction or an
    # event belongs to that row, and must not count.
    assert "interaction_id" not in sql.split("FROM notes")[1]
    assert "event_id" not in sql.split("FROM notes")[1]


def test_survey_comment_counts_applied_responses_only():
    sql = _sql(comment_filter="survey_comment")
    assert "survey_responses.status = 'applied'" in sql
    for status in ("pending", "rejected", "confirmed"):
        assert f"'{status}'" not in sql


def test_survey_comment_needs_a_real_comment_in_the_payload():
    sql = _sql(comment_filter="survey_comment")
    assert "jsonb_typeof" in sql
    assert "btrim" in sql
    assert "'comments'" in sql


def test_survey_comment_never_reads_notes():
    # The source is the response, so a staff note worded "Survey comment (...)"
    # cannot get an alum into the filter, and deleting the real note can't take
    # them out.
    sql = _sql(comment_filter="survey_comment")
    assert "FROM notes" not in sql
    assert "notes.body" not in sql


def test_survey_comment_keeps_a_response_a_reset_superseded():
    # A reset deletes nothing and the comment was really left (#395).
    assert "survey_reset_log" not in _sql(comment_filter="survey_comment")


@pytest.mark.parametrize("value", ["survey_comment", "any_note"])
def test_filter_is_an_exists_not_a_row_multiplying_join(value):
    sql = _sql(comment_filter=value)
    assert "EXISTS" in sql
    assert " JOIN " not in sql


def test_unknown_value_raises_rather_than_widening():
    with pytest.raises(ValueError):
        build_alumni_query(comment_filter="everything")


def test_an_unknown_value_is_a_422_on_both_routes(client):
    app.dependency_overrides[get_current_db_user] = lambda: _ctx("full_access")
    assert client.get("/alumni", params={"comment_filter": "x"}).status_code == 422
    assert _export(client, "full_access", {"comment_filter": "x"}).status_code == 422


# --- authorization -------------------------------------------------------------


def test_view_only_survey_comment_is_ignored_on_the_list(client, captured):
    app.dependency_overrides[get_current_db_user] = lambda: _ctx("view_only")
    response = client.get("/alumni", params={"comment_filter": "survey_comment"})
    assert response.status_code == 200, response.text
    assert captured["list"]["comment_filter"] is None
    # The audit summary records the EFFECTIVE filter.
    assert captured["log"]["comment_filter"] is None


def test_view_only_keeps_any_note_on_the_list(client, captured):
    # Every view-access role reads profile notes (GET /notes), so this one tells
    # them nothing they can't already see.
    app.dependency_overrides[get_current_db_user] = lambda: _ctx("view_only")
    response = client.get("/alumni", params={"comment_filter": "any_note"})
    assert response.status_code == 200, response.text
    assert captured["list"]["comment_filter"] == "any_note"
    assert captured["log"]["comment_filter"] == "any_note"


@pytest.mark.parametrize(
    "role", ["student", "full_access", "super_admin", "engineer"]
)
@pytest.mark.parametrize("value", ["survey_comment", "any_note"])
def test_editors_keep_both_options_on_the_list(client, captured, role, value):
    app.dependency_overrides[get_current_db_user] = lambda: _ctx(role)
    response = client.get("/alumni", params={"comment_filter": value})
    assert response.status_code == 200, response.text
    assert captured["list"]["comment_filter"] == value


def test_a_caller_without_view_access_gets_nothing(client, captured):
    app.dependency_overrides[get_current_db_user] = lambda: _ctx()
    response = client.get("/alumni", params={"comment_filter": "any_note"})
    assert response.status_code == 403
    assert captured["list"] == {}


def test_view_only_export_drops_survey_comment(client, export_capture):
    response = _export(
        client, "view_only", {"q": "ab12", "comment_filter": "survey_comment"}
    )
    assert response.status_code == 200, response.text
    filters = export_capture["filters"]
    # DROPPED, not set to None.
    assert filters.model_fields_set == {"q"}
    assert export_capture["csv_filters"] is filters


def test_view_only_export_keeps_any_note(client, export_capture):
    response = _export(client, "view_only", {"comment_filter": "any_note"})
    assert response.status_code == 200, response.text
    assert export_capture["filters"].comment_filter == "any_note"


@pytest.mark.parametrize("value", ["survey_comment", "any_note"])
def test_editor_export_keeps_both_options(client, export_capture, value):
    response = _export(client, "student", {"comment_filter": value})
    assert response.status_code == 200, response.text
    assert export_capture["filters"].comment_filter == value
