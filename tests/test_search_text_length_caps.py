"""Length caps on the free-text search/filter strings (#597).

``GET /alumni`` and the ``POST /alumni/export`` filter body share one ceiling
(``SEARCH_TEXT_MAX_LENGTH``), so the list and the export refuse the same input.
Over-long input must be the app's ordinary 422 validation envelope — naming the
field, never echoing the value — and must never reach the search service.
"""

import uuid

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.api.dependencies.auth import get_current_db_user
from app.api.routes import alumni as alumni_routes
from app.core.database import get_session
from app.main import app
from app.schemas.alumni_export import SEARCH_TEXT_MAX_LENGTH, AlumniExportFilters
from app.schemas.auth import UserContext

_CAPPED = ("q", "net_id", "first_name", "last_name", "preferred_name", "email", "near")
_MARKER = "Z" * (SEARCH_TEXT_MAX_LENGTH + 1)


def _ctx(*roles: str) -> UserContext:
    return UserContext(
        user_id=1,
        auth_user_id=uuid.UUID("11111111-1111-1111-1111-111111111111"),
        roles=list(roles),
    )


async def _no_db_session():
    yield None


@pytest.fixture
def client(monkeypatch):
    calls: list[dict] = []

    async def _fake_list(session, **kwargs):
        calls.append(kwargs)
        return [], 0

    async def _fake_log(session, **kwargs):
        return None

    monkeypatch.setattr(alumni_routes.service, "list_alumni", _fake_list)
    monkeypatch.setattr(alumni_routes.service, "log_search", _fake_log)
    app.dependency_overrides[get_session] = _no_db_session
    app.dependency_overrides[get_current_db_user] = lambda: _ctx("full_access")
    with TestClient(app) as test_client:
        test_client.calls = calls
        yield test_client
    app.dependency_overrides.clear()


def test_the_cap_is_the_codebase_wide_search_length():
    assert SEARCH_TEXT_MAX_LENGTH == 200


@pytest.mark.parametrize("param", _CAPPED)
def test_an_over_long_list_filter_is_a_friendly_422(client, param):
    response = client.get("/alumni", params={param: _MARKER})

    assert response.status_code == 422
    body = response.json()
    assert body["error"]["code"] == "validation_error"
    assert param in {f["field"] for f in body["error"]["fields"]}
    assert _MARKER not in response.text  # the value is never echoed back
    assert client.calls == []  # and never reached the search


def test_a_filter_at_the_cap_is_still_accepted(client):
    response = client.get("/alumni", params={"q": "a" * SEARCH_TEXT_MAX_LENGTH})
    assert response.status_code == 200, response.text


def test_the_published_parameters_carry_the_cap():
    params = {p["name"]: p for p in app.openapi()["paths"]["/alumni"]["get"]["parameters"]}
    for name in _CAPPED:
        schema = params[name]["schema"]
        variants = schema.get("anyOf", [schema])
        assert any(v.get("maxLength") == SEARCH_TEXT_MAX_LENGTH for v in variants), name


@pytest.mark.parametrize("field", _CAPPED)
def test_the_export_filter_body_has_the_same_cap(field):
    AlumniExportFilters(**{field: "a" * SEARCH_TEXT_MAX_LENGTH})
    with pytest.raises(ValidationError):
        AlumniExportFilters(**{field: _MARKER})


def test_an_over_long_export_filter_is_a_friendly_422(client):
    response = client.post(
        "/alumni/export",
        json={"columns": ["first_name"], "filters": {"q": _MARKER}},
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"
    assert _MARKER not in response.text
