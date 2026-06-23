"""Tests for the alumni search/filter query builder.

Pure unit tests: ``build_alumni_query`` is compiled to Postgres SQL and the
clauses are asserted — no database needed.
"""

import datetime

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


def test_city_filter():
    sql = _sql(build_alumni_query(city="Provo"))
    # Correlated EXISTS against the contact-info table, case-insensitive.
    assert "EXISTS" in sql
    assert "NOT (EXISTS" not in sql
    assert "alumni_contact_info" in sql
    assert "city ILIKE" in sql


def test_tag_filter():
    sql = _sql(build_alumni_query(tag="Speaker"))
    # EXISTS over the alumni_tags join to the tags lookup, matched case-insensitively
    # on the tag label — generic, accepts any tag value.
    assert "EXISTS" in sql
    assert "NOT (EXISTS" not in sql
    assert "alumni_tags" in sql
    assert "tags" in sql
    assert "tag_name ILIKE" in sql


def test_city_and_tag_compose_with_archived_default():
    sql = _sql(build_alumni_query(city="Provo", tag="Highly Engaged"))
    assert "archived IS false" in sql
    assert "city ILIKE" in sql
    assert "tag_name ILIKE" in sql


def test_attended_event_filter():
    sql = _sql(build_alumni_query(attended_event=True))
    assert "EXISTS" in sql
    assert "event_attendance" in sql


def test_spoke_window_filter_matches_dashboard_kpi():
    # The "Guest speakers this month" deep-link: alumni who served as a speaker
    # at an event in the window. Must mirror the dashboard KPI's predicate
    # (attendance_status ILIKE '%speaker%' joined to events on event_date), NOT
    # the alumnus-level guest_speaker_willing flag.
    sql = _sql(
        build_alumni_query(
            spoke_after=datetime.date(2026, 6, 1),
            spoke_before=datetime.date(2026, 6, 30),
        )
    )
    assert "EXISTS" in sql
    assert "event_attendance" in sql
    assert "events" in sql
    assert "attendance_status ILIKE" in sql
    assert "event_date >=" in sql
    assert "event_date <=" in sql
    # It is the event-participation predicate, not the willing flag.
    assert "guest_speaker_willing" not in sql


def test_spoke_filter_absent_by_default():
    sql = _sql(build_alumni_query())
    assert "attendance_status" not in sql


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


# --- #37 advanced filters: multi-select + new fields -------------------------


def test_employer_multi_value_is_or_of_ilikes():
    sql = _sql(build_alumni_query(employer=["Goldman", "JPMorgan"]))
    # Two values -> OR of two case-insensitive matches inside one EXISTS.
    assert sql.count("current_employer ILIKE") == 2
    assert " OR " in sql


def test_employer_single_string_still_works():
    # Back-compat: a scalar (legacy deep-link) degrades to one match.
    sql = _sql(build_alumni_query(employer="Goldman Sachs"))
    assert sql.count("current_employer ILIKE") == 1


def test_past_employer_filter():
    sql = _sql(build_alumni_query(past_employer=["Bain"]))
    assert "employment_history" in sql
    assert "employer_name ILIKE" in sql


def test_title_filter():
    sql = _sql(build_alumni_query(title=["Analyst"]))
    assert "current_title ILIKE" in sql


def test_seniority_filter():
    sql = _sql(build_alumni_query(seniority=["VP"]))
    assert "seniority_level ILIKE" in sql


def test_state_filter():
    sql = _sql(build_alumni_query(state=["UT", "CA"]))
    assert sql.count("alumni_contact_info.state ILIKE") == 2


def test_status_label_filter():
    sql = _sql(build_alumni_query(status_label=["Prospect"]))
    assert "alumni_status_labels" in sql
    assert "status_label_name ILIKE" in sql


def test_leadership_role_filter():
    sql = _sql(build_alumni_query(leadership_role=["President"]))
    assert "finance_society_leadership" in sql
    assert "leadership_role ILIKE" in sql


def test_survey_status_filter():
    sql = _sql(build_alumni_query(survey_status=["Complete"]))
    assert "surveys" in sql
    assert "survey_status ILIKE" in sql


def test_contacted_after_is_exists_on_interactions():
    sql = _sql(build_alumni_query(contacted_after=datetime.date(2025, 1, 1)))
    assert "interactions" in sql
    assert "interaction_date_time >=" in sql
    assert "NOT (EXISTS" not in sql


def test_contacted_before_is_not_exists_stale():
    sql = _sql(build_alumni_query(contacted_before=datetime.date(2025, 1, 1)))
    assert "interactions" in sql
    assert "NOT (EXISTS" in sql


def test_never_contacted_is_not_exists_any():
    sql = _sql(build_alumni_query(never_contacted=True))
    assert "interactions" in sql
    assert "NOT (EXISTS" in sql


def test_advanced_filters_absent_by_default():
    # None of the new tables appear unless their filter is set.
    sql = _sql(build_alumni_query())
    for tbl in (
        "employment_history",
        "finance_society_leadership",
        "surveys",
        "interactions",
        "alumni_status_labels",
    ):
        assert tbl not in sql
