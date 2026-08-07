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
    # Name/identifier facets — kept in parity with GET /alumni so a future list-UI
    # facet on these exports the same population (they flow straight into
    # build_alumni_query via _filters_dict). The export route is full_access-only,
    # so the email/net_id enumeration concern that gates these on GET doesn't apply.
    net_id: str | None = None
    first_name: str | None = None
    last_name: str | None = None
    preferred_name: str | None = None
    email: str | None = None
    graduation_year: int | None = None
    grad_year_min: int | None = None
    grad_year_max: int | None = None
    deceased: bool | None = None
    # Gender + industry-bucket facets (#360, #351/#352), kept in parity with
    # GET /alumni so an export matches the filtered view the user is looking at.
    gender: str | None = None
    industry_group: str | None = None
    employer: list[str] | None = None
    past_employer: list[str] | None = None
    # Split industry facets (#584): ``industry`` is the PRIMARY column only and
    # ``secondary_industry`` the secondary one, matching GET /alumni — so an
    # export of a filtered view still returns exactly that view.
    industry: list[str] | None = None
    secondary_industry: list[str] | None = None
    title: list[str] | None = None
    seniority: list[str] | None = None
    employment_status: list[str] | None = None
    city: list[str] | None = None
    state: list[str] | None = None
    # Work country + derived region, and the employment/education-history facets.
    # Every one of these must exist here or an export of a view that uses it
    # silently widens back to the whole population — the #366 failure the parity
    # test below this schema guards against.
    country: list[str] | None = None
    region: list[str] | None = None
    past_title: list[str] | None = None
    university: list[str] | None = None
    degree: list[str] | None = None
    major: list[str] | None = None
    worked_in_year: int | None = Field(default=None, ge=1900, le=2200)
    # Location proximity search (#358/#366). The plain-English phrase the list
    # view sent as ``?near=`` plus its optional ``?radius=`` override — NOT a
    # resolved city set: the export re-resolves the phrase through the very same
    # ``geo_search.resolve_near`` the list route uses, so both land on the same
    # cities. Without these the export of a "near Provo" view silently widened to
    # every alumnus nationwide (#366).
    #
    # A phrase that cannot be pinpointed is a 422 on this path (the list falls
    # back to an unfiltered search and shows a "couldn't pinpoint" note; an
    # export has no such affordance, and quietly handing over a nationwide file
    # when the operator asked for one metro is a disclosure).
    near: str | None = None
    radius: float | None = Field(default=None, ge=1, le=3000)
    tag: list[str] | None = None
    status_label: list[str] | None = None
    leadership_role: list[str] | None = None
    survey_status: list[str] | None = None
    # "Needs surveying" view (#160). The export route is full_access-and-up
    # (admin tier), which is exactly the role set allowed to use this filter, so
    # no extra gating is needed here; the service derives the 2-year cutoff
    # server-side from this flag.
    needs_survey: bool = False
    contacted_after: datetime.date | None = None
    contacted_before: datetime.date | None = None
    never_contacted: bool = False
    attended_event: bool = False
    # Guest-speaker-AT-AN-EVENT window (#366) — the dashboard "Guest speakers
    # this month" deep-link's date bounds. Distinct from the alumnus-level
    # ``guest_speaker_willing`` flag below.
    spoke_after: datetime.date | None = None
    spoke_before: datetime.date | None = None
    donor: bool = False
    mentor_willing: bool = False
    guest_speaker_willing: bool = False
    cfp: bool = False
    cfa: bool = False
    cpa: bool = False
    # Designation facet (#404/#366): CFP / CFA / CPA tokens, ANY semantics. Sent
    # as-typed and validated through the SAME parser GET /alumni uses
    # (``dropdowns.parse_designation_tokens``) — an unknown token is a 422, never
    # a silently dropped predicate that would widen the export to everyone.
    designations: list[str] | None = None
    graduate_degree: bool = False
    missing_email: bool = False
    missing_employer: bool = False
    missing_phone: bool = False
    duplicate: bool = False
    # Friends/alumni split (#218). Unset -> the query builder's default
    # (alumni only), so an export mirrors the default Alumni list view. Send
    # ``true`` for alumni only, ``false`` for friends only, or ``null`` for both.
    is_alumni: bool | None = None
    include_archived: bool = False
    sort: str = "name"


class AlumniExportRequest(BaseModel):
    """Body for ``POST /alumni/export``: the chosen column keys (a non-empty
    subset of the catalog) and the active filters."""

    model_config = ConfigDict(extra="forbid")

    columns: list[str] = Field(min_length=1)
    filters: AlumniExportFilters = Field(default_factory=AlumniExportFilters)
