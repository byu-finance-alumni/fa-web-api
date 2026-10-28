"""Response schemas for the NetTreks tab (fa-web-app #887).

Amy's per-area reports: for each NetTrek area a "Hosts" list (alumni willing to
host a NetTrek whose company is in the area) and an "Area Alumni" list (everyone
whose company is in the area), plus an "Other" hosts list. Location is the
COMPANY (work) city — there is no residence data (#287).
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel


class NetTrekAreaKey(StrEnum):
    """The fixed set of NetTrek areas. ``other`` has only a Hosts report."""

    dallas = "dallas"
    san_francisco = "san_francisco"
    los_angeles = "los_angeles"
    phoenix = "phoenix"
    seattle = "seattle"
    washington_dc = "washington_dc"
    wasatch = "wasatch"
    other = "other"


class NetTrekReportKind(StrEnum):
    hosts = "hosts"
    area_alumni = "area_alumni"


class NetTrekReportCount(BaseModel):
    """One report link on the landing page."""

    kind: NetTrekReportKind
    # "Dallas NetTrek Hosts" / "Dallas Area Alumni".
    title: str
    count: int


class NetTrekAreaSummary(BaseModel):
    key: NetTrekAreaKey
    name: str
    hosts: NetTrekReportCount
    # ``None`` for Other, which has no Area Alumni report.
    area_alumni: NetTrekReportCount | None


class NetTrekSummary(BaseModel):
    areas: list[NetTrekAreaSummary]


class NetTrekRow(BaseModel):
    """One person on a report — the issue's columns, in order, plus the id the
    UI links to the profile with. ``gender`` is ``None`` for a caller who cannot
    edit alumni (it is a view_only-hidden field)."""

    alumni_id: int
    first_name: str | None
    middle_name: str | None
    last_name: str | None
    company: str | None
    industry: str | None
    secondary_industry: str | None
    company_city: str | None
    company_state: str | None
    permanent_email: str | None
    work_email: str | None
    phone: str | None
    linkedin_url: str | None
    gender: str | None
    graduation_year: int | None
    willing_to_host: bool


class NetTrekReportPage(BaseModel):
    area: NetTrekAreaKey
    area_name: str
    kind: NetTrekReportKind
    title: str
    items: list[NetTrekRow]
    total: int
    limit: int
    offset: int
