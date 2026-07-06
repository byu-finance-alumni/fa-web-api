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
    """One state bucket in the by-state distribution."""

    state: str
    count: int


class DashboardSummary(BaseModel):
    """KPIs + distributions for ``GET /dashboard/summary`` (aggregate counts
    only; no per-alumnus identity)."""

    total_alumni: int
    archived: int
    deceased: int
    missing_email: int
    missing_employer: int
    contacted_this_month: int
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
    missing_email: int
    missing_employer: int
    duplicate_count: int


class FollowUpRow(BaseModel):
    """One open follow-up task in ``GET /dashboard/follow-ups``."""

    task_id: int
    alumni_id: int
    alumni_name: str
    title: str | None = None
    due_date: str | None = None
    assigned_to: str | None = None
