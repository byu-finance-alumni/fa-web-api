"""Outreach interest lists: Conference Interest, Event Speakers, Event Sponsors,
Alumni Mentors, Case Competition Hosts and Event Help.

Each is a landing page of report links with counts, like NetTreks, built on the
willingness flags on ``alumni_program_engagement`` (one row per alum, set from
survey answers; "expressed interest" = the flag is true, and an alum with no
engagement row has expressed none):

  * **Conference Interest** (``finance_conference_willing``), split by INDUSTRY:
    Real Estate, Corporate Finance, and Financial Services = every other
    industry INCLUDING no industry, so nobody interested is left off.
  * **Event Speakers** (``guest_speaker_willing``) and **Event Sponsors**
    (``company_event_sponsor_willing``), split by WORK state: in Utah ("Utah"
    or "UT", any case/spacing) and Out of State = everyone else, including no
    work state.
  * **Alumni Mentors** (``mentor_willing``): one list per industry with at
    least one mentor, plus Industry Not Listed, plus Alumni Mentors for Women
    (``women_in_finance_mentor_willing``, whether or not ``mentor_willing``).
  * **Case Competition Hosts** (``case_competition_host_willing``) and **Event
    Help** (``help_at_event_willing``), api#682: an "All" list of everyone
    willing, then one list per dashboard FINANCE industry
    (``dropdowns.WHEEL_INDUSTRIES`` — the industries the dashboard industry
    chart gives a bar of their own), EVERY one of them even at zero, most first
    (ties A→Z, the dashboard chart's order). "Other", "Unknown", "Graduate
    Student" and the other non-wheel values are not finance industries and get
    no row; anyone in them is on the "All" list only.

Industry is the industry the alumni list DISPLAYS
(``repositories.alumni.displayed_industry``, #891: the secondary industry when
the primary is "Other" and a secondary is filled in), compared case-
insensitively. An explicit "Unknown" counts as no industry, as on the dashboard
(#295). Location is the WORK (company) state — there is no residence data.

Everything else is the NetTrek reports': the same people (``nettreks.
population_conditions``: no archived, no friends, no deceased flag, no Deceased /
Do Not Contact label), the same joins, sort, row shape and CSV columns. Counts
are computed from the SAME predicates the lists filter on, so a link's count
and the list behind it cannot disagree.
"""

from __future__ import annotations

import datetime
import logging
import re
from dataclasses import dataclass

from sqlalchemy import String, cast, false, func, not_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.dropdowns import INDUSTRIES, WHEEL_INDUSTRIES
from app.models.audit import AuditLog
from app.models.employment import CurrentEmployment
from app.models.engagement import AlumniProgramEngagement as APE
from app.repositories.alumni import displayed_industry
from app.schemas.outreach import (
    OutreachGroupKey,
    OutreachGroupSummary,
    OutreachListKey,
    OutreachListLink,
    OutreachListPage,
)
from app.services import nettreks
from app.services.report_view import ReportView, audit_safe

log = logging.getLogger(__name__)

G = OutreachGroupKey
L = OutreachListKey

#: Landing-page title per group.
GROUP_TITLES: dict[OutreachGroupKey, str] = {
    G.conference: "Conference Interest",
    G.speakers: "Event Speakers",
    G.sponsors: "Event Sponsors",
    G.mentors: "Alumni Mentors",
    G.case_competition: "Case Competition Hosts",
    G.event_help: "Event Help",
}

#: The lists each group has, in landing-page order.
GROUP_LISTS: dict[OutreachGroupKey, tuple[OutreachListKey, ...]] = {
    G.conference: (L.real_estate, L.corporate_finance, L.financial_services),
    G.speakers: (L.utah, L.out_of_state),
    G.sponsors: (L.utah, L.out_of_state),
    G.mentors: (L.industry, L.no_industry, L.women),
    G.case_competition: (L.all, L.industry),
    G.event_help: (L.all, L.industry),
}

#: The groups whose ``industry`` lists are exactly the dashboard's finance
#: industries (every one shown, zeros included; no other industry is a list).
FINANCE_INDUSTRY_GROUPS: frozenset[OutreachGroupKey] = frozenset(
    {G.case_competition, G.event_help}
)

#: Each group's per-industry list title, after the industry name.
_INDUSTRY_TITLE_SUFFIX: dict[OutreachGroupKey, str] = {
    G.mentors: "Mentors",
    G.case_competition: "Case Competition Hosts",
    G.event_help: "Alumni willing to help at a Finance Event",
}

