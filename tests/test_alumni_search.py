"""Tests for the alumni search/filter query builder.

Pure unit tests: ``build_alumni_query`` is compiled to Postgres SQL and the
clauses are asserted — no database needed.
"""

from sqlalchemy.dialects import postgresql

from app.repositories.alumni import build_alumni_query


def _sql(stmt) -> str:
    return str(stmt.compile(dialect=postgresql.dialect()))


def test_default_excludes_archived():
    sql = _sql(build_alumni_query())
    assert "WHERE" in sql
    assert "archived IS false" in sql


def test_include_archived_has_no_where():
    sql = _sql(build_alumni_query(include_archived=True))
    assert "WHERE" not in sql


def test_q_searches_six_columns_with_ilike():
    sql = _sql(build_alumni_query(q="smith"))
    # names (4) + byu_id + net_id
    assert sql.count("ILIKE") == 6


def test_graduation_year_exact():
    sql = _sql(build_alumni_query(graduation_year=2018))
    assert "graduation_year =" in sql


def test_grad_year_range():
    sql = _sql(build_alumni_query(grad_year_min=2015, grad_year_max=2020))
    assert "graduation_year >=" in sql
    assert "graduation_year <=" in sql


def test_deceased_filter():
    sql = _sql(build_alumni_query(deceased=True))
    assert "deceased IS true" in sql


def test_filters_combine():
    sql = _sql(build_alumni_query(q="lee", graduation_year=2019, deceased=False))
    assert "ILIKE" in sql
    assert "graduation_year =" in sql
    assert "deceased IS false" in sql
    assert "archived IS false" in sql
