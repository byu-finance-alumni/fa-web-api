"""Response schemas for the Outreach interest lists.

Four landing pages under the Outreach menu — Conference Interest, Event
Speakers, Event Sponsors, Alumni Mentors — each a set of report links with a
count. Every list uses the NetTrek reports' people, sort, table and CSV, so a
list's rows are :class:`app.schemas.nettreks.NetTrekRow`.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel

from app.schemas.nettreks import NetTrekRow


class OutreachGroupKey(StrEnum):
    """One Outreach landing page."""

    conference = "conference"
    speakers = "speakers"
    sponsors = "sponsors"
    mentors = "mentors"


class OutreachListKey(StrEnum):
    """One list on a landing page. Which keys a group has is fixed by the
    backend (``outreach_lists.GROUP_LISTS``); ``industry`` (Alumni Mentors)
    also takes an ``industry`` query parameter naming the industry."""

    real_estate = "real_estate"
    corporate_finance = "corporate_finance"
    financial_services = "financial_services"
    utah = "utah"
    out_of_state = "out_of_state"
    industry = "industry"
    no_industry = "no_industry"
    women = "women"


class OutreachListLink(BaseModel):
    """One report link on a landing page."""

    key: OutreachListKey
    # The industry, for an Alumni Mentors ``industry`` link; else ``None``.
    industry: str | None
    # "Real Estate Conference Alumni Interest" / "Investment Banking Mentors".
    title: str
    count: int


class OutreachGroupSummary(BaseModel):
    group: OutreachGroupKey
    # "Conference Interest" / "Event Speakers" / ...
    title: str
    links: list[OutreachListLink]


class OutreachListPage(BaseModel):
    group: OutreachGroupKey
    group_title: str
    key: OutreachListKey
    industry: str | None
    title: str
    items: list[NetTrekRow]
    total: int
    limit: int
    offset: int
