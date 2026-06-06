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


def test_missing_email_filter():
    sql = _sql(build_alumni_query(missing_email=True))
    # Correlated NOT EXISTS against the contact-info table on either email.
    assert "NOT (EXISTS" in sql
    assert "alumni_contact_info" in sql
    assert "personal_email IS NOT NULL" in sql
    assert "work_email IS NOT NULL" in sql


def test_missing_employer_filter():
    sql = _sql(build_alumni_query(missing_employer=True))
    assert "NOT (EXISTS" in sql
    assert "current_employment" in sql
    assert "current_employer IS NOT NULL" in sql


def test_duplicate_filter():
    sql = _sql(build_alumni_query(duplicate=True))
    # EXISTS (not NOT EXISTS) against duplicate_candidates on either side.
    assert "EXISTS" in sql
    assert "NOT (EXISTS" not in sql
    assert "duplicate_candidates" in sql
    assert "alumni_id_1" in sql
    assert "alumni_id_2" in sql


def test_employer_filter():
    sql = _sql(build_alumni_query(employer="Goldman Sachs"))
    assert "EXISTS" in sql
    assert "current_employment" in sql
    assert "current_employer ILIKE" in sql


def test_industry_filter_checks_primary_and_secondary():
    sql = _sql(build_alumni_query(industry="Investment Banking"))
    assert "EXISTS" in sql
    assert "current_industry ILIKE" in sql
    assert "current_industry_secondary ILIKE" in sql


def test_attended_event_filter():
    sql = _sql(build_alumni_query(attended_event=True))
    assert "EXISTS" in sql
    assert "event_attendance" in sql


def test_donor_filter():
    sql = _sql(build_alumni_query(donor=True))
    assert "EXISTS" in sql
    assert "alumni_program_engagement" in sql
    assert "piff_donor IS true" in sql


def test_mentor_and_speaker_filters():
    sql = _sql(
        build_alumni_query(mentor_willing=True, guest_speaker_willing=True)
    )
    assert "mentor_willing IS true" in sql
    assert "guest_speaker_willing IS true" in sql


def test_missing_filters_default_off():
    # Default query must not reference the related tables at all.
    sql = _sql(build_alumni_query())
    assert "alumni_contact_info" not in sql
    assert "current_employment" not in sql
    assert "duplicate_candidates" not in sql
    assert "event_attendance" not in sql
    assert "alumni_program_engagement" not in sql


def test_missing_filters_combine_with_archived_default():
    sql = _sql(build_alumni_query(missing_email=True, missing_employer=True))
    assert "archived IS false" in sql
    assert "alumni_contact_info" in sql
    assert "current_employment" in sql
