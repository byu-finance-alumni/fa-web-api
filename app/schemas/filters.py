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
    industries: list[str]
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
