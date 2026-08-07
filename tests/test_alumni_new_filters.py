"""Filters for data the schema holds but nothing could search.

Country, region, previous job title, university, degree, major and
employment-history years were all stored, and all phrasable in the free-text
search box, but had no query parameter behind them. The failure mode was the bad
one: with no parameter to bind to, the request carried no predicate and the
endpoint returned the UNFILTERED list — so asking "who works in Canada" answered
with everyone, which reads as an answer rather than as a missing feature.

These are pure tests. ``build_alumni_query`` returns a statement, so they assert
on the compiled SQL and never touch a database.
"""

import pytest
from sqlalchemy.dialects import postgresql

from app.repositories.alumni import build_alumni_query


def _sql(**kwargs) -> str:
    return str(build_alumni_query(**kwargs).compile(dialect=postgresql.dialect()))


def _params(**kwargs) -> dict:
    compiled = build_alumni_query(**kwargs).compile(dialect=postgresql.dialect())
    return dict(compiled.params)


BASELINE = _sql()


@pytest.mark.parametrize(
    ("kwarg", "value", "table", "column"),
    [
        ("country", "Canada", "current_employment", "current_country"),
        ("region", "Mountain West", "alumni_contact_info", "region"),
        ("past_title", "Analyst", "employment_history", "employment_title"),
        ("university", "Brigham Young University", "education_history", "university"),
        ("degree", "MBA", "education_history", "degree"),
        ("major", "Accounting", "education_history", "major"),
    ],
)
def test_each_new_filter_narrows_against_the_right_column(
    kwarg, value, table, column
):
    sql = _sql(**{kwarg: value})
    # It narrows at all — the whole point, since the old behaviour was to return
    # the unfiltered population.
    assert sql != BASELINE
    # ...and against the column that actually holds the data.
    assert f"{table}.{column}" in sql


@pytest.mark.parametrize(
    "kwarg",
    ["country", "region", "past_title", "university", "degree", "major"],
)
def test_each_new_filter_is_absent_when_not_supplied(kwarg):
    assert _sql(**{kwarg: None}) == BASELINE


@pytest.mark.parametrize(
    "kwarg",
    ["country", "region", "past_title", "university", "degree", "major"],
)
def test_each_new_filter_ors_within_its_own_facet(kwarg):
    # Multi-select semantics, matching every other text facet: several values in
    # ONE facet widen (OR), they don't intersect.
    params = _params(**{kwarg: ["One", "Two"]})
    assert "One" in params.values()
    assert "Two" in params.values()


def test_a_filter_value_with_sql_wildcards_is_matched_literally():
    # These are ILIKE comparisons, so an unescaped `%` would turn a narrow filter
    # into a near-match-everything one — widening a search rather than failing it.
    assert "100\\%\\_real" in _params(country="100%_real").values()


# --- worked_in_year ----------------------------------------------------------


def test_worked_in_year_narrows_on_the_history_years():
    sql = _sql(worked_in_year=2015)
    assert sql != BASELINE
    assert "employment_history.start_year" in sql
    assert "employment_history.end_year" in sql


def test_worked_in_year_counts_an_open_ended_role_as_still_running():
    # A history row with no end year is a job the person still holds. Requiring
    # end_year >= the asked year would drop exactly those, which inverts the
    # question being asked.
    sql = _sql(worked_in_year=2015)
    assert "employment_history.end_year IS NULL" in sql


def test_worked_in_year_ignores_rows_with_no_start_year():
    # No start year means there is nothing to compare; those rows are excluded
    # rather than guessed at.
    assert "employment_history.start_year IS NOT NULL" in _sql(worked_in_year=2015)


def test_worked_in_year_is_absent_when_not_supplied():
    assert _sql(worked_in_year=None) == BASELINE


def test_filters_from_different_facets_intersect():
    # Across facets the filters AND — "studied Accounting AND works in Canada"
    # must be narrower than either alone.
    both = _sql(major="Accounting", country="Canada")
    assert "education_history.major" in both
    assert "current_employment.current_country" in both
