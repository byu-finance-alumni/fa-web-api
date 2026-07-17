"""The alumni location is the WORK location (#287).

The intake sheet's address block is the EMPLOYER's address, but the importer
historically bound it to the residence record, so ``alumni_contact_info``'s
city/state/country held work locations under a residence label. There is no
residence data in this system — nothing populates one.

These are the regression tests for the READ side of the rebind: every reader of
the alumni location must key off ``current_employment`` (current_city /
current_state / current_country). They exist so the next maintainer can't
silently reintroduce the misleading binding — reading a location off
``alumni_contact_info`` reads as "where alumni LIVE", and anything built on that
premise would be wrong.

Pure unit tests: the statements/expressions are compiled to Postgres SQL and
asserted — no database needed.
"""

from sqlalchemy import select
from sqlalchemy.dialects import postgresql

from app.services import geography as geo


def _sql(element) -> str:
    return str(element.compile(dialect=postgresql.dialect())).lower()


# --- the location expressions the map groups on ------------------------------


def test_state_expression_reads_work_state():
    sql = _sql(geo._STATE)
    assert "current_employment.current_state" in sql
    assert "alumni_contact_info" not in sql


def test_city_expressions_read_work_city():
    for expr in (geo._CITY, geo._CITY_DISPLAY):
        sql = _sql(expr)
        assert "current_employment.current_city" in sql
        assert "alumni_contact_info" not in sql


def test_country_expression_reads_work_country():
    sql = _sql(geo._COUNTRY)
    assert "current_employment.current_country" in sql
    assert "alumni_contact_info" not in sql


def test_city_present_guard_reads_work_city():
    for cond in geo._CITY_PRESENT:
        sql = _sql(cond)
        assert "current_employment.current_city" in sql
        assert "alumni_contact_info" not in sql


# --- the filter conditions ---------------------------------------------------


def test_require_state_guard_reads_work_state():
    sql = " ".join(_sql(c) for c in geo._filter_conditions({}, require_state=True))
    assert "current_employment.current_state" in sql
    assert "alumni_contact_info" not in sql


def test_require_state_false_drops_the_state_guard():
    sql = " ".join(_sql(c) for c in geo._filter_conditions({}, require_state=False))
    assert "current_state" not in sql


def test_region_filter_still_reads_contact_info():
    # region is NOT an address — it's a derived catchment label that already
    # keys off the work state, so it deliberately stays on alumni_contact_info.
    sql = " ".join(
        _sql(c) for c in geo._filter_conditions({"region": "Mountain West"})
    )
    assert "alumni_contact_info.region" in sql


# --- the join shape ----------------------------------------------------------


def test_base_inner_joins_employment_and_outer_joins_contact():
    """current_employment is the location record, so it is the INNER join;
    alumni_contact_info is only consulted for ``region`` and drops to an OUTER
    join (a located alumnus with no contact row stays on the map)."""
    sql = _sql(geo._base())
    assert "join current_employment" in sql
    assert "left outer join current_employment" not in sql
    assert "left outer join alumni_contact_info" in sql


def test_city_sort_orders_by_work_city():
    sql = _sql(select(geo.Alumni.alumni_id).order_by(*geo._SORTS["city"]))
    assert "current_employment.current_city" in sql


# --- an alum with no current_employment row has no location ------------------


def test_no_employment_row_means_no_location_either_way():
    """The INNER join on current_employment is not a NEW exclusion.

    Every geography query already required either a non-empty current_state
    (require_state=True) or a non-empty current_country (the world view) — both
    NULL without an employment row — so an alumnus with no current-employment
    row was filtered out by the WHERE regardless of the join type. The inner
    join just states that fact in the FROM.
    """
    us = " ".join(_sql(c) for c in geo._filter_conditions({}, require_state=True))
    assert "current_employment.current_state is not null" in us
    # The world view's own guard (get_countries) is the country not-null check,
    # which is equally unsatisfiable without an employment row.
    assert "current_employment.current_country" in _sql(geo._COUNTRY)
