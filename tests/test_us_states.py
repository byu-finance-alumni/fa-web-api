"""Unit tests for the US state name/code crosswalk (app.core.us_states)."""

from app.core.us_states import (
    STATE_NAME_BY_CODE,
    to_code,
    to_full_name,
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
