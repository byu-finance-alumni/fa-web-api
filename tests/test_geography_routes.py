"""Auth-gate tests for the geography dashboard endpoints.

No public access: every geography read must reject a missing token with 401
before any query runs. (View access is granted to all three roles.)
"""

import asyncio

import pytest
from fastapi.testclient import TestClient

from app.core.database import get_session
from app.main import app
from app.services import geography as svc


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


# --- summary options payload --------------------------------------------------


class _Result:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return self._rows


class _FakeSession:
    """Session stub feeding execute() a queue of per-call row lists (consumed in
    order, empty once drained); scalar() always returns 0."""

    def __init__(self, executes):
        self._executes = list(executes)

    async def execute(self, stmt):
        return _Result(self._executes.pop(0) if self._executes else [])

    async def scalar(self, stmt):
        return 0


def test_summary_options_includes_cities_alongside_employers():
    # The City dropdown sources its options from summary().options.cities, which
    # must be produced exactly like employers (distinct, non-null, sorted).
    # execute() order in get_summary: _top employers, _top industries, city_rows,
    # then the options _distinct queries (employers, CITIES, industries,
    # graduation_years, regions) and finally the tags query. The 5th execute is
    # the cities _distinct — feed it two city rows, everything else empty.
    executes = [[], [], [], [], [("Provo",), ("Salt Lake City",)]]
    summary = asyncio.run(svc.get_summary(_FakeSession(executes), {}))
    options = summary["options"]
    assert "cities" in options
    assert "employers" in options
    assert options["cities"] == ["Provo", "Salt Lake City"]
