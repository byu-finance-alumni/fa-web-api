"""Auth-gate tests for the read-only list endpoints.

Confirms the dashboard / events / audit endpoints are not public — a missing
token is rejected with 401 before any query runs. (View access is granted to
all three roles, so there is no 403 case for these reads.)
"""

import pytest
from fastapi.testclient import TestClient

from app.core.database import get_session
from app.main import app


async def _no_db_session():
    yield None


@pytest.fixture
def client():
    app.dependency_overrides[get_session] = _no_db_session
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


@pytest.mark.parametrize(
    "path",
    ["/dashboard/summary", "/events", "/audit"],
)
def test_read_endpoint_requires_auth(client, path):
    response = client.get(path)
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "unauthorized"