_FIXED_TITLES: dict[tuple[OutreachGroupKey, OutreachListKey], str] = {
    (G.conference, L.real_estate): "Real Estate Conference Alumni Interest",
    (G.conference, L.corporate_finance): "Corporate Finance Conference Alumni Interest",
    (G.conference, L.financial_services): "Financial Services Conference Alumni Interest",
    (G.speakers, L.utah): "Potential Alumni Speakers in Utah",
    (G.speakers, L.out_of_state): "Potential Alumni Speakers Out of State",
    (G.sponsors, L.utah): "Potential Alumni Sponsors in Utah",
    (G.sponsors, L.out_of_state): "Potential Alumni Sponsors Out of State",
    (G.mentors, L.no_industry): "Industry Not Listed Mentors",
    (G.mentors, L.women): "Alumni Mentors for Women",
    (G.case_competition, L.all): "All Case Competition Hosts",
    (G.event_help, L.all): "All Alumni willing to help at a Finance Event",
}

#: The audit action every Outreach list CSV writes (counted in the global
#: export budget via ``rate_limit.EXPORT_AUDIT_ACTIONS``).
OUTREACH_EXPORT_ACTION = "export_outreach_list"
#: ``entity_type`` on both the view and the export audit rows.
OUTREACH_ENTITY = "outreach_list"

_INDUSTRY_BY_LOWER = {v.lower(): v for v in INDUSTRIES}
_FINANCE_BY_LOWER = {v.lower(): v for v in WHEEL_INDUSTRIES}


class NoSuchList(LookupError):
    """The group has no such list, or an industry list was asked for without an
    industry (or a non-industry list with one), or — on a Case Competition
    Hosts / Event Help page — with an industry that is not a dashboard finance
    industry."""


@dataclass(frozen=True)
class ListRef:
    """One list: ``industry`` is set only for an ``industry`` list (the
    displayed industry, any casing)."""

    group: OutreachGroupKey
    key: OutreachListKey
    industry: str | None = None


def make_ref(
    group: OutreachGroupKey, key: OutreachListKey, industry: str | None
) -> ListRef:
    """Validate ``(group, key, industry)``; raise :class:`NoSuchList` if it is
    not a list. The industry is trimmed; a blank one counts as missing."""
    if key not in GROUP_LISTS[group]:
        raise NoSuchList(key)
    industry = (industry or "").strip() or None
    if (key is L.industry) != (industry is not None):
        raise NoSuchList(key)
    if (
        industry is not None
        and group in FINANCE_INDUSTRY_GROUPS
        and industry.lower() not in _FINANCE_BY_LOWER
    ):
        raise NoSuchList(key)
    return ListRef(group, key, industry)


# --- predicates ----------------------------------------------------------------


def _willing(column):
    """The flag is true. An alum with no engagement row (outer join) is not."""
    return func.coalesce(column, false()).is_(True)


_GROUP_FLAG = {
    G.conference: APE.finance_conference_willing,
    G.speakers: APE.guest_speaker_willing,
    G.sponsors: APE.company_event_sponsor_willing,
    G.mentors: APE.mentor_willing,
    G.case_competition: APE.case_competition_host_willing,
    G.event_help: APE.help_at_event_willing,
}


def industry_key_expr():
    """The displayed industry (#891), lower-cased; NULL for none, blank or an
    explicit "Unknown"."""
    shown = displayed_industry(
        CurrentEmployment.current_industry,
        CurrentEmployment.current_industry_secondary,
    )
    return func.nullif(func.lower(shown), "unknown")


def _displayed_industry_expr():
    return displayed_industry(
        CurrentEmployment.current_industry,
        CurrentEmployment.current_industry_secondary,
    )


_UTAH_SPELLINGS = ("ut", "utah")


def _in_utah():
    """Work state is Utah ("Utah"/"UT", any case or spacing). Never NULL, so
    ``not_`` of it is "Out of State" INCLUDING no work state."""
    state = func.lower(func.trim(cast(CurrentEmployment.current_state, String)))
    return func.coalesce(state.in_(_UTAH_SPELLINGS), false())


_CONFERENCE_SPLIT = ("real estate", "corporate finance")


