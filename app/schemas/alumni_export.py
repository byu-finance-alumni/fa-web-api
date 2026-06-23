"""Schemas for the customizable alumni CSV export (#33).

The export is a column-picker: the frontend fetches the column catalog
(``GET /alumni/export/columns``), lets the user choose which columns to include
(seeded with a FERPA-light default selection), and POSTs the chosen columns plus
the SAME filter set the list view uses to ``POST /alumni/export``. The backend
streams back a CSV of every alumnus matching those filters (no pagination),
restricted to the chosen columns.
"""

from __future__ import annotations

import datetime

from pydantic import BaseModel, ConfigDict, Field


class ExportColumn(BaseModel):
    """One offerable export column: a stable ``key``, a human ``label`` for the
    CSV header + the picker, and a ``group`` for sectioning the picker UI."""

    key: str
    label: str
    group: str


class ExportColumnCatalog(BaseModel):
    """The full set of exportable columns plus the default-checked selection."""

    columns: list[ExportColumn]
    default_selected: list[str]


class AlumniExportFilters(BaseModel):
    """The list view's filter set, as a body model so the export hits exactly the
    same population the user is looking at. Every field is optional; unset fields
    fall back to ``build_alumni_query``'s defaults. Mirrors the ``GET /alumni``
    query parameters one-for-one."""

    model_config = ConfigDict(extra="forbid")

    q: str | None = None
    graduation_year: int | None = None
    grad_year_min: int | None = None
    grad_year_max: int | None = None
    deceased: bool | None = None
    employer: list[str] | None = None
    past_employer: list[str] | None = None
    industry: list[str] | None = None
    title: list[str] | None = None
    seniority: list[str] | None = None
    city: list[str] | None = None
    state: list[str] | None = None
    tag: list[str] | None = None
    status_label: list[str] | None = None
    leadership_role: list[str] | None = None
    survey_status: list[str] | None = None
    contacted_after: datetime.date | None = None
    contacted_before: datetime.date | None = None
    never_contacted: bool = False
    attended_event: bool = False
    donor: bool = False
    mentor_willing: bool = False
    guest_speaker_willing: bool = False
    missing_email: bool = False
    missing_employer: bool = False
    duplicate: bool = False
    include_archived: bool = False
    sort: str = "name"


class AlumniExportRequest(BaseModel):
    """Body for ``POST /alumni/export``: the chosen column keys (a non-empty
    subset of the catalog) and the active filters."""

    model_config = ConfigDict(extra="forbid")

    columns: list[str] = Field(min_length=1)
    filters: AlumniExportFilters = Field(default_factory=AlumniExportFilters)
