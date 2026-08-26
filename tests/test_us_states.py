"""Unit tests for the US state name/code crosswalk (app.core.us_states)."""

import sqlalchemy as sa

from app.core.us_states import (
    STATE_NAME_BY_CODE,
    is_us_country,
    to_code,
    to_full_name,
    us_state_full_name_expr,
)


def test_all_51_round_trip_code_to_name_to_code():
    # Every code -> full name -> code round-trips for all 50 states + DC.
    assert len(STATE_NAME_BY_CODE) == 51
    for code, name in STATE_NAME_BY_CODE.items():
        assert to_full_name(code) == name
        assert to_code(name) == code
        # code -> code, name -> name are both stable.
        assert to_code(code) == code
        assert to_full_name(name) == name


def test_to_full_name_accepts_code_any_case():
    assert to_full_name("ut") == "Utah"
    assert to_full_name("UT") == "Utah"
    assert to_full_name("  ut  ") == "Utah"


def test_to_full_name_accepts_name_any_case():
    assert to_full_name("utah") == "Utah"
    assert to_full_name("UTAH") == "Utah"
    assert to_full_name("new york") == "New York"
    assert to_full_name("District Of Columbia") == "District of Columbia"


def test_to_full_name_unknown_passthrough_trimmed():
    # Non-US values pass through, trimmed but otherwise untouched.
    assert to_full_name("  Narnia ") == "Narnia"
    assert to_full_name("ON") == "ON"  # not a US state code
    assert to_full_name("Ontario") == "Ontario"


def test_to_full_name_none_and_empty():
    assert to_full_name(None) is None
    assert to_full_name("") is None
    assert to_full_name("   ") is None


def test_to_code_accepts_name_and_code():
    assert to_code("Utah") == "UT"
    assert to_code("utah") == "UT"
    assert to_code("ut") == "UT"
    assert to_code("UT") == "UT"
    assert to_code("New York") == "NY"


def test_to_code_unknown_and_none():
    assert to_code(None) is None
    assert to_code("") is None
    assert to_code("Narnia") is None
    assert to_code("ON") is None  # Ontario is not a US state


# --- #754: the SQL fold behind the dashboard's state count -------------------
#
# These run the REAL expression against a REAL (in-memory SQLite) database
# rather than asserting on a compiled SQL string. That matters here: the bug
# being pinned — "Across 70 states" — shipped green precisely because every test
# that touched it inspected SQL text against a stubbed session and never once
# asked the database what the query actually returns.


def _state_fold(values: list[str | None]) -> tuple[int, list[str]]:
    """Insert *values* into a scratch table, then return
    ``(COUNT(DISTINCT folded), sorted distinct non-null folded values)``."""
    engine = sa.create_engine("sqlite://")
    metadata = sa.MetaData()
    table = sa.Table("scratch", metadata, sa.Column("state", sa.String(100)))
    metadata.create_all(engine)
    folded = us_state_full_name_expr(table.c.state)
    with engine.begin() as conn:
        conn.execute(table.insert(), [{"state": v} for v in values])
        count = conn.scalar(sa.select(sa.func.count(sa.func.distinct(folded))))
        names = list(
            conn.execute(
                sa.select(folded).where(folded.is_not(None)).distinct()
            ).scalars()
        )
    return int(count), sorted(names)


def test_state_fold_counts_ut_utah_and_a_non_us_region_as_one_state():
    """THE #754 REGRESSION, in one assertion.

    The tile read "Across 70 states" because `lower(trim(...))` made "UT" and
    "Utah" two states and nothing excluded "Ontario" from being a third. All
    three rows describe ONE state between them, and Ontario is not one.
    """
    count, names = _state_fold(["UT", "Utah", "Ontario"])
    assert count == 1
    assert names == ["Utah"]


def test_state_fold_ignores_casing_padding_blanks_and_nulls():
    count, names = _state_fold(
        ["  ut  ", "UTAH", "utah", "Utah", "", "   ", None]
    )
    assert count == 1
    assert names == ["Utah"]


def test_state_fold_yields_null_for_every_non_us_value():
    # Non-US regions, cities and junk all collapse to NULL, which is what keeps
    # them out of a COUNT(DISTINCT) — they are not "one other state" either.
    count, names = _state_fold(["Ontario", "London", "Bavaria", "ZZ", "n/a"])
    assert count == 0
    assert names == []


def test_state_fold_can_never_exceed_51():
    """The structural half of the fix: 50 states + DC is the ceiling, whether
    the data spells them as codes, names, or both at once."""
    every_spelling = list(STATE_NAME_BY_CODE) + list(STATE_NAME_BY_CODE.values())
    count, names = _state_fold(every_spelling + ["Ontario", "Quebec", None])
    assert count == 51
    assert len(names) == 51
    assert names == sorted(STATE_NAME_BY_CODE.values())


def test_state_fold_matches_to_full_name_for_us_values():
    """The SQL expression and the Python helper must agree on US values — the
    dashboard counts with one and the write path persists with the other, so a
    divergence would make the tile disagree with the stored data."""
    for code, name in STATE_NAME_BY_CODE.items():
        _, folded = _state_fold([code])
        assert folded == [to_full_name(code)] == [name]


def test_us_country_aliases_cover_the_spellings_the_data_holds():
    for spelling in ("USA", "usa", "U.S.", "u.s.a.", "United States", "america"):
        assert is_us_country(spelling) is True
    for spelling in ("Canada", "United Kingdom", "", "US of A"):
        assert is_us_country(spelling) is False
    assert is_us_country(None) is False