def list_predicate(ref: ListRef):
    """The WHERE clause (on top of the NetTrek population) for one list."""
    if ref.key is L.women:
        return _willing(APE.women_in_finance_mentor_willing)
    willing = _willing(_GROUP_FLAG[ref.group])
    if ref.key is L.all:
        return willing
    ind = industry_key_expr()
    if ref.key is L.real_estate:
        return willing & (ind == "real estate")
    if ref.key is L.corporate_finance:
        return willing & (ind == "corporate finance")
    if ref.key is L.financial_services:
        # Every other industry, AND no industry: NULL-safe complement.
        return willing & not_(func.coalesce(ind.in_(_CONFERENCE_SPLIT), false()))
    if ref.key is L.utah:
        return willing & _in_utah()
    if ref.key is L.out_of_state:
        return willing & not_(_in_utah())
    if ref.key is L.industry:
        return willing & (ind == (ref.industry or "").strip().lower())
    if ref.key is L.no_industry:
        return willing & ind.is_(None)
    raise NoSuchList(ref.key)  # pragma: no cover - make_ref rules it out


def _list_select(ref: ListRef, view: ReportView | None = None):
    return nettreks.report_select(list_predicate(ref), view or ReportView())


async def _count(session: AsyncSession, ref: ListRef) -> int:
    stmt = nettreks.report_from_clause(select(func.count())).where(
        *nettreks.population_conditions(), list_predicate(ref)
    )
    return int(await session.scalar(stmt) or 0)


# --- titles ----------------------------------------------------------------------


def industry_label(industry: str) -> str:
    """Canonical vocabulary casing when the industry is in the vocabulary;
    otherwise the value as given (trimmed)."""
    value = industry.strip()
    return _INDUSTRY_BY_LOWER.get(value.lower(), value)


def list_title(ref: ListRef) -> str:
    if ref.key is L.industry:
        suffix = _INDUSTRY_TITLE_SUFFIX[ref.group]
        return f"{industry_label(ref.industry or '')} {suffix}"
    return _FIXED_TITLES[(ref.group, ref.key)]


# --- landing pages ----------------------------------------------------------------


def _most_first(item: tuple[str, int]) -> tuple[int, str]:
    """Most first, ties A→Z — the dashboard industry chart's order."""
    return (-item[1], item[0].lower())


async def _industry_counts(
    session: AsyncSession, group: OutreachGroupKey
) -> list[tuple[str, str, int]]:
    """``(industry key, displayed spelling, n)`` for every displayed industry
    with at least one alum willing on ``group``'s flag. One grouped query; the
    key is labelled in a subquery and grouped OUTSIDE it, so PostgreSQL never
    has to match the displayed-industry CASE (with its bind parameters) between
    SELECT and GROUP BY."""
    per_alum = (
        nettreks.report_from_clause(
            select(
                industry_key_expr().label("ind_key"),
                _displayed_industry_expr().label("shown"),
            )
        )
        .where(*nettreks.population_conditions(), _willing(_GROUP_FLAG[group]))
        .subquery()
    )
    rows = (
        await session.execute(
            select(per_alum.c.ind_key, func.min(per_alum.c.shown), func.count())
            .where(per_alum.c.ind_key.is_not(None))
            .group_by(per_alum.c.ind_key)
        )
    ).all()
    return [(key, shown, int(n)) for key, shown, n in rows]


async def _mentor_industries(session: AsyncSession) -> list[tuple[str, int]]:
    """``(industry label, mentors)`` for every industry with at least one
    mentor, most mentors first (ties A→Z)."""
    out = [
        (industry_label(shown), n)
        for _key, shown, n in await _industry_counts(session, G.mentors)
    ]
    out.sort(key=_most_first)
    return out


async def _finance_industries(
    session: AsyncSession, group: OutreachGroupKey
) -> list[tuple[str, int]]:
    """``(industry, n)`` for EVERY dashboard finance industry, zeros included,
    most first (ties A→Z). Displayed industries outside that set are not
    counted here (they are on the "All" list only)."""
    counts = dict.fromkeys(WHEEL_INDUSTRIES, 0)
    for key, _shown, n in await _industry_counts(session, group):
        canonical = _FINANCE_BY_LOWER.get(key)
        if canonical is not None:
            counts[canonical] += n
    return sorted(counts.items(), key=_most_first)


