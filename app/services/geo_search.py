"""Plain-English location search over alumni cities (#358).

Turns free text a user types into the alumni search box — "near Los Angeles,
California", "within 50 miles of Seattle", "Bay Area", "Greater Seattle area",
"DMV", or a bare "Provo, UT" — into a concrete set of city keys the alumni
search can filter on. Nothing here decides *how* alumni are listed; it only
answers "which cities does this phrase mean?".

Two layers:

  * :func:`parse_location_query` — pure text -> :class:`LocationMatch` (or
    ``None`` when the text isn't a location, so the caller falls back to normal
    search). No IO, fully unit-testable.
  * :func:`resolve_location` / :func:`city_keys_within` — async, resolve a match
    against the ``city_geo`` crosswalk into ``(city_norm, state)`` keys using a
    great-circle radius. :func:`haversine_miles` is the pure, tested distance
    helper the radius logic is built on.

Identifier contract
-------------------
A city is identified by a :data:`CityKey` = ``(city_norm, state)`` where
``city_norm = lower(trim(city))`` and ``state`` is the 2-letter uppercase code —
exactly the composite primary key of ``city_geo``. This lines up with how alumni
rows join to geography today: an alum's location is where they **WORK** —
``current_employment.current_city`` (free text) + ``.current_state`` (stored as a
full name like "Utah"). That is the employer's address, the only address this
system holds; there is no residence data (#287). To filter alumni by a resolved
key set, compare ``lower(trim(current_city))`` and the folded 2-letter state code
against the keys — :func:`alumni_location_filter` builds exactly that predicate
for the caller.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field

from sqlalchemy import ColumnElement, String, case, cast, func, literal, select, tuple_
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.us_states import CODE_BY_NAME, to_code
from app.models.employment import CurrentEmployment
from app.models.geo import CityGeo
from app.services.geo_regions import CityKey, Region, lookup_region

# Radius (miles) applied to a "near"/"around" phrase with no explicit number.
DEFAULT_RADIUS_MILES = 50.0
# Radius (miles) applied to a bare "City, State" — tighter, since naming a single
# city reads as "this city and its immediate metro", not a wide region.
BARE_CITY_RADIUS_MILES = 25.0

# Mean earth radius in miles (matches app.services.geography._EARTH_MI).
_EARTH_MI = 3958.8

# "within <N> miles of <place>"  (mi | mile | miles, integer or decimal).
_WITHIN_RE = re.compile(
    r"^\s*within\s+(?P<num>\d+(?:\.\d+)?)\s*(?:mi|mile|miles)\s+of\s+(?P<place>.+?)\s*$",
    re.IGNORECASE,
)
# STRONG locational prefixes — an unambiguous "this is a place" intent, so a
# bare city name (no state) is accepted.
_STRONG_NEAR_RE = re.compile(
    r"^\s*(?:near|around|close to|nearby)\s+(?P<place>.+?)\s*$", re.IGNORECASE
)
# WEAK locational prefix — "in <x>" is too ambiguous ("in finance") to treat a
# bare word as a city, so it only resolves when the place is a region alias or
# carries an explicit state.
_WEAK_IN_RE = re.compile(r"^\s*in\s+(?P<place>.+?)\s*$", re.IGNORECASE)


@dataclass(frozen=True)
class LocationMatch:
    """A parsed location intent.

    Attributes
    ----------
    label:
        Human-readable interpretation to echo back ("Los Angeles, CA",
        "Bay Area, CA", "Within 50 mi of Seattle").
    radius_miles:
        Catchment radius. Always set (defaults applied by the parser).
    center:
        ``(lat, lng)`` when the phrase resolves to coordinates without a DB hit
        (region aliases). ``None`` for a city/state that must be geocoded from
        ``city_geo`` at resolve time.
    city:
        Normalized city name (``lower(trim(...))``) when a city was named, else
        ``None``. Used to geocode against ``city_geo`` in the resolver.
    state:
        2-letter uppercase state code when known, else ``None``.
    cities:
        Explicit ``(city_norm, state)`` keys from a region alias, UNIONed with
        any radius result by the resolver. ``None`` when there's no explicit set.
    """

    label: str
    radius_miles: float
    center: tuple[float, float] | None = None
    city: str | None = None
    state: str | None = None
    cities: tuple[CityKey, ...] | None = field(default=None)


# --- parsing (pure) ----------------------------------------------------------


def _title_city(city_norm: str) -> str:
    """Best-effort human spelling of a normalized city for display labels."""
    return city_norm.title()


def _split_city_state(place: str) -> tuple[str, str] | None:
    """Split a place into ``(city_norm, state_code)`` when a US state is present.

    Handles "City, State" (comma form) and "City State" (trailing full-name or
    code, up to two trailing words). Returns ``None`` when no US state can be
    peeled off, so callers can decide whether a state-less phrase is a location.
    """
    place = place.strip()
    if not place:
        return None
    # Comma form: "Los Angeles, California" / "Provo, UT".
    if "," in place:
        head, _, tail = place.rpartition(",")
        code = to_code(tail.strip())
        if code and head.strip():
            return head.strip().lower(), code
        return None
    # Trailing state form: "Los Angeles California" / "New York NY". Try the last
    # two words then the last word as a state name/code.
    words = place.split()
    for take in (2, 1):
        if len(words) > take:
            candidate = " ".join(words[-take:])
            code = to_code(candidate)
            if code:
                city = " ".join(words[:-take]).strip()
                if city:
                    return city.lower(), code
    return None


def _interpret_place(
    place: str, *, radius: float | None, strong: bool
) -> LocationMatch | None:
    """Resolve an extracted place phrase into a ``LocationMatch``.

    ``radius`` is an explicit override (from "within N miles of ...") or ``None``
    to apply a default. ``strong`` marks an unambiguous locational context in
    which a bare city (no state) is accepted.
    """
    place = place.strip()
    if not place:
        return None

    region: Region | None = lookup_region(place)
    if region is not None:
        r = radius if radius is not None else (region.radius_miles or DEFAULT_RADIUS_MILES)
        return LocationMatch(
            label=region.label,
            radius_miles=float(r),
            center=region.center,
            cities=region.cities,
        )

    parsed = _split_city_state(place)
    if parsed is not None:
        city_norm, state_code = parsed
        r = radius if radius is not None else (
            DEFAULT_RADIUS_MILES if strong else BARE_CITY_RADIUS_MILES
        )
        return LocationMatch(
            label=f"{_title_city(city_norm)}, {state_code}",
            radius_miles=float(r),
            city=city_norm,
            state=state_code,
        )

    # City with no state. Only a strong locational context ("near X", "within N
    # miles of X") makes a bare word a location — otherwise it's a normal search
    # term and we return None so the caller doesn't hijack it.
    if strong:
        city_norm = place.lower()
        r = radius if radius is not None else DEFAULT_RADIUS_MILES
        return LocationMatch(
            label=_title_city(city_norm),
            radius_miles=float(r),
            city=city_norm,
        )
    return None


def parse_location_query(text: str) -> LocationMatch | None:
    """Parse free text into a :class:`LocationMatch`, or ``None`` if not a location.

    Recognizes, in order: ``within <N> miles of <place>`` (explicit radius);
    ``near/around/close to <place>`` (strong intent, default radius); ``in
    <place>`` (weak — only region aliases or explicit "City, State"); a region
    alias standing alone ("Bay Area", "DMV"); and a bare ``City, State``.
    Returns ``None`` when nothing locational is detected.
    """
    if not text or not text.strip():
        return None
    t = text.strip()

    m = _WITHIN_RE.match(t)
    if m:
        return _interpret_place(m.group("place"), radius=float(m.group("num")), strong=True)

    m = _STRONG_NEAR_RE.match(t)
    if m:
        return _interpret_place(m.group("place"), radius=None, strong=True)

    m = _WEAK_IN_RE.match(t)
    if m:
        # Weak: region alias or explicit City, State only (strong=False).
        return _interpret_place(m.group("place"), radius=None, strong=False)

    # No prefix: a region alias standing alone, or a bare "City, State".
    return _interpret_place(t, radius=None, strong=False)


# --- distance (pure) ---------------------------------------------------------


def haversine_miles(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    """Great-circle distance in miles between two lat/lng points.

    Pure helper mirroring the spherical-law-of-cosines distance the SQL radius
    filter computes (:func:`_distance_mi_expr`), so the two stay in agreement and
    the math is unit-testable without a database.
    """
    rlat1, rlat2 = math.radians(lat1), math.radians(lat2)
    dlat = math.radians(lat2 - lat1)
    dlng = math.radians(lng2 - lng1)
    a = (
        math.sin(dlat / 2) ** 2
        + math.cos(rlat1) * math.cos(rlat2) * math.sin(dlng / 2) ** 2
    )
    return _EARTH_MI * 2 * math.asin(min(1.0, math.sqrt(a)))


def _distance_mi_expr(lat: float, lng: float) -> ColumnElement[float]:
    """SQL great-circle distance (miles) from ``(lat, lng)`` to each city_geo row.

    Spherical law of cosines evaluated in PostgreSQL, with the acos argument
    clamped to [-1, 1] so float rounding can't push it out of domain. Matches
    ``app.services.geography._distance_mi``.
    """
    arg = (
        func.sin(func.radians(literal(lat))) * func.sin(func.radians(CityGeo.lat))
        + func.cos(func.radians(literal(lat)))
        * func.cos(func.radians(CityGeo.lat))
        * func.cos(func.radians(CityGeo.lng - literal(lng)))
    )
    clamped = func.least(1.0, func.greatest(-1.0, arg))
    return _EARTH_MI * func.acos(clamped)


# --- resolution (async, DB) --------------------------------------------------


async def city_keys_within(
    session: AsyncSession, lat: float, lng: float, radius_miles: float
) -> list[CityKey]:
    """``(city_norm, state)`` keys from ``city_geo`` within ``radius_miles``.

    The distance is computed in PostgreSQL against the crosswalk's coordinates.
    Returns the natural keys (not integer ids — ``city_geo`` has none; its PK is
    the composite ``(city_norm, state)``) so the caller can filter alumni whose
    ``(lower(trim(city)), state_code)`` is in the set.
    """
    rows = (
        await session.execute(
            select(CityGeo.city_norm, CityGeo.state).where(
                _distance_mi_expr(lat, lng) <= radius_miles
            )
        )
    ).all()
    return [(cn, st) for cn, st in rows]


async def _lookup_city_coords(
    session: AsyncSession, city_norm: str, state: str | None
) -> list[tuple[str, str, float, float]]:
    """``city_geo`` rows (city_norm, state, lat, lng) for a normalized city.

    Filters by ``state`` when known; otherwise returns every state's match for
    that city name (the resolver unions a radius around each, so an ambiguous
    bare city like "Springfield" yields a superset rather than picking one)."""
    stmt = select(CityGeo.city_norm, CityGeo.state, CityGeo.lat, CityGeo.lng).where(
        CityGeo.city_norm == city_norm
    )
    if state:
        stmt = stmt.where(CityGeo.state == state)
    rows = (await session.execute(stmt)).all()
    return [(cn, st, lat, lng) for cn, st, lat, lng in rows]


async def resolve_location(
    session: AsyncSession, match: LocationMatch
) -> list[CityKey]:
    """Resolve a :class:`LocationMatch` to its full set of ``(city_norm, state)`` keys.

    One-call path for the caller: unions (a) any explicit alias city set, (b) a
    radius around an alias center, and (c) a radius around the coordinates of a
    named city looked up in ``city_geo``. Returns a sorted, de-duplicated list.
    """
    result: set[CityKey] = set()

    if match.cities:
        result.update(match.cities)

    centers: list[tuple[float, float]] = []
    if match.center is not None:
        centers.append(match.center)
    elif match.city:
        for cn, st, lat, lng in await _lookup_city_coords(
            session, match.city, match.state
        ):
            result.add((cn, st))  # the named city itself is always in-set
            centers.append((lat, lng))

    for lat, lng in centers:
        result.update(await city_keys_within(session, lat, lng, match.radius_miles))

    return sorted(result)


async def resolve_location_query(
    session: AsyncSession, text: str
) -> tuple[LocationMatch, list[CityKey]] | None:
    """Parse *and* resolve in one call: ``(match, keys)`` or ``None``.

    Convenience for the alumni-search caller — returns ``None`` when the text
    isn't a location (fall back to normal search), otherwise the interpreted
    match (for echoing the label) plus the resolved key set (for filtering).
    """
    match = parse_location_query(text)
    if match is None:
        return None
    keys = await resolve_location(session, match)
    return match, keys


# --- alumni filter helper ----------------------------------------------------


def _alumni_state_code_expr() -> ColumnElement[str]:
    """Fold ``current_employment.current_state`` (the alum's WORK state, stored as
    a full name) to its 2-letter code, so it can be compared to a ``city_geo``
    key's state. Mirrors ``app.services.geography._state_code_expr``."""
    trimmed = func.trim(cast(CurrentEmployment.current_state, String))
    return case(
        *[
            (func.lower(trimmed) == full_lower, code)
            for full_lower, code in CODE_BY_NAME.items()
        ],
        else_=func.upper(trimmed),
    )


