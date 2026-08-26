"""Response schemas for the dashboard routes.

These mirror the EXACT dict/list shapes the handlers in
``app/api/routes/dashboard.py`` already return — they exist only so the routes
carry a concrete ``response_model`` and are therefore covered by the OpenAPI
type-contract drift guard (#187). They must not change the response data; if a
handler's shape changes, update the matching schema here in lockstep.
"""

from __future__ import annotations

from pydantic import BaseModel


class DashboardGradYearCount(BaseModel):
    """One graduation-year bucket in the cohort distribution."""

    year: int
    count: int


class DashboardEmployerCount(BaseModel):
    """One employer bucket in the top-employers distribution."""

    employer: str
    count: int


class DashboardStateCount(BaseModel):
    """One state bucket in the by-state distribution.

    ``state`` is the CANONICAL FULL state name ("Utah", "District of Columbia") —
    never a 2-letter code and never a non-US region. The stored column is free
    text; the query folds it before grouping (#754), so a record entered as "UT"
    and one entered as "Utah" land in this one bucket.
    """

    state: str
    count: int


class DashboardIndustryCount(BaseModel):
    """One finance-industry bucket in the industry breakdown (#353)."""

    industry: str
    count: int


class DashboardIndustryBreakdown(BaseModel):
    """Industry breakdown for the dashboard wheel (#351/#352/#353).

    ``industries`` covers EVERY canonical finance industry (from the controlled
    vocab) — including ones with a count of 0 — so the legend can list them all.
    ``other`` (the catch-all "Other" vocab value + any non-canonical value) and
    ``unknown`` (active alumni with NO industry on file) are SEPARATE buckets,
    distinct from each other. ``graduate_student`` (#294) is likewise its own
    bucket — alumni whose current industry is "Graduate Student" — split out of
    ``other`` so the dashboard can show it as its own bar. "Military" (#608) gets
    NO such bucket — Jake kept the chart about finance sectors, so it folds into
    ``other`` like any other non-wheel value.
    """

    industries: list[DashboardIndustryCount]
    other: int
    unknown: int
    graduate_student: int


class DashboardSummary(BaseModel):
    """KPIs + distributions for ``GET /dashboard/summary`` (aggregate counts
    only; no per-alumnus identity)."""

    total_alumni: int
    archived: int
    deceased: int
    missing_email: int
    missing_employer: int
    contacted_this_month: int
    # #606: active alumni whose ``updated_at`` falls in the CURRENT CALENDAR
    # month (1st 00:00 UTC through now) — not the rolling 30-day window the
    # contacted/attended KPIs use. Counts any write to the record, including
    # bulk imports (deliberate — the tile measures data freshness).
    alumni_edited_this_month: int
    # #645: the same count over the CURRENT CALENDAR YEAR to date (1 Jan 00:00
    # UTC through now) — a running year total shown under the month figure on
    # the same tile. Year-to-date, NOT a rolling 12 months, so it drops to near
    # zero each January by design. Both fields count DISTINCT ALUMNI RECORDS
    # (one row per alumnus in ``alumni``), never individual changes, so the year
    # value is always >= ``alumni_edited_this_month``.
    alumni_edited_this_year: int
    #: How many DIFFERENT firms the active alumni currently work for. Names are
    #: folded with lower(trim(...)) before counting and the Top-employers
    #: chart's non-employer placeholders are excluded, so this number and that
    #: chart describe the same set of companies. See the query for why.
    distinct_employers: int
    #: How many US STATES those companies are in — the first half of the
    #: Companies tile's sub-line, "Across N states and M countries" (#754).
    #: The employer's address, the same one the geography map plots. Free-text
    #: values are folded to a canonical state name ("UT" and "Utah" are one
    #: state) and anything that is not one of the 50 states + DC is dropped, so
    #: this is BOUNDED AT 51 — a larger number is a bug, which is how the tile
    #: came to read "Across 70 states".
    employer_states: int
    #: How many COUNTRIES the alumni working OUTSIDE the US are in — the second
    #: half of the same sub-line. Counts distinct ``current_country`` values for
    #: alumni whose work state does not resolve to a US state, with US spellings
    #: excluded, so this and ``employer_states`` partition the population rather
    #: than overlap. ⚠️ READS LOW BY DESIGN: an alum abroad with a blank country
    #: cannot be attributed to one and is simply not counted, and the column has
    #: no controlled vocab, so "UK" and "United Kingdom" count separately.
    employer_countries: int
    not_contacted_6mo: int
    not_contacted_12mo: int
    not_contacted_24mo: int
    upcoming_follow_ups: int
    duplicate_count: int
    attended_event_this_month: int
    upcoming_events: int
    events_this_month: int
    guest_speakers_this_month: int
    piff_donors: int
    willing_mentors: int
    by_graduation_year: list[DashboardGradYearCount]
    top_employers: list[DashboardEmployerCount]
    by_state: list[DashboardStateCount]
    industry_breakdown: DashboardIndustryBreakdown


class BirthdayRow(BaseModel):
    """One alumnus with a birthday this month (``GET /dashboard/birthdays``).
    Only the recurring month+day is exposed — never the birth year (FERPA)."""

    id: int
    first_name: str | None = None
    last_name: str | None = None
    current_employer: str | None = None
    graduation_year: int | None = None
    birth_month: int | None = None
    birth_day: int | None = None


class EventParticipationRow(BaseModel):
    """One event with its attendee count
    (``GET /dashboard/event-participation``)."""

    event_id: int
    event_name: str | None = None
    event_type: str | None = None
    event_date: str | None = None
    participant_count: int


class InteractionActivity(BaseModel):
    """One interaction row in the activity feed / contacted-this-month list.

    Matches ``dashboard._serialize_interaction`` exactly. ``by`` is the actor's
    display name (email fallback); ``by_user_id`` their user id — both null when
    the actor user was removed. No internal user PK beyond ``by_user_id``.
    """

    interaction_id: int
    alumni_id: int
    alumni_name: str
    type: str | None = None
    when: str | None = None
    by: str | None = None
    by_user_id: int | None = None


class ActivityFeed(BaseModel):
    """Paginated interaction feed for ``GET /dashboard/activity``."""

    items: list[InteractionActivity]
    types: list[str]
    total: int
    limit: int
    offset: int


class DataQuality(BaseModel):
    """Data-quality alert counts for ``GET /dashboard/data-quality``."""

    total_alumni: int
    complete_alumni: int
    missing_email: int
    missing_employer: int
    missing_phone: int
    duplicate_count: int


class FollowUpRow(BaseModel):
    """One open follow-up task in ``GET /dashboard/follow-ups``."""

    task_id: int
    alumni_id: int
    alumni_name: str
    title: str | None = None
    due_date: str | None = None
    assigned_to: str | None = None
