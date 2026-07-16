"""US state -> region crosswalk (50 states + DC -> the 6 alumni regions).

Single source of truth for deriving an alum's ``region`` from the state they
WORK in (``career.current_state``). Per issue #283 the employment state drives
region: for anyone who lives and works in different states, region now means
*where they work*, not *where they live*. The field still physically lives on the
contact/residence row (``AlumniContactInfo.region``) — only its meaning changed.

The six regions: Northeast, Southeast, Midwest, Southwest, West, Mountain West.

"Mountain West" was added as a 6th region on 2026-07-16 at the stakeholder's
direction. This is a BYU database, so the Mountain states are not a long tail —
they are the bulk of it (on dev, 84 of the 122 "West" alumni worked in Utah,
Colorado or Nevada), and folding them into ``West`` lumped roughly a third of all
alumni in with California. ``West`` is now the Pacific coast + AK/HI only.

Existing records were deliberately NOT backfilled: region is user-overridable and
a bulk rewrite could stomp deliberate overrides, so a stored region only moves
when someone edits that alum's work state. Until then a Utah alum keeps reading
"West" even though this map now says "Mountain West".

Keys are the canonical FULL state names from :mod:`app.core.us_states`, because
the hygiene cleaner normalizes state values to full names (``to_full_name``)
before this map is consulted. Lookup is case-insensitive and also accepts a
2-letter code, so callers don't have to normalize first.

NOT to be confused with :mod:`app.services.geo_regions`, which maps metro /
catchment aliases ("Bay Area", "DMV") to lat/lng for free-text location search.
Unrelated.
"""

from __future__ import annotations

from app.core.us_states import to_full_name

# The six valid regions (matches the intake sheet's "Region (Northeast,
# Southeast, Midwest, Southwest, West, and Mountain West)" column).
NORTHEAST = "Northeast"
SOUTHEAST = "Southeast"
MIDWEST = "Midwest"
SOUTHWEST = "Southwest"
WEST = "West"
MOUNTAIN_WEST = "Mountain West"

# ORDER IS THE DISPLAY ORDER: GET /vocabulary/state-regions serves this tuple as
# ``regions`` and the frontend sources the Region dropdown's options from it, so
# Mountain West goes last (appended, not alphabetized) rather than reshuffling an
# order staff already read.
REGIONS: tuple[str, ...] = (
    NORTHEAST,
    SOUTHEAST,
    MIDWEST,
    SOUTHWEST,
    WEST,
    MOUNTAIN_WEST,
)

# Region -> its member states. Grouped as region -> [states] (rather than the
# flat state -> region the lookup needs) because that is the shape a human
# reviews and confirms; the flat index is derived from it below.
STATES_BY_REGION: dict[str, tuple[str, ...]] = {
    NORTHEAST: (
        "Connecticut",
        "Maine",
        "Massachusetts",
        "New Hampshire",
        "New Jersey",
        "New York",
        "Pennsylvania",
        "Rhode Island",
        "Vermont",
    ),
    SOUTHEAST: (
        "Alabama",
        "Arkansas",
        "Delaware",
        "District of Columbia",
        "Florida",
        "Georgia",
        "Kentucky",
        "Louisiana",
        "Maryland",
        "Mississippi",
        "North Carolina",
        "South Carolina",
        "Tennessee",
        "Virginia",
        "West Virginia",
    ),
    MIDWEST: (
        "Illinois",
        "Indiana",
        "Iowa",
        "Kansas",
        "Michigan",
        "Minnesota",
        "Missouri",
        "Nebraska",
        "North Dakota",
        "Ohio",
        "South Dakota",
        "Wisconsin",
    ),
    SOUTHWEST: (
        "Arizona",
        "New Mexico",
        "Oklahoma",
        "Texas",
    ),
    WEST: (
        "Alaska",
        "California",
        "Hawaii",
        "Oregon",
        "Washington",
    ),
    MOUNTAIN_WEST: (
        "Colorado",
        "Idaho",
        "Montana",
        "Nevada",
        "Utah",
        "Wyoming",
    ),
}

# Flat lookup index: lower-cased canonical full state name -> region.
_REGION_BY_STATE: dict[str, str] = {
    state.lower(): region
    for region, states in STATES_BY_REGION.items()
    for state in states
}


def region_for_state(value: str | None) -> str | None:
    """Return the region for a US state, or ``None`` when it isn't derivable.

    Accepts a canonical full name ("Utah"), any casing ("utah", "UTAH"), or a
    2-letter code ("UT") — the value is run through
    :func:`app.core.us_states.to_full_name` first.

    ``None`` is returned for a blank value and for anything that isn't one of the
    50 states + DC (e.g. "Ontario", "London"). Callers treat ``None`` as "leave
    the stored region alone" — never as "clear it".
    """
    full_name = to_full_name(value)
    if full_name is None:
        return None
    return _REGION_BY_STATE.get(full_name.lower())
