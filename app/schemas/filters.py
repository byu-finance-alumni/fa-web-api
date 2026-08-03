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
    tags: list[str]
    status_labels: list[str]
    leadership_roles: list[str]
    survey_statuses: list[str]
    graduation_years: list[int]
    # Distinct "Class of" (Marriott) years on file — the cohort-update export can
    # pick a cohort by this OR by graduation_year.
    graduation_classes: list[int]
