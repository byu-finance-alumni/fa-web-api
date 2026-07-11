"""Unit tests for the plain-English location search module (#358).

Covers the pure parser (``parse_location_query``), the pure distance helper
(``haversine_miles``), region-alias resolution, and the ``resolve_location`` /
``alumni_location_filter`` wiring with a stubbed session — no real database.
"""

import asyncio

from sqlalchemy.dialects import postgresql

from app.services import geo_search as gs
from app.services.geo_regions import REGION_ALIASES, lookup_region

# --- parser: distance phrases ------------------------------------------------


def test_within_miles_of_city_state():
    m = gs.parse_location_query("within 50 miles of Seattle, Washington")
    assert m is not None
    assert m.radius_miles == 50.0
    assert m.city == "seattle"
    assert m.state == "WA"
    assert m.center is None
    assert m.label == "Seattle, WA"


def test_within_miles_bare_city_no_state_is_allowed():
    # "within N miles of X" is a strong locational intent, so a bare city (no
    # state) still resolves — the resolver geocodes it against city_geo.
    m = gs.parse_location_query("within 25 miles of Seattle")
    assert m is not None
    assert m.radius_miles == 25.0
    assert m.city == "seattle"
    assert m.state is None


def test_within_accepts_decimal_and_mi_abbreviation():
    m = gs.parse_location_query("within 12.5 mi of Provo, UT")
    assert m is not None
    assert m.radius_miles == 12.5
    assert m.city == "provo"
    assert m.state == "UT"


# --- parser: near / around ---------------------------------------------------


def test_near_city_state_default_radius():
    m = gs.parse_location_query("near Los Angeles, California")
    assert m is not None
    assert m.radius_miles == gs.DEFAULT_RADIUS_MILES
    assert m.city == "los angeles"
    assert m.state == "CA"
    assert m.label == "Los Angeles, CA"


def test_around_bare_city_allowed_strong_context():
    m = gs.parse_location_query("around Chicago")
    assert m is not None
    assert m.city == "chicago"
    assert m.state is None
    assert m.radius_miles == gs.DEFAULT_RADIUS_MILES


# --- parser: bare "City, State" ---------------------------------------------


def test_bare_city_state_uses_bare_radius():
    m = gs.parse_location_query("Provo, UT")
    assert m is not None
    assert m.city == "provo"
    assert m.state == "UT"
    assert m.radius_miles == gs.BARE_CITY_RADIUS_MILES


def test_bare_city_trailing_state_name_no_comma():
    m = gs.parse_location_query("New York NY")
    assert m is not None
    assert m.city == "new york"
    assert m.state == "NY"


# --- parser: negatives (fall back to normal search) --------------------------


def test_bare_word_without_state_is_not_a_location():
    # A lone term with no state and no region alias must NOT hijack normal search.
    assert gs.parse_location_query("Seattle") is None
    assert gs.parse_location_query("software engineer") is None


def test_weak_in_prefix_requires_region_or_state():
    # "in <bare word>" is too ambiguous to treat as a city.
    assert gs.parse_location_query("in finance") is None
    # ...but "in <City, State>" and "in <region>" do resolve.
    assert gs.parse_location_query("in Boston, MA").state == "MA"
    assert gs.parse_location_query("in the Bay Area").center is not None


def test_empty_and_blank_return_none():
    assert gs.parse_location_query("") is None
    assert gs.parse_location_query("   ") is None
    assert gs.parse_location_query(None) is None


# --- parser: region aliases --------------------------------------------------


def test_region_alias_bay_area_has_center_and_cities():
    m = gs.parse_location_query("Bay Area")
    assert m is not None
    assert m.center == (37.7749, -122.4194)
    assert m.radius_miles == 45.0
    assert m.cities is not None
    assert ("san francisco", "CA") in m.cities
    assert m.label == "Bay Area, CA"


def test_region_alias_dmv_and_greater_seattle():
    dmv = gs.parse_location_query("DMV")
    assert dmv is not None and dmv.center == (38.9072, -77.0369)
    seattle = gs.parse_location_query("Greater Seattle area")
    assert seattle is not None and seattle.label == "Greater Seattle, WA"


def test_region_alias_socal_wide_radius():
    m = gs.parse_location_query("SoCal")
    assert m is not None
    assert m.radius_miles == 90.0
    assert m.center == (34.0522, -118.2437)


