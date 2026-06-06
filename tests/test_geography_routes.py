"""Auth-gate tests for the geography dashboard endpoints.

No public access: every geography read must reject a missing token with 401
before any query runs. (View access is granted to all three roles.)
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
    [
        "/geography/summary",
        "/geography/states",
        "/geography/states/UT",
        "/geography/states/UT/alumni",
        "/geography/cities?state=UT&city=Provo",
    ],
)
def test_geography_requires_auth(client, path):
    response = client.get(path)
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "unauthorized"
