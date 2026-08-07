"""Option lists for the alumni advanced-filter panel.

Distinct values pulled from the actual data (capped) so each multi-select shows
real, selectable options. Read-only; view-access. Reflects the live data rather
than a fixed vocab, so new employers/titles/etc. appear automatically.
"""

from __future__ import annotations

from pydantic import BaseModel


class FilterOptions(BaseModel):
    employers: list[str]
    past_employers: list[str]
    titles: list[str]
    seniority_levels: list[str]
    # Split industry facets (#584): ``industries`` feeds the PRIMARY-industry
    # select and only holds primary values; ``secondary_industries`` feeds the new
    # secondary select. Previously ``industries`` was the union of both columns,
    # back when one filter matched either.
    industries: list[str]
    secondary_industries: list[str]
    # Distinct employment statuses actually on file — free text, so this includes
    # off-list legacy values alongside the canonical seven.
    employment_statuses: list[str]
    cities: list[str]
    states: list[str]
    # Work country, from the same employment record as cities/states.
    countries: list[str]
    # Derived US region (#283), read from the contact row where it is stored —
    # the same column the list filter matches, so the two can't drift.
    regions: list[str]
    # Employment- and education-history facets. These have stored data and a
    # searchable phrasing in the free-text box, but had no filter behind them,
    # so asking for them returned the whole list rather than an answer.
    past_titles: list[str]
    universities: list[str]
    degrees: list[str]
    majors: list[str]
    tags: list[str]
    status_labels: list[str]
    leadership_roles: list[str]
    survey_statuses: list[str]
    graduation_years: list[int]
    # Distinct "Class of" (Marriott) years on file — the cohort-update export can
    # pick a cohort by this OR by graduation_year.
    graduation_classes: list[int]