def test_region_alias_punctuation_insensitive():
    # "D.C. Area" normalizes to the DMV alias key.
    assert lookup_region("D.C. Area") is not None
    assert lookup_region("d.c. area") is lookup_region("dmv")


def test_within_of_region_alias_overrides_radius():
    # An explicit mileage on a region phrase wins over the region's default.
    m = gs.parse_location_query("within 20 miles of Greater Boston")
    assert m is not None
    assert m.radius_miles == 20.0
    assert m.center == (42.3601, -71.0589)


def test_every_alias_maps_to_center_or_cities():
    # Invariant: no region resolves to "nothing to search".
    for region in REGION_ALIASES.values():
        assert region.center is not None or region.cities


# --- haversine (pure) --------------------------------------------------------


def test_haversine_zero_distance():
    assert gs.haversine_miles(40.0, -111.0, 40.0, -111.0) == 0.0


def test_haversine_sf_to_la_known_distance():
    # San Francisco -> Los Angeles is ~347 miles great-circle.
    d = gs.haversine_miles(37.7749, -122.4194, 34.0522, -118.2437)
    assert 340 <= d <= 355


def test_haversine_provo_to_slc_short_hop():
    # Provo -> Salt Lake City is ~43 miles; comfortably inside a 45 mi radius.
    d = gs.haversine_miles(40.2338, -111.6585, 40.7608, -111.8910)
    assert 38 <= d <= 48


def test_haversine_is_symmetric():
    a = gs.haversine_miles(47.6062, -122.3321, 45.5152, -122.6784)
    b = gs.haversine_miles(45.5152, -122.6784, 47.6062, -122.3321)
    assert abs(a - b) < 1e-9


# --- resolve_location (stubbed session) --------------------------------------


class _Result:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return self._rows


class _FakeSession:
    """Feeds execute() a queue of per-call row lists (consumed in order)."""

    def __init__(self, executes):
        self._executes = list(executes)

    async def execute(self, stmt):
        return _Result(self._executes.pop(0) if self._executes else [])


def test_resolve_region_unions_explicit_cities_and_radius():
    # Bay Area has both explicit cities and a center. The single execute() is the
    # radius query; the result must union the radius rows with the explicit set.
    radius_rows = [("napa", "CA"), ("san francisco", "CA")]
    keys = asyncio.run(
        gs.resolve_location(
            _FakeSession([radius_rows]), gs.parse_location_query("Bay Area")
        )
    )
    # Explicit alias city present...
    assert ("oakland", "CA") in keys
    # ...and a radius-only city present...
    assert ("napa", "CA") in keys
    # ...and de-duplicated + sorted.
    assert keys == sorted(set(keys))


def test_resolve_named_city_geocodes_then_radius():
    # "near Provo, UT": first execute() is the city_geo coord lookup, second is
    # the radius query around those coords.
    coord_rows = [("provo", "UT", 40.2338, -111.6585)]
    radius_rows = [("orem", "UT"), ("provo", "UT"), ("lehi", "UT")]
    keys = asyncio.run(
        gs.resolve_location(
            _FakeSession([coord_rows, radius_rows]),
            gs.parse_location_query("near Provo, UT"),
        )
    )
    assert ("provo", "UT") in keys  # the named city itself
    assert ("orem", "UT") in keys   # a nearby city from the radius
    assert ("lehi", "UT") in keys


def test_resolve_location_query_returns_none_for_non_location():
    out = asyncio.run(gs.resolve_location_query(_FakeSession([]), "software engineer"))
    assert out is None


# --- alumni_location_filter --------------------------------------------------


def test_alumni_location_filter_empty_is_false():
    sql = str(
        gs.alumni_location_filter([]).compile(
            dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}
        )
    ).lower()
    assert "false" in sql


def test_alumni_location_filter_builds_tuple_in_with_state_fold():
    expr = gs.alumni_location_filter([("provo", "UT"), ("orem", "UT")])
    sql = str(
        expr.compile(
            dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}
        )
    ).lower()
    # Normalized city + folded state code compared as a tuple membership test.
    assert "lower" in sql
    assert "trim" in sql
    assert "case" in sql            # full-name -> code fold
    assert "in (" in sql
    assert "'provo'" in sql
    assert "'ut'" in sql
