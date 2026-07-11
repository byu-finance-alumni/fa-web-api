"""Tests for the alumni search/filter query builder.

Pure unit tests: ``build_alumni_query`` is compiled to Postgres SQL and the
clauses are asserted — no database needed.
"""

import datetime

from sqlalchemy import literal, select
from sqlalchemy.dialects import postgresql

from app.models.alumni import Alumni
from app.models.contact import AlumniContactInfo
from app.models.employment import CurrentEmployment
from app.repositories.alumni import alumni_order_by, build_alumni_query


def _sql(stmt) -> str:
    return str(stmt.compile(dialect=postgresql.dialect()))


def _order_sql(sort: str | None) -> str:
    """Compile just the ORDER BY clause for a list ``sort`` token."""
    return _sql(select(Alumni).order_by(*alumni_order_by(sort)))


def test_default_excludes_archived():
    sql = _sql(build_alumni_query())
    assert "WHERE" in sql
    assert "archived IS false" in sql


def test_include_archived_drops_archived_guard():
    # include_archived removes the archived predicate, but the default
    # alumni-only (is_alumni=true) split still applies (#218).
    sql = _sql(build_alumni_query(include_archived=True))
    assert "archived IS false" not in sql
    assert "is_alumni IS true" in sql


def test_include_archived_and_all_kinds_has_no_where():
    # Truly no WHERE only when archived is included AND both kinds are returned.
    sql = _sql(build_alumni_query(include_archived=True, is_alumni=None))
    assert "WHERE" not in sql


def test_q_searches_seven_columns_with_ilike():
    sql = _sql(build_alumni_query(q="smith", is_alumni=None))
    # names (5: first, last, preferred, birth_name, middle) + byu_id + net_id
    assert sql.count("ILIKE") == 7


def test_q_matches_birth_name():
    # Maiden / birth name is part of the free-text name search (#216).
    sql = _sql(build_alumni_query(q="smith"))
    assert "birth_name ILIKE" in sql


def test_last_name_field_also_matches_birth_name():
    # The dedicated last-name box also matches the maiden / birth name (#216).
    sql = _sql(build_alumni_query(last_name="smith", is_alumni=None))
    assert "last_name ILIKE" in sql
    assert "birth_name ILIKE" in sql


def test_default_is_alumni_only():
    # #218: the default query is alumni-only so the Alumni page is unchanged.
    sql = _sql(build_alumni_query())
    assert "is_alumni IS true" in sql


def test_friends_only_filter():
    sql = _sql(build_alumni_query(is_alumni=False))
    assert "is_alumni IS false" in sql


def test_both_kinds_omits_is_alumni_predicate():
    # is_alumni appears in the SELECT column list; assert it's not a WHERE
    # predicate when both kinds are requested.
    sql = _sql(build_alumni_query(is_alumni=None))
    assert "is_alumni IS" not in sql


def test_per_field_search_filters_each_field():
    # Each provided field adds its own partial-match condition; blanks are ignored.
    sql = _sql(
        build_alumni_query(
            first_name="jane",
            last_name="doe",
            net_id="jd123",
            preferred_name="janie",
            email="jane@example.com",
        )
    )
    assert "first_name ILIKE" in sql
    assert "last_name ILIKE" in sql
    assert "net_id ILIKE" in sql
    assert "preferred_first_name ILIKE" in sql
    # Email matches personal OR work email via an EXISTS on the contact table.
    assert "personal_email ILIKE" in sql
    assert "work_email ILIKE" in sql


def test_per_field_blank_values_ignored():
    # Empty/whitespace-only field values must not add a WHERE clause.
    sql = _sql(build_alumni_query(first_name="", last_name="   ", net_id=None))
    assert "first_name ILIKE" not in sql
    assert "last_name ILIKE" not in sql
    assert "net_id ILIKE" not in sql


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


def test_cfa_filter():
    # CFA designation: correlated EXISTS on the program-engagement profile.
    sql = _sql(build_alumni_query(cfa=True))
    assert "EXISTS" in sql
    assert "NOT (EXISTS" not in sql
    assert "alumni_program_engagement" in sql
    assert "cfa_designation IS NOT NULL" in sql
    # Only the CFA flag is referenced, not the CPA flag.
    assert "cpa_designation" not in sql


def test_graduate_degree_filter():
    # Graduate degree lives on the alumni table (not the engagement profile), so
    # it filters directly on a non-null, non-empty graduate_degree.
    sql = _sql(build_alumni_query(graduate_degree=True))
    assert "graduate_degree IS NOT NULL" in sql
    assert "graduate_degree" in sql


def test_graduate_degree_filter_absent_by_default():
    sql = _sql(build_alumni_query())
    assert "graduate_degree IS NOT NULL" not in sql