async def get_summary(
    session: AsyncSession, group: OutreachGroupKey
) -> OutreachGroupSummary:
    """Every list on one landing page with its count. Alumni Mentors lists one
    link per industry that has a mentor (most first), then Industry Not Listed
    (only when someone is in it), then Alumni Mentors for Women (always). Case
    Competition Hosts and Event Help list "All" first, then every dashboard
    finance industry (most first, zeros included), each titled with the
    industry alone."""
    links: list[OutreachListLink] = []
    if group in FINANCE_INDUSTRY_GROUPS:
        everyone = ListRef(group, L.all)
        links.append(
            OutreachListLink(
                key=L.all,
                industry=None,
                title=list_title(everyone),
                count=await _count(session, everyone),
            )
        )
        for label, n in await _finance_industries(session, group):
            links.append(
                OutreachListLink(key=L.industry, industry=label, title=label, count=n)
            )
        return OutreachGroupSummary(
            group=group, title=GROUP_TITLES[group], links=links
        )
    if group is G.mentors:
        for label, n in await _mentor_industries(session):
            ref = ListRef(group, L.industry, label)
            links.append(
                OutreachListLink(
                    key=L.industry, industry=label, title=list_title(ref), count=n
                )
            )
        fixed = [L.no_industry, L.women]
    else:
        fixed = list(GROUP_LISTS[group])
    for key in fixed:
        ref = ListRef(group, key)
        n = await _count(session, ref)
        if key is L.no_industry and n == 0:
            continue
        links.append(
            OutreachListLink(key=key, industry=None, title=list_title(ref), count=n)
        )
    return OutreachGroupSummary(group=group, title=GROUP_TITLES[group], links=links)


# --- one list ---------------------------------------------------------------------


async def list_page(
    session: AsyncSession,
    ref: ListRef,
    *,
    limit: int,
    offset: int,
    can_edit: bool,
    view: ReportView | None = None,
) -> OutreachListPage:
    view = (view or ReportView()).for_caller(can_edit=can_edit)
    items, total, options = await nettreks.report_page(
        session,
        list_predicate(ref),
        view,
        limit=limit,
        offset=offset,
        can_edit=can_edit,
    )
    return OutreachListPage(
        group=ref.group,
        group_title=GROUP_TITLES[ref.group],
        key=ref.key,
        industry=industry_label(ref.industry) if ref.industry else None,
        title=list_title(ref),
        items=items,
        total=total,
        limit=limit,
        offset=offset,
        filter_options=options,
    )


def audit_detail(ref: ListRef) -> str:
    """``list=<group>:<key>[:<industry>]`` for the audit row — what was read,
    never the rows. The caller-supplied industry is reduced to one plain
    ``;``-free line so it cannot add or alter fields of the audit line."""
    base = f"list={ref.group.value}:{ref.key.value}"
    if not ref.industry:
        return base
    return f"{base}:{audit_safe(industry_label(ref.industry))}"


# --- CSV --------------------------------------------------------------------------


def _slug(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", value.lower()).strip("_")[:60] or "industry"


def export_filename(
    ref: ListRef, today: datetime.date | None = None, *, filtered: bool = False
) -> str:
    """``conference_real_estate_<date>.csv`` /
    ``mentors_industry_investment_banking_<date>.csv``, with ``_filtered``
    before the date when filters narrowed it. The industry part is slugged to
    ``[a-z0-9_]`` — the only caller text in the name — so the header can never
    be broken by it."""
    parts = [ref.group.value, ref.key.value]
    if ref.industry:
        parts.append(_slug(ref.industry))
    if filtered:
        parts.append("filtered")
    day = (today or datetime.date.today()).isoformat()
    return f"{'_'.join(parts)}_{day}.csv"


async def export_list_csv(
    session: AsyncSession,
    ref: ListRef,
    *,
    actor_user_id: int | None,
    can_edit: bool,
    view: ReportView | None = None,
) -> str:
    """The list as CSV — the whole list, or as the view filters and sorts it,
    from the same query as the page, with the NetTrek reports' columns and cell
    guard. Audit-logged as :data:`OUTREACH_EXPORT_ACTION` with the list, row
    count and filters — never the rows."""
    view = (view or ReportView()).for_caller(can_edit=can_edit)
    rows = (await session.execute(_list_select(ref, view))).all()
    csv_text = nettreks.report_rows_csv(rows, can_edit=can_edit)
    if actor_user_id is None:
        log.warning(
            "%s audit skipped: no actor (rows=%s)", OUTREACH_EXPORT_ACTION, len(rows)
        )
    else:
        session.add(
            AuditLog(
                user_id=actor_user_id,
                action_type=OUTREACH_EXPORT_ACTION,
                entity_type=OUTREACH_ENTITY,
                entity_id=None,
                new_value=f"{audit_detail(ref)}; rows={len(rows)}{view.audit_suffix()}",
            )
        )
        await session.commit()
    return csv_text
