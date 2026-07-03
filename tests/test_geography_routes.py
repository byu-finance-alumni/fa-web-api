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
        "/geography/counties",
        "/geography/countries",
        "/geography/countries/Japan",
        "/geography/countries/Japan/alumni",
        "/geography/states/UT",
        "/geography/states/UT/alumni",
        "/geography/cities?state=UT&city=Provo",
        "/geography/radius?lat=40.25&lng=-111.65&miles=50",
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


def test_get_counties_maps_fips_to_counts():
    # get_counties runs one grouped query: (county_fips, count) rows -> dicts with
    # int counts, ready for the national county choropleth.
    executes = [[("49049", 12), ("49035", 7)]]
    result = asyncio.run(svc.get_counties(_FakeSession(executes), {}))
    assert result == [
        {"county_fips": "49049", "count": 12},
        {"county_fips": "49035", "count": 7},
    ]


def test_get_countries_folds_case_variants_and_sorts():
    # get_countries runs one grouped query returning (country, count) rows (the
    # USA is already excluded in SQL). Case/spacing variants of the same country
    # fold together, and the result is sorted by count desc.
    executes = [[("United Kingdom", 5), ("Japan", 3), ("united kingdom", 2)]]
    result = asyncio.run(svc.get_countries(_FakeSession(executes), {}))
    assert result == [
        {"country": "United Kingdom", "alumni_count": 7},
        {"country": "Japan", "alumni_count": 3},
    ]


def test_get_country_detail_shortcircuits_usa_without_querying():
    # The world view is international: a US alias must return the empty shape
    # without running any query (session would raise if execute/scalar ran).
    class _Boom:
        async def execute(self, stmt):
            raise AssertionError("should not query for a US alias")

        async def scalar(self, stmt):
            raise AssertionError("should not query for a US alias")

    result = asyncio.run(svc.get_country_detail(_Boom(), "USA", {}))
    assert result == {
        "country": "USA",
        "alumni_count": 0,
        "employers": [],
        "industries": [],
        "by_graduation_year": [],
    }


def test_get_country_detail_aggregates_for_a_country():
    # execute() order in get_country_detail: employers _top, industries _top,
    # then the grad-year histogram. scalar() (total) returns 0 from the stub, so
    # feed the three execute() calls their rows.
    executes = [
        [("Barclays", 4)],           # employers _top
        [("Investment Banking", 4)], # industries _top
        [(2012, 2), (2016, 2)],      # grad-year histogram
    ]
    result = asyncio.run(
        svc.get_country_detail(_FakeSession(executes), "United Kingdom", {})
    )
    assert result["country"] == "United Kingdom"
    assert result["employers"] == [{"employer": "Barclays", "count": 4}]
    assert result["industries"] == [
        {"industry": "Investment Banking", "count": 4}
    ]
    assert result["by_graduation_year"] == [
        {"year": 2012, "count": 2},
        {"year": 2016, "count": 2},
    ]


def test_get_country_alumni_shortcircuits_usa_without_querying():
    # The world-view alumni list is international: a US alias returns an empty
    # page without querying (session would raise if execute/scalar ran).
    class _Boom:
        async def execute(self, stmt):
            raise AssertionError("should not query for a US alias")

        async def scalar(self, stmt):
            raise AssertionError("should not query for a US alias")

    result = asyncio.run(
        svc.get_country_alumni(
            _Boom(), "USA", {}, limit=50, offset=0, sort="name"
        )
    )
    assert result == {"items": [], "total": 0, "limit": 50, "offset": 0}
