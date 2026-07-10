"""Canonical US state name/code crosswalk.

Single source of truth for mapping between 2-letter US state (+ DC) codes and
their full display names. Alumni state values are now STORED as the full name
(e.g. "Utah"), while the geography/map SQL folds a stored full name back to its
2-letter code at query time (so the ``city_geo`` crosswalk joins still work).

Two helpers back the two directions:
  * :func:`to_full_name` — accepts a code OR a full name in any casing and
    returns the canonical full name. Unknown / non-US values pass through
    trimmed; ``None``/empty return ``None``. Used by the hygiene cleaner so what
    we persist is a consistent full name.
  * :func:`to_code` — accepts a full name OR a code and returns the 2-letter
    code, or ``None`` if the value isn't a US state. Used by the query layer to
    fold stored values back to codes.
"""

from __future__ import annotations

# 2-letter code -> canonical full display name (50 states + DC).
STATE_NAME_BY_CODE: dict[str, str] = {
    "AL": "Alabama",
    "AK": "Alaska",
    "AZ": "Arizona",
    "AR": "Arkansas",
    "CA": "California",
    "CO": "Colorado",
    "CT": "Connecticut",
    "DE": "Delaware",
    "DC": "District of Columbia",
    "FL": "Florida",
    "GA": "Georgia",
    "HI": "Hawaii",
    "ID": "Idaho",
    "IL": "Illinois",
    "IN": "Indiana",
    "IA": "Iowa",
    "KS": "Kansas",
    "KY": "Kentucky",
    "LA": "Louisiana",
    "ME": "Maine",
    "MD": "Maryland",
    "MA": "Massachusetts",
    "MI": "Michigan",
    "MN": "Minnesota",
    "MS": "Mississippi",
    "MO": "Missouri",
    "MT": "Montana",
    "NE": "Nebraska",
    "NV": "Nevada",
    "NH": "New Hampshire",
    "NJ": "New Jersey",
    "NM": "New Mexico",
    "NY": "New York",
    "NC": "North Carolina",
    "ND": "North Dakota",
    "OH": "Ohio",
    "OK": "Oklahoma",
    "OR": "Oregon",
    "PA": "Pennsylvania",
    "RI": "Rhode Island",
    "SC": "South Carolina",
    "SD": "South Dakota",
    "TN": "Tennessee",
    "TX": "Texas",
    "UT": "Utah",
    "VT": "Vermont",
    "VA": "Virginia",
    "WA": "Washington",
    "WV": "West Virginia",
    "WI": "Wisconsin",
    "WY": "Wyoming",
}

# Lower-cased full name -> 2-letter code (for name-or-code lookups on read).
CODE_BY_NAME: dict[str, str] = {
    name.lower(): code for code, name in STATE_NAME_BY_CODE.items()
}


def to_full_name(value: str | None) -> str | None:
    """Return the canonical full state name for a code OR full name (any casing).

    * ``None`` / empty (after trim) -> ``None``.
    * A recognized 2-letter code ("ut", "UT") -> its full name ("Utah").
    * A recognized full name in any casing ("utah", "UTAH") -> "Utah".
    * Anything else (non-US value) -> the trimmed input, untouched.
    """
    if value is None:
        return None
    trimmed = value.strip()
    if not trimmed:
        return None
    # Code match first (exactly two letters).
    if len(trimmed) == 2 and trimmed.isalpha():
        code = trimmed.upper()
        if code in STATE_NAME_BY_CODE:
            return STATE_NAME_BY_CODE[code]
    # Full-name match (case-insensitive).
    code = CODE_BY_NAME.get(trimmed.lower())
    if code is not None:
        return STATE_NAME_BY_CODE[code]
    return trimmed


def to_code(value: str | None) -> str | None:
    """Return the 2-letter code for a full name OR code, else ``None``.

    * A recognized full name in any casing ("Utah", "utah") -> "UT".
    * A recognized 2-letter code ("ut", "UT") -> "UT".
    * ``None`` / empty / non-US value -> ``None``.
    """
    if value is None:
        return None
    trimmed = value.strip()
    if not trimmed:
        return None
    if len(trimmed) == 2 and trimmed.isalpha():
        code = trimmed.upper()
        if code in STATE_NAME_BY_CODE:
            return code
    return CODE_BY_NAME.get(trimmed.lower())