def test_cpa_filter():
    sql = _sql(build_alumni_query(cpa=True))
    assert "EXISTS" in sql
    assert "NOT (EXISTS" not in sql
    assert "alumni_program_engagement" in sql
    assert "cpa_designation IS NOT NULL" in sql
    assert "cfa_designation" not in sql


def test_cfa_combines_with_other_filter_via_and():
    # CFA holders graduating 2018 — both predicates present, ANDed with the
    # archived-default guard.
    sql = _sql(build_alumni_query(cfa=True, graduation_year=2018))
    assert "cfa_designation IS NOT NULL" in sql
    assert "graduation_year =" in sql
    assert "archived IS false" in sql


def test_cfa_and_cpa_combine():
    # Both certifications requested -> two correlated EXISTS, ANDed (an alumnus
    # must hold both).
    sql = _sql(build_alumni_query(cfa=True, cpa=True))
    assert "cfa_designation IS NOT NULL" in sql
    assert "cpa_designation IS NOT NULL" in sql
    # Two separate correlated EXISTS, one per designation.
    assert sql.count("EXISTS (SELECT") == 2


def test_cert_filters_absent_by_default():
    sql = _sql(build_alumni_query())
    assert "cfa_designation" not in sql
    assert "cpa_designation" not in sql


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


# --- #160 "needs surveying" (DUE for the biennial survey) --------------------


def test_needs_survey_is_not_exists_recent_completion():
    # DUE = no survey COMPLETED on/after the server-computed cutoff. Expressed as
    # a correlated NOT EXISTS over surveys.completed_at >= threshold, which in one
    # predicate covers never-surveyed (no qualifying row) AND stale (>2yr old).
    cutoff = datetime.datetime(2024, 6, 23, tzinfo=datetime.UTC)
    sql = _sql(build_alumni_query(needs_survey=True, survey_due_before=cutoff))
    assert "surveys" in sql
    assert "NOT (EXISTS" in sql
    assert "completed_at IS NOT NULL" in sql
    assert "completed_at >=" in sql


def test_needs_survey_noop_without_threshold():
    # Defense-in-depth: the flag does nothing unless the caller supplied a
    # server-computed cutoff (the route owns "now"; the builder never invents it).
    sql = _sql(build_alumni_query(needs_survey=True))
    assert "surveys" not in sql


def test_needs_survey_off_by_default():
    sql = _sql(build_alumni_query())
    assert "completed_at" not in sql


def test_needs_survey_combines_with_other_filters_as_and():
    cutoff = datetime.datetime(2024, 6, 23, tzinfo=datetime.UTC)
    sql = _sql(
        build_alumni_query(
            needs_survey=True,
            survey_due_before=cutoff,
            graduation_year=2018,
        )
    )
    # AND-combined with the rest, and still archived-gated by default.
    assert "graduation_year =" in sql
    assert "completed_at >=" in sql
    assert "archived IS false" in sql


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


# --- grad-year sort direction (E3 regression guard) --------------------------
#
# The reported bug: "Grad year (oldest)" returned newest-first and vice-versa.
# These lock the ONE place the direction is decided: grad_desc must be DESC
# (newest grad year first), grad_asc must be ASC (oldest first), nulls last in
# both. If the .desc()/.asc() ever get swapped, these fail.


def test_grad_desc_is_year_descending_nulls_last():
    # "Grad year (newest)" -> most-recent graduation_year FIRST.
    sql = _order_sql("grad_desc")
    assert "ORDER BY alumni.graduation_year DESC NULLS LAST" in sql
    # Name is the deterministic tiebreaker.
    assert "last_name ASC" in sql


def test_grad_asc_is_year_ascending_nulls_last():
    # "Grad year (oldest)" -> oldest graduation_year FIRST.
    sql = _order_sql("grad_asc")
    assert "ORDER BY alumni.graduation_year ASC NULLS LAST" in sql
    assert "last_name ASC" in sql


def test_grad_sorts_are_not_swapped():
    # Belt-and-suspenders: the two directions must differ, and each must carry
    # the direction its token names (guards against a future copy-paste swap).
    # Scope the assertions to the ORDER BY clause — graduation_year also appears
    # in the SELECT column list.
    desc_order = _order_sql("grad_desc").split("ORDER BY", 1)[1]
    asc_order = _order_sql("grad_asc").split("ORDER BY", 1)[1]
    assert desc_order != asc_order
    assert "graduation_year DESC" in desc_order
    assert "graduation_year ASC" not in desc_order
    assert "graduation_year ASC" in asc_order
    assert "graduation_year DESC" not in asc_order


def test_grad_desc_final_tiebreak_is_unique_alumni_id():
    # #183: grad_desc tiebreaks on the non-unique last_name, then the UNIQUE PK.
    # Ending on alumni_id gives tied rows a total order so OFFSET paging can't
    # duplicate/skip a row across a page boundary.
    order_by = _order_sql("grad_desc").split("ORDER BY", 1)[1]
    assert "graduation_year DESC NULLS LAST" in order_by
    assert "last_name ASC" in order_by
    assert order_by.rstrip().endswith("alumni.alumni_id ASC")


