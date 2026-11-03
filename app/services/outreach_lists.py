"""Outreach interest lists: Conference Interest, Event Speakers, Event Sponsors
and Alumni Mentors.

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

from app.core.dropdowns import INDUSTRIES
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

log = logging.getLogger(__name__)

G = OutreachGroupKey
L = OutreachListKey

#: Landing-page title per group.
GROUP_TITLES: dict[OutreachGroupKey, str] = {
    G.conference: "Conference Interest",
    G.speakers: "Event Speakers",
    G.sponsors: "Event Sponsors",
    G.mentors: "Alumni Mentors",
}

#: The lists each group has, in landing-page order.
GROUP_LISTS: dict[OutreachGroupKey, tuple[OutreachListKey, ...]] = {
    G.conference: (L.real_estate, L.corporate_finance, L.financial_services),
    G.speakers: (L.utah, L.out_of_state),
    G.sponsors: (L.utah, L.out_of_state),
    G.mentors: (L.industry, L.no_industry, L.women),
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
}

#: The audit action every Outreach list CSV writes (counted in the global
#: export budget via ``rate_limit.EXPORT_AUDIT_ACTIONS``).
OUTREACH_EXPORT_ACTION = "export_outreach_list"
#: ``entity_type`` on both the view and the export audit rows.
OUTREACH_ENTITY = "outreach_list"

_INDUSTRY_BY_LOWER = {v.lower(): v for v in INDUSTRIES}


class NoSuchList(LookupError):
    """The group has no such list, or an industry list was asked for without an
    industry (or a non-industry list with one)."""


@dataclass(frozen=True)
class ListRef:
    """One list: ``industry`` is set only for an Alumni Mentors ``industry``
    list (the displayed industry, any casing)."""

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


def _list_select(ref: ListRef):
    return (
        nettreks.report_from_clause(select(*nettreks.REPORT_ROW_COLUMNS))
        .where(*nettreks.population_conditions(), list_predicate(ref))
        .order_by(*nettreks.REPORT_ORDER_BY)
    )


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
        return f"{industry_label(ref.industry or '')} Mentors"
    return _FIXED_TITLES[(ref.group, ref.key)]


# --- landing pages ----------------------------------------------------------------


async def _mentor_industries(session: AsyncSession) -> list[tuple[str, int]]:
    """``(industry label, mentors)`` for every industry with at least one
    mentor, most mentors first (ties A→Z). One grouped query; the key is
    labelled in a subquery and grouped OUTSIDE it, so PostgreSQL never has to
    match the displayed-industry CASE (with its bind parameters) between
    SELECT and GROUP BY."""
    per_alum = (
        nettreks.report_from_clause(
            select(
                industry_key_expr().label("ind_key"),
                _displayed_industry_expr().label("shown"),
            )
        )
        .where(*nettreks.population_conditions(), _willing(APE.mentor_willing))
        .subquery()
    )
    rows = (
        await session.execute(
            select(per_alum.c.ind_key, func.min(per_alum.c.shown), func.count())
            .where(per_alum.c.ind_key.is_not(None))
            .group_by(per_alum.c.ind_key)
        )
    ).all()
    out = [(industry_label(shown), int(n)) for _key, shown, n in rows]
    out.sort(key=lambda item: (-item[1], item[0].lower()))
    return out


async def get_summary(
    session: AsyncSession, group: OutreachGroupKey
) -> OutreachGroupSummary:
    """Every list on one landing page with its count. Alumni Mentors lists one
    link per industry that has a mentor (most first), then Industry Not Listed
    (only when someone is in it), then Alumni Mentors for Women (always)."""
    links: list[OutreachListLink] = []
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
) -> OutreachListPage:
    stmt = _list_select(ref)
    total = await session.scalar(
        select(func.count()).select_from(stmt.order_by(None).subquery())
    )
    rows = (await session.execute(stmt.limit(limit).offset(offset))).all()
    return OutreachListPage(
        group=ref.group,
        group_title=GROUP_TITLES[ref.group],
        key=ref.key,
        industry=industry_label(ref.industry) if ref.industry else None,
        title=list_title(ref),
        items=[nettreks.to_report_row(r, can_edit=can_edit) for r in rows],
        total=int(total or 0),
        limit=limit,
        offset=offset,
    )


# Characters an audit value must not carry: control characters (newlines
# included) and ``;``, the field separator of the audit detail line.
_AUDIT_UNSAFE = re.compile(r"[\x00-\x1f\x7f-\x9f;]+")


def _audit_safe(value: str) -> str:
    return _AUDIT_UNSAFE.sub(" ", value).strip()


def audit_detail(ref: ListRef) -> str:
    """``list=<group>:<key>[:<industry>]`` for the audit row — what was read,
    never the rows. The caller-supplied industry is reduced to one plain
    ``;``-free line so it cannot add or alter fields of the audit line."""
    base = f"list={ref.group.value}:{ref.key.value}"
    if not ref.industry:
        return base
    return f"{base}:{_audit_safe(industry_label(ref.industry))}"


# --- CSV --------------------------------------------------------------------------


def _slug(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", value.lower()).strip("_")[:60] or "industry"


def export_filename(ref: ListRef, today: datetime.date | None = None) -> str:
    """``conference_real_estate_<date>.csv`` /
    ``mentors_industry_investment_banking_<date>.csv``. The industry part is
    slugged to ``[a-z0-9_]`` — the only caller text in the name — so the
    header can never be broken by it."""
    parts = [ref.group.value, ref.key.value]
    if ref.industry:
        parts.append(_slug(ref.industry))
    day = (today or datetime.date.today()).isoformat()
    return f"{'_'.join(parts)}_{day}.csv"


async def export_list_csv(
    session: AsyncSession,
    ref: ListRef,
    *,
    actor_user_id: int | None,
    can_edit: bool,
) -> str:
    """The whole list as CSV — the NetTrek reports' columns and cell guard.
    Audit-logged as :data:`OUTREACH_EXPORT_ACTION` with the list and row count
    — never the rows."""
    rows = (await session.execute(_list_select(ref))).all()
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
                new_value=f"{audit_detail(ref)}; rows={len(rows)}",
            )
        )
        await session.commit()
    return csv_text