def alumni_location_filter(keys: list[CityKey]) -> ColumnElement[bool]:
    """A SQLAlchemy predicate selecting ``CurrentEmployment`` rows whose WORK
    location is one of ``keys``.

    Compares ``(lower(trim(current_city)), folded 2-letter state code)`` against
    the resolved key set with a single ``IN (tuple, ...)`` — the exact mapping
    between a ``city_geo`` key and how alumni store their location (#287). An
    empty key set yields a ``false`` predicate (matches nothing), so a location
    search that resolved to zero cities returns no alumni rather than every
    alumnus.
    """
    if not keys:
        return literal(False)
    city_norm = func.lower(func.trim(cast(CurrentEmployment.current_city, String)))
    state_code = _alumni_state_code_expr()
    return tuple_(city_norm, state_code).in_([(cn, st) for cn, st in keys])


async def resolve_near(
    session: AsyncSession,
    near: str | None,
    radius: float | None = None,
) -> tuple[ColumnElement[bool] | None, dict | None]:
    """Turn a ``near`` phrase (+ optional ``radius`` override) into a filter.

    THE single place a ``near``/``radius`` pair becomes a ``location_filter`` for
    ``build_alumni_query``. ``GET /alumni`` and ``POST /alumni/export`` both call
    this, so a list and an export of that same list can never resolve the phrase
    differently and end up on different populations (#366).

    Returns ``(location_filter, envelope)``:

    * ``(None, None)`` — no phrase given; no location predicate at all.
    * ``(predicate, {"label", "radius_miles", "resolved": True})`` — resolved.
      An empty key set yields a match-nothing predicate (see
      :func:`alumni_location_filter`), never a widened result.
    * ``(None, {"label": <phrase>, "resolved": False})`` — the phrase isn't a
      place we can pinpoint. The caller decides what that means: the list falls
      back to a normal (unfiltered-by-location) search and surfaces the flag in
      the response envelope; the export refuses, because silently exporting a
      wider population than the operator asked for is a disclosure (#366).

    A ``radius`` override is folded into the phrase so the resolved city set AND
    the human label both reflect it. If that phrasing doesn't parse (e.g. an odd
    region alias), we retry the raw phrase so a valid place still resolves; the
    override radius is then only echoed in the envelope.
    """
    if not near or not near.strip():
        return None, None
    near_text = near.strip()
    resolved = None
    if radius is not None:
        resolved = await resolve_location_query(
            session, f"within {radius:g} miles of {near_text}"
        )
    if resolved is None:
        resolved = await resolve_location_query(session, near_text)
    if resolved is None:
        return None, {"label": near_text, "resolved": False}
    match, keys = resolved
    return alumni_location_filter(keys), {
        "label": match.label,
        "radius_miles": (
            radius if radius is not None else getattr(match, "radius_miles", None)
        ),
        "resolved": True,
    }
