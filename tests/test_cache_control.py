"""Cache-Control on API responses (#597).

- The PUBLIC survey GET returns the alum's on-file PII behind a bearer token in
  the URL, so it must be ``no-store`` — success AND the dead-link 404.
- Every other response defaults to ``no-store`` via the security-headers
  middleware, but a route that deliberately sets its own caching keeps it (the
  favicons here; the headshot image's ``private, max-age=600`` is asserted in
  tests/test_headshot_proxy.py).
"""

import pytest
from fastapi.testclient import TestClient

from app.core import rate_limit
from app.core.database import get_session
from app.main import app
from app.schemas.survey import SurveyRespondInfo
from app.services import survey_email


@pytest.fixture
def client():
    async def _no_db_session():
        yield None

    rate_limit.reset()
    app.dependency_overrides[get_session] = _no_db_session
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.pop(get_session, None)
    rate_limit.reset()


def test_survey_respond_get_is_no_store(client, monkeypatch):
    async def found(session, token):
        return SurveyRespondInfo(
            first_name="Jane",
            full_name="Jane Doe",
            fields={"alumni.email": "jane@example.org"},
        )

    monkeypatch.setattr(survey_email, "get_respondent", found)
    resp = client.get("/survey/respond/tok-cache")
    assert resp.status_code == 200
    assert resp.headers["cache-control"] == "no-store"


def test_survey_respond_dead_link_is_no_store(client, monkeypatch):
    async def missing(session, token):
        return None

    monkeypatch.setattr(survey_email, "get_respondent", missing)
    resp = client.get("/survey/respond/tok-dead")
    assert resp.status_code == 404
    assert resp.headers["cache-control"] == "no-store"


def test_unauthenticated_error_is_no_store(client):
    resp = client.get("/alumni")
    assert resp.status_code == 401
    assert resp.headers["cache-control"] == "no-store"


def test_plain_json_route_defaults_to_no_store(client):
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.headers["cache-control"] == "no-store"


@pytest.mark.parametrize("path", ["/favicon.svg", "/favicon.ico"])
def test_route_with_its_own_cache_policy_keeps_it(client, path):
    resp = client.get(path)
    assert resp.status_code == 200
    assert resp.headers["cache-control"] == "public, max-age=86400"