def test_grad_asc_final_tiebreak_is_unique_alumni_id():
    # #183: same total-order guarantee for the ascending direction.
    order_by = _order_sql("grad_asc").split("ORDER BY", 1)[1]
    assert "graduation_year ASC NULLS LAST" in order_by
    assert "last_name ASC" in order_by
    assert order_by.rstrip().endswith("alumni.alumni_id ASC")


def test_default_and_name_sort_by_last_name():
    for token in (None, "name", "bogus", "grad_desc_typo"):
        sql = _order_sql(token)
        # Unknown/legacy tokens fall back to name; never to a grad-year order.
        order_by = sql.split("ORDER BY", 1)[1]
        assert order_by.startswith(" alumni.last_name ASC")
        assert "graduation_year" not in order_by


# --- #357 related-data sorts (industry / city / state) -----------------------
#
# These tokens order by an expression the caller supplies (a correlated scalar
# subquery in list_page). The unit test passes a plain column expression to prove
# the token uses it, tie-broken by last_name then the unique PK.


def _related_order_sql(sort, **exprs) -> str:
    return _sql(select(Alumni).order_by(*alumni_order_by(sort, **exprs)))


def test_industry_sort_orders_by_supplied_expression_nulls_last():
    expr = CurrentEmployment.current_industry
    order_by = _related_order_sql("industry", industry=expr).split("ORDER BY", 1)[1]
    assert "current_employment.current_industry ASC NULLS LAST" in order_by
    # Deterministic tiebreaks: last name then the unique PK (stable OFFSET paging).
    assert "last_name ASC" in order_by
    assert order_by.rstrip().endswith("alumni.alumni_id ASC")


def test_city_and_state_sorts_order_by_supplied_expression():
    city_order = _related_order_sql(
        "city", city=AlumniContactInfo.city
    ).split("ORDER BY", 1)[1]
    assert "alumni_contact_info.city ASC NULLS LAST" in city_order
    state_order = _related_order_sql(
        "state", state=AlumniContactInfo.state
    ).split("ORDER BY", 1)[1]
    assert "alumni_contact_info.state ASC NULLS LAST" in state_order


def test_related_sort_without_expression_falls_back_to_name():
    # A related token with no expression supplied degrades to the name order,
    # never an unfiltered/ambiguous order.
    for token in ("industry", "city", "state"):
        order_by = _order_sql(token).split("ORDER BY", 1)[1]
        assert order_by.startswith(" alumni.last_name ASC")
        assert "graduation_year" not in order_by


# --- #360 gender facet -------------------------------------------------------


def test_gender_filter_matches_first_letter_case_insensitively():
    sql = _sql(build_alumni_query(gender="F"))
    # First-letter match: upper(substr(trim(gender), 1, 1)) = 'F'.
    assert "substr(trim(alumni.gender)" in sql
    assert "upper(" in sql


def test_gender_filter_absent_by_default():
    # gender appears in the SELECT column list; assert the FIRST-LETTER match
    # predicate is what's absent by default, not the column itself.
    assert "substr(trim(alumni.gender)" not in _sql(build_alumni_query())


def test_gender_combines_with_industry_via_and():
    # #360: gender is AND-combined with the industry facet.
    sql = _sql(build_alumni_query(gender="M", industry="Investment Banking"))
    assert "substr(trim(alumni.gender)" in sql
    assert "current_industry ILIKE" in sql
    assert "archived IS false" in sql


# --- #351 / #352 industry-bucket facets --------------------------------------


def test_industry_group_unknown_is_not_exists_on_industry():
    # "unknown" = no current-employment row names a non-blank primary industry.
    sql = _sql(build_alumni_query(industry_group="unknown"))
    assert "NOT (EXISTS" in sql
    assert "current_employment" in sql
    assert "current_industry IS NOT NULL" in sql


def test_industry_group_other_excludes_finance_industries():
    # "other" = has a primary industry that isn't a canonical finance industry.
    sql = _sql(build_alumni_query(industry_group="other"))
    assert "EXISTS" in sql
    assert "current_industry IS NOT NULL" in sql
    assert "NOT IN" in sql
    assert "lower(trim(current_employment.current_industry))" in sql


def test_industry_group_absent_by_default():
    assert "current_employment" not in _sql(build_alumni_query())


# --- #358 location proximity filter ------------------------------------------


def test_location_filter_wraps_predicate_in_contact_exists():
    # The geo module supplies the (city, state) match predicate; the builder
    # correlates it to the alumnus via a contact-info EXISTS.
    sql = _sql(build_alumni_query(location_filter=literal(True)))
    assert "EXISTS" in sql
    assert "alumni_contact_info" in sql


def test_location_filter_absent_by_default():
    assert "alumni_contact_info" not in _sql(build_alumni_query())
