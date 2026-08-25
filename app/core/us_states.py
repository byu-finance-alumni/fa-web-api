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

Plus the pieces callers need to answer "is this free-text value even in the US?",
which the two folds above deliberately do NOT answer on their own (``to_full_name``
passes a non-US value straight through):
  * :data:`US_STATE_NAMES` — the 50 states + DC as canonical full names, i.e. the
    set ``to_full_name`` maps INTO. ``to_full_name(v) in US_STATE_NAMES`` is the
    "this really is a US state" test.
  * :func:`us_state_full_name_expr` — the SQL twin of ``to_full_name`` + that
    membership test in one expression: a stored value folds to its canonical full
    name, and anything non-US folds to ``NULL``. Lets a query GROUP BY / COUNT
    DISTINCT on real states without dragging rows into Python.
  * :data:`US_COUNTRY_ALIASES` / :func:`is_us_country` — every spelling of "the
    United States" the free-text country column actually holds.
"""

from __future__ import annotations

from sqlalchemy import String, case, cast, func

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

# The 50 states + DC as canonical FULL names — exactly the values ``to_full_name``
# can return for a US value. Membership in this set is the discriminator callers
# need: ``to_full_name`` returns non-US input UNCHANGED (that is its contract), so
# folding alone never tells you whether "Ontario" is a state. There are 51 members
# and there is no 52nd, which is what caps any DISTINCT count built on it.
US_STATE_NAMES: frozenset[str] = frozenset(STATE_NAME_BY_CODE.values())

# Every spelling of "the United States" the free-text country columns actually
# hold. Free text with no vocab behind it, so we accept what the intake sheet and
# the survey produce rather than demanding a canonical form. Compared
# case-insensitively (and with trailing dots ignored) by :func:`is_us_country`;
# ``app.services.geography`` compares the UPPER-cased set in SQL. Single source
# for both — a second copy is how the world map and the region deriver would end
# up disagreeing about who is abroad.
US_COUNTRY_ALIASES: frozenset[str] = frozenset(
    {
        "us",
        "u.s.",
        "u.s.a.",
        "usa",
        "united states",
        "united states of america",
        "america",
    }
)


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


def is_us_country(value: object) -> bool:
    """True when a free-text country value names the United States.

    Case-insensitive and tolerant of trailing dots, so "USA", "usa", "U.S." and
    "United States of America" all answer True. Anything else — including
    ``None``, a blank, and every genuinely foreign country — is False.
    """
    if not isinstance(value, str):
        return False
    return value.strip().lower().rstrip(".") in {
        alias.rstrip(".") for alias in US_COUNTRY_ALIASES
    }


def us_state_full_name_expr(col):
    """SQL expression: a stored state value folded to its canonical FULL name,
    or ``NULL`` when the value is not one of the 50 states + DC.

    The SQL twin of ``to_full_name(v) if to_full_name(v) in US_STATE_NAMES``, and
    the reason it exists is that BOTH halves matter and each is useless alone:

    * folding is what makes "UT" and "Utah" one state rather than two — the
      column is free text and both spellings are in the data;
    * the ``NULL`` for anything unrecognized is what keeps "Ontario" and "London"
      out of a state count. ``COUNT(DISTINCT ...)`` skips NULLs and ``GROUP BY``
      buckets them together for a single ``IS NOT NULL`` filter to drop, so one
      expression gives a count and its own drill-down list the SAME definition of
      "a state". They cannot drift apart, which is the point.

    Deliberately NOT the same contract as :func:`to_full_name`, which passes a
    non-US value through untouched: that helper is for what we PERSIST (keep what
    the user typed), this one is for what we COUNT (a state or nothing).

    Evaluated in PostgreSQL, so callers keep aggregating in the database instead
    of pulling rows into the app. Compiles to a CASE over the crosswalk — the
    same shape as ``app.services.geography._state_code_expr``, which folds to
    codes for the ``city_geo`` joins instead of to names for display.
    """
    lowered = func.lower(func.trim(cast(col, String)))
    whens = []
    for code, name in STATE_NAME_BY_CODE.items():
        whens.append((lowered == code.lower(), name))
        whens.append((lowered == name.lower(), name))
    # No ``else_``: unmatched (non-US, blank, junk) yields SQL NULL by default.
    return case(*whens)
