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
    # execute() order in get_summary: get_states (for states_represented), _top
    # employers, _top industries, city_rows, then the options _distinct queries
    # (employers, CITIES, industries, graduation_years, regions) and finally the
    # tags query. The 6th execute is the cities _distinct — feed it two city
    # rows, everything else empty.
    executes = [[], [], [], [], [], [("Provo",), ("Salt Lake City",)]]
    summary = asyncio.run(svc.get_summary(_FakeSession(executes), {}))
    options = summary["options"]
    assert "cities" in options
    assert "employers" in options
    assert options["cities"] == ["Provo", "Salt Lake City"]


def test_get_states_folds_full_names_into_codes():
    # get_states normalizes raw state values: full names fold into their 2-letter
    # code ("Utah" -> "UT") and non-US values drop, so the map shows one bubble
    # per state regardless of how the value was entered.
    executes = [[("UT", 5), ("Utah", 3), ("CA", 2), ("Narnia", 9)]]
    result = asyncio.run(svc.get_states(_FakeSession(executes), {}))
    assert result == [
        {"state": "UT", "state_name": "Utah", "alumni_count": 8},
        {"state": "CA", "state_name": "California", "alumni_count": 2},
    ]


def test_summary_counts_match_normalized_map(monkeypatch):
    # #180: /geography/summary must count states/cities the SAME way the map does.
    # states_represented is derived from get_states, so a mix of "UT" and "Utah"
    # collapses to ONE state (raw COUNT DISTINCT would have said two). city rows
    # fold their state display through normalize_state ("UTAH" -> "UT").
    # execute() order: get_states rows, _top employers, _top industries,
    # city_rows, then options queries (all empty here).
    executes = [
        [("UT", 5), ("Utah", 3), ("CA", 2)],  # get_states -> folds to UT, CA
        [],                                     # top_employers
        [],                                     # top_industries
        [("Provo", "UTAH", 10)],                # city_rows (raw state name)
    ]
    summary = asyncio.run(svc.get_summary(_FakeSession(executes), {}))
    # UT + Utah collapse -> 2 states, not 3.
    assert summary["states_represented"] == 2
    # City-row state display is normalized to the 2-letter code.
    assert summary["top_cities"] == [{"city": "Provo", "state": "UT", "count": 10}]
    assert summary["largest_hub"] == {"city": "Provo", "state": "UT", "count": 10}


def test_city_group_by_normalizes_case_and_whitespace():
    # #180: cities are grouped on lower(trim(city)) so "Provo", "provo", and
    # "Provo " collapse into one hub instead of fragmenting the count. Assert the
    # shared grouping expression folds case (lower) and whitespace (trim).
    from sqlalchemy.dialects import postgresql

    sql = str(svc._CITY.compile(dialect=postgresql.dialect())).lower()
    assert "lower" in sql
    assert "trim" in sql


def test_state_code_expr_folds_full_name_to_code_in_sql():
    # State is stored as a full name ("Utah"), but every map GROUP BY and the
    # city_geo join key on 2-letter codes. _state_code_expr must emit a CASE that
    # folds a recognized full name to its code and passes anything else through
    # as upper(trim(...)), so a row stored "Utah" resolves to "UT".
    from sqlalchemy.dialects import postgresql

    from app.models.contact import AlumniContactInfo

    expr = svc._state_code_expr(AlumniContactInfo.state)
    sql = str(
        expr.compile(
            dialect=postgresql.dialect(),
            compile_kwargs={"literal_binds": True},
        )
    ).lower()
    assert "case" in sql
    assert "'utah'" in sql          # a full-name branch condition
    assert "'ut'" in sql            # ...mapping to the 2-letter code
    assert "upper" in sql           # else-branch pass-through
    assert "trim" in sql


def test_state_expr_used_by_map_is_the_code_fold():
    # The module-level _STATE (used for GROUP BY + city_geo joins) is the
    # code-fold expression, so "UT" and "Utah" rows collapse into one code.
    from sqlalchemy.dialects import postgresql

    sql = str(
        svc._STATE.compile(
            dialect=postgresql.dialect(),
            compile_kwargs={"literal_binds": True},
        )
    ).lower()
    assert "case" in sql
    assert "'utah'" in sql
    assert "'ut'" in sql


def test_get_state_detail_resolves_code_and_full_name_display():
    # get_state_detail("UT") normalizes the requested state to its code and shows
    # the full name; a row stored "Utah" would match via the _STATE code-fold.
    # scalar()->0 (total); executes: cities _top, employers _top, industries
    # _top, grad-year histogram.
    executes = [[], [], [], []]
    result = asyncio.run(svc.get_state_detail(_FakeSession(executes), "UT", {}))
    assert result["state"] == "UT"
    assert result["state_name"] == "Utah"


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
