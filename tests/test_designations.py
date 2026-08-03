"""The one predicate for "does this alumnus hold the CFA/CFP/CPA?".

``alumni_program_engagement.cfa/cfp/cpa_designation`` are varchar, not booleans,
and the intake sheet's headers say "(Yes/No)" — so a human can type "No" straight
into a column the rest of the app reads as a presence flag. These tests pin the
shared predicate and its SQL twin so the import path, the list filter and the
survey pre-fill can never disagree about what a negative means.
"""

import pytest
from sqlalchemy.dialects import postgresql

from app.core.dropdowns import (
    DESIGNATION_NEGATIVES,
    holds_designation,
    normalize_designation,
)
from app.repositories.alumni import build_alumni_query

# --- the Python predicate ----------------------------------------------------


@pytest.mark.parametrize(
    "value",
    [
        None,
        "",
        "   ",
        "\t\n",
        "No",
        "no",
        "NO",
        "  No  ",
        "n",
        "N",
        "false",
        "FALSE",
        "f",
        "0",
        "None",
        "n/a",
        "N/A",
        "na",
        "not applicable",
        "unknown",
        "Undeclared",
        "-",
        "--",
        "nope",
    ],
)
def test_negative_values_do_not_hold(value):
    assert holds_designation(value) is False
    assert normalize_designation(value) is None


@pytest.mark.parametrize(
    "value",
    [
        "CFA",
        "CFP",
        "CPA",
        "Yes",
        "yes",
        "Y",
        "true",
        "1",
        "CPA (Utah)",
        "CFA all 3 levels",
    ],
)
def test_affirmative_values_hold(value):
    assert holds_designation(value) is True


def test_in_progress_values_count_as_held():
    # DELIBERATE: "CFP Level 1" is real production data and "CFA Level II
    # Candidate" is the shape to expect next. Whether a candidate "holds" the
    # designation is an open product question (Jake has not decided as of
    # 2026-08), so in-progress text is NOT interpreted — it falls through as
    # non-negative == held, exactly as it behaves today. If this test starts
    # failing, someone answered that question; make sure they meant to.
    assert holds_designation("CFP Level 1") is True
    assert holds_designation("CFA Level II Candidate") is True


def test_negatives_are_whole_value_matches_not_substrings():
    # "No CFA yet" contains "no" but is prose, not a negative marker. Guessing at
    # prose is how you silently drop real holders.
    assert holds_designation("No CFA yet") is True
    assert holds_designation("Not a CFA") is True
    assert holds_designation("Nope, passed level 1") is True


def test_normalize_trims_a_held_value():
    assert normalize_designation("  CFA  ") == "CFA"
    assert normalize_designation("CFP Level 1") == "CFP Level 1"


def test_negatives_are_stored_lower_case_and_trimmed():
    # The set is matched against `value.strip().lower()`, so any entry carrying
    # upper case or padding would be unreachable.
    for token in DESIGNATION_NEGATIVES:
        assert token == token.strip().lower(), token
        assert token != "", "blank is handled before the set lookup"


# --- the SQL twin ------------------------------------------------------------


def _sql(stmt) -> str:
    """Compile with literal binds so the NOT IN list is visible in the string."""
    return str(
        stmt.compile(
            dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}
        )
    )


@pytest.mark.parametrize(
    "kwargs",
    [
        {"cfa": True},
        {"designations": ["CFA"]},
        {"q": "CFA"},
    ],
)
def test_sql_filter_excludes_negative_values(kwargs):
    # Every path that asks "holds the CFA?" must exclude the negatives in SQL —
    # this runs over 8,000+ alumni, so it can never become a Python-side filter.
    sql = _sql(build_alumni_query(**kwargs))
    assert "cfa_designation IS NOT NULL" in sql
    assert "lower(trim(alumni_program_engagement.cfa_designation)) NOT IN" in sql
    assert "'no'" in sql
    assert "'false'" in sql
    assert "'0'" in sql
    # Blank/whitespace-only is folded into the same NOT IN.
    assert "''" in sql


def test_sql_filter_negatives_match_the_python_predicate():
    # One source of truth: every token the Python predicate rejects is in the
    # SQL exclusion list (plus the empty string for blank cells).
    sql = _sql(build_alumni_query(cfa=True))
    clause = sql.split("NOT IN (", 1)[1].split(")", 1)[0]
    rendered = {part.strip().strip("'") for part in clause.split(",")}
    assert DESIGNATION_NEGATIVES <= rendered
    assert "" in rendered


def test_sql_filter_absent_when_no_designation_asked_for():
    sql = _sql(build_alumni_query())
    assert "cfa_designation" not in sql
    assert "NOT IN" not in sql
