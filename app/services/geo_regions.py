"""Curated US metro / region aliases for plain-English location search (#358).

A ``Region`` maps a colloquial place name people type into the alumni search box
("Bay Area", "DMV", "Greater Seattle area", "SoCal") to a concrete geographic
target the radius search can resolve:

  * ``center`` + ``radius_miles`` — a great-circle catchment around a hub city, OR
  * ``cities`` — an explicit set of ``(city_norm, state)`` keys, OR
  * both, which are UNIONed by the resolver in :mod:`app.services.geo_search`.

Keys line up with the ``city_geo`` crosswalk (and therefore with how alumni rows
join to it): ``city_norm`` is ``lower(trim(city))`` and ``state`` is the 2-letter
uppercase code. The list is intentionally small and data-only so it's trivial to
extend — add a ``Region`` and list its spellings in ``_REGION_SPELLINGS``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# A city_geo natural key: (lower(trim(city)), 2-letter uppercase state code).
CityKey = tuple[str, str]


@dataclass(frozen=True)
class Region:
    """A resolved region target.

    ``label`` is the human-readable interpretation shown back to the user.
    ``center``/``radius_miles`` describe a catchment; ``cities`` is an optional
    explicit key set. At least one of ``center`` or ``cities`` is always set.
    """

    label: str
    center: tuple[float, float] | None = None
    radius_miles: float | None = None
    cities: tuple[CityKey, ...] | None = None


# San Francisco Bay Area — carried as BOTH a center+radius catchment and an
# explicit city set, so the resolver exercises (and unions) both paths. The
# explicit list guarantees the core peninsula/South Bay hubs are always included
# even if a city_geo row falls just outside the mileage cutoff.
_BAY_AREA_CITIES: tuple[CityKey, ...] = (
    ("san francisco", "CA"),
    ("oakland", "CA"),
    ("san jose", "CA"),
    ("berkeley", "CA"),
    ("palo alto", "CA"),
    ("mountain view", "CA"),
    ("sunnyvale", "CA"),
    ("santa clara", "CA"),
    ("fremont", "CA"),
    ("san mateo", "CA"),
    ("redwood city", "CA"),
    ("menlo park", "CA"),
    ("cupertino", "CA"),
    ("hayward", "CA"),
    ("walnut creek", "CA"),
    ("south san francisco", "CA"),
)


# (Region, [spellings]) — every spelling is normalized (see ``_normalize``) and
# mapped to its Region in ``REGION_ALIASES``. Coordinates are the hub city's
# lat/lng; radii are hand-tuned to the metro's commuting footprint.
_REGION_SPELLINGS: tuple[tuple[Region, tuple[str, ...]], ...] = (
    (
        Region("Bay Area, CA", (37.7749, -122.4194), 45.0, _BAY_AREA_CITIES),
        ("bay area", "sf bay area", "san francisco bay area", "the bay", "the bay area"),
    ),
    (
        Region("Greater Seattle, WA", (47.6062, -122.3321), 40.0),
        ("greater seattle", "greater seattle area", "seattle area", "seattle metro",
         "puget sound"),
    ),
    (
        Region("Greater Boston, MA", (42.3601, -71.0589), 35.0),
        ("greater boston", "greater boston area", "boston area", "boston metro"),
    ),
    (
        Region("DC / DMV Area", (38.9072, -77.0369), 40.0),
        ("dmv", "the dmv", "dc area", "dc metro", "d c area", "washington dc area",
         "washington metro", "national capital region"),
    ),
    (
        Region("Southern California", (34.0522, -118.2437), 90.0),
        ("socal", "southern california", "so cal"),
    ),
    (
        Region("Northern California", (37.7749, -122.4194), 95.0),
        ("norcal", "northern california", "nor cal"),
    ),
    (
        Region("New York Metro, NY", (40.7128, -74.0060), 50.0),
        ("nyc metro", "new york metro", "nyc metro area", "new york city metro",
         "tri state area", "greater new york", "nyc area"),
    ),
    (
        Region("Greater Los Angeles, CA", (34.0522, -118.2437), 45.0),
        ("greater la", "la metro", "los angeles metro", "los angeles area",
         "greater los angeles", "la area"),
    ),
    (
        Region("Dallas-Fort Worth, TX", (32.7767, -96.7970), 45.0),
        ("dfw", "dallas fort worth", "dallas-fort worth", "dallas fort worth metroplex",
         "the metroplex", "dallas area"),
    ),
    (
        Region("Greater Chicago, IL", (41.8781, -87.6298), 45.0),
        ("chicagoland", "greater chicago", "chicago area", "chicago metro"),
    ),
    (
        Region("Greater Houston, TX", (29.7604, -95.3698), 45.0),
        ("greater houston", "houston area", "houston metro"),
    ),
    (
        Region("Greater Atlanta, GA", (33.7490, -84.3880), 45.0),
        ("greater atlanta", "atlanta metro", "atlanta area", "metro atlanta"),
    ),
    (
        Region("Wasatch Front, UT", (40.7608, -111.8910), 45.0),
        ("wasatch front", "greater salt lake", "salt lake area", "slc metro",
         "greater salt lake city"),
    ),
    (
        Region("Utah County, UT", (40.2338, -111.6585), 25.0),
        ("utah county", "provo area", "happy valley", "provo orem", "provo-orem"),
    ),
    (
        Region("Greater Denver, CO", (39.7392, -104.9903), 45.0),
        ("greater denver", "denver metro", "denver area", "front range"),
    ),
    (
        Region("Twin Cities, MN", (44.9778, -93.2650), 40.0),
        ("twin cities", "minneapolis st paul", "minneapolis-st paul", "msp",
         "minneapolis area"),
    ),
    (
        Region("South Florida", (25.7617, -80.1918), 55.0),
        ("south florida", "greater miami", "miami metro", "miami area", "sofla"),
    ),
    (
        Region("Greater Phoenix, AZ", (33.4484, -112.0740), 45.0),
        ("greater phoenix", "phoenix metro", "phoenix area", "valley of the sun",
         "the valley"),
    ),
)


def _normalize(text: str) -> str:
    """Fold a place phrase to a canonical alias key.

    Lower-cases, drops punctuation (so "D.C. Area" == "dc area"), and collapses
    runs of whitespace. Used both to build ``REGION_ALIASES`` and to look up an
    incoming query against it.
    """
    text = text.lower().strip()
    text = re.sub(r"[.,]", " ", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


# Normalized alias string -> Region.
REGION_ALIASES: dict[str, Region] = {
    _normalize(spelling): region
    for region, spellings in _REGION_SPELLINGS
    for spelling in spellings
}


def lookup_region(place: str | None) -> Region | None:
    """Return the ``Region`` a place phrase names, or ``None`` if it's not an alias."""
    if not place:
        return None
    return REGION_ALIASES.get(_normalize(place))
