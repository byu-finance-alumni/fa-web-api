"""Data access for alumni core records.

Thin query layer — business rules (soft-delete, manual-edit stamping) live in
the service. ``build_alumni_query`` is a pure function (no IO) so the filter
logic can be unit-tested by compiling the statement.

Search currently covers the alumni *core* table (names, external ids,
graduation year, deceased). Employer / industry / title / city / state / tags /
status-label search needs the related tables (modeled later); the filter list
here is structured so those conditions can be added as joins without reshaping
callers.
"""

import datetime

from sqlalchemy import Select, and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.alumni import Alumni
from app.models.contact import AlumniContactInfo
from app.models.crm import Interaction, Survey
from app.models.duplicate import DuplicateCandidate
from app.models.employment import CurrentEmployment, EmploymentHistory
from app.models.engagement import AlumniProgramEngagement, FinanceSocietyLeadership
from app.models.event import Event, EventAttendance
from app.models.tags import AlumniStatusLabel, AlumniTag, StatusLabel, Tag
from app.utils.sql import escape_like

# Biennial survey cadence (#160): an alumnus is DUE for surveying when their
# most-recent completed survey is older than this, or they have never completed
# one. Expressed in days so the cutoff arithmetic stays in stdlib; a 2-year
# staleness window is insensitive to leap-year calendar drift. Callers compute
# the cutoff as ``now() - SURVEY_CADENCE`` and pass it as ``survey_due_before``.
SURVEY_CADENCE = datetime.timedelta(days=365 * 2)


def _as_values(value: "str | list[str] | None") -> list[str]:
    """Normalize a filter input to a clean list of non-empty strings.

    Accepts a single string (legacy / single deep-link), a list (multi-select),
    or None. Lets every text filter support multi-select via one repeated query
    param while a single-value link (``?employer=X``) keeps working unchanged.
    """
    if value is None:
        return []
    if isinstance(value, str):
        value = [value]
    return [v for v in value if v and v.strip()]


def _ilike_any(column, values: list[str]):
    """OR of case-insensitive exact matches (literal %/_ escaped) for `values`."""
    return or_(*[column.ilike(escape_like(v), escape="\\") for v in values])


async def get(session: AsyncSession, alumni_id: int) -> Alumni | None:
    return await session.get(Alumni, alumni_id)


# Sort options exposed by the list UI, mapped to their ORDER BY columns. Kept as
# a pure module-level function (no IO) so the mapping can be unit-tested by
# compiling the clause — this is the one place the grad-year direction is decided
# and it MUST stay: grad_desc = most-recent grad year FIRST (DESC, nulls last),
# grad_asc = oldest FIRST (ASC, nulls last). The frontend dropdown labels
# ("newest" -> grad_desc, "oldest" -> grad_asc) and the route's `sort` enum stay
# in lockstep with these tokens. Unknown/legacy values fall back to name.
def alumni_order_by(sort: str | None) -> tuple:
    """Return the ORDER BY tuple for a list ``sort`` token (name | grad_desc | grad_asc)."""
    return {
        "name": (Alumni.last_name.asc(), Alumni.alumni_id.asc()),
        "grad_desc": (
            Alumni.graduation_year.desc().nulls_last(),
            Alumni.last_name.asc(),
        ),
        "grad_asc": (
            Alumni.graduation_year.asc().nulls_last(),
            Alumni.last_name.asc(),
        ),
    }.get(sort or "name", (Alumni.last_name.asc(), Alumni.alumni_id.asc()))


def build_alumni_query(
    *,
    q: str | None = None,
    # Per-field search (dashboard Quick / Advanced search). Each is a
    # case-insensitive PARTIAL match, AND-combined with the others, and ignored
    # when blank — so an empty box never narrows the results.
    net_id: str | None = None,
    first_name: str | None = None,
    last_name: str | None = None,
    preferred_name: str | None = None,
    email: str | None = None,
    graduation_year: int | None = None,
    grad_year_min: int | None = None,
    grad_year_max: int | None = None,
    deceased: bool | None = None,
    # Text facets accept a single value (legacy / deep-link) OR a list
    # (multi-select). Each is matched case-insensitively, exact, literal-escaped.
    employer: "str | list[str] | None" = None,
    past_employer: "str | list[str] | None" = None,
    industry: "str | list[str] | None" = None,
    title: "str | list[str] | None" = None,
    seniority: "str | list[str] | None" = None,
    city: "str | list[str] | None" = None,
    state: "str | list[str] | None" = None,
    tag: "str | list[str] | None" = None,
    status_label: "str | list[str] | None" = None,
    leadership_role: "str | list[str] | None" = None,
    survey_status: "str | list[str] | None" = None,
    # "Needs surveying" (#160): alumni who are DUE for the biennial survey —
    # never completed one, or whose most-recent completion is older than the
    # survey cadence. The threshold is computed server-side and passed in (the
    # route owns "now"); a ``None`` threshold disables the filter.
    needs_survey: bool = False,
    survey_due_before: datetime.datetime | None = None,
    # Last-contacted (derived from interactions): contacted on/after a date,
    # NOT contacted since a date (stale), or never contacted at all.
    contacted_after: datetime.date | None = None,
    contacted_before: datetime.date | None = None,
    never_contacted: bool = False,
    attended_event: bool = False,
    # Guest-speaker-AT-AN-EVENT window (drives the dashboard "Guest speakers this
    # month" tile's deep-link). Distinct from ``guest_speaker_willing`` (the
    # alumnus-level willing flag, no date) — these select alumni who actually
    # served as a speaker at an event whose date falls in the window.
    spoke_after: datetime.date | None = None,
    spoke_before: datetime.date | None = None,
    donor: bool = False,
    mentor_willing: bool = False,
    guest_speaker_willing: bool = False,
    # Professional-certification flags on the program-engagement profile. Each
    # narrows to alumni who hold that designation (correlated EXISTS).
    cfa: bool = False,
    cpa: bool = False,
    missing_email: bool = False,
    missing_employer: bool = False,
    duplicate: bool = False,
    # Friends of the finance program (#218). ``True`` -> alumni only, ``False``
    # -> friends only, ``None`` -> both. The list route defaults this to ``True``
    # so the Alumni page is unchanged; friends are opt-in.
    is_alumni: bool | None = True,
    include_archived: bool = False,
) -> Select:
    """Build the filtered ``SELECT alumni`` statement (without limit/offset).

    The missing-data filters mirror the dashboard's KPI logic exactly so the
    "Review" deep-links land on the same population the dashboard counted:
      * ``missing_email``    — no contact-info row with a personal/work email
      * ``missing_employer`` — no current-employment row naming an employer
      * ``duplicate``        — the alumnus appears on either side of a
        ``duplicate_candidates`` pair
    All conditions are correlated EXISTS subqueries so filtering stays in
    PostgreSQL (never client-side) and the plan is stable at scale.
    """
    conditions = []
    if not include_archived:
        conditions.append(Alumni.archived.is_(False))
    # Alumni / friends split (#218). None -> no predicate (return both).
    if is_alumni is not None:
        conditions.append(Alumni.is_alumni.is_(is_alumni))
    if q:
        like = f"%{escape_like(q)}%"
        conditions.append(
            or_(
                Alumni.first_name.ilike(like, escape="\\"),
                Alumni.last_name.ilike(like, escape="\\"),
                Alumni.preferred_first_name.ilike(like, escape="\\"),
                # Maiden / birth name (#216): a last-name search must also find
                # alumnae listed under their birth name. Matched case-insensitively
                # like the other name columns.
                Alumni.birth_name.ilike(like, escape="\\"),
                Alumni.middle_name.ilike(like, escape="\\"),
                Alumni.byu_id.ilike(like, escape="\\"),
                Alumni.net_id.ilike(like, escape="\\"),
            )
        )
    # Per-field partial matches (AND-combined; blanks ignored).
    def _field_like(value: str | None, column) -> None:
        if value and value.strip():
            conditions.append(
                column.ilike(f"%{escape_like(value.strip())}%", escape="\\")
            )

    _field_like(net_id, Alumni.net_id)
    _field_like(first_name, Alumni.first_name)
    # Last-name field search also matches the maiden / birth name (#216) so an
    # alumna looked up by her birth name is found even via the dedicated
    # last-name box, not just the free-text ``q`` search.
    if last_name and last_name.strip():
        like = f"%{escape_like(last_name.strip())}%"
        conditions.append(
            or_(
                Alumni.last_name.ilike(like, escape="\\"),
                Alumni.birth_name.ilike(like, escape="\\"),
            )
        )
    _field_like(preferred_name, Alumni.preferred_first_name)
    if email and email.strip():
        like = f"%{escape_like(email.strip())}%"
        conditions.append(
            select(AlumniContactInfo.contact_info_id)
            .where(
                AlumniContactInfo.alumni_id == Alumni.alumni_id,
                or_(
                    AlumniContactInfo.personal_email.ilike(like, escape="\\"),
                    AlumniContactInfo.work_email.ilike(like, escape="\\"),
                ),
            )
            .exists()
        )
    if graduation_year is not None:
        conditions.append(Alumni.graduation_year == graduation_year)
    if grad_year_min is not None:
        conditions.append(Alumni.graduation_year >= grad_year_min)
    if grad_year_max is not None:
        conditions.append(Alumni.graduation_year <= grad_year_max)
    if deceased is not None:
        conditions.append(Alumni.deceased.is_(deceased))
    employers = _as_values(employer)
    if employers:
        conditions.append(
            select(CurrentEmployment.current_employment_id)
            .where(
                CurrentEmployment.alumni_id == Alumni.alumni_id,
                _ilike_any(CurrentEmployment.current_employer, employers),
            )
            .exists()
        )
    past_employers = _as_values(past_employer)
    if past_employers:
        conditions.append(
            select(EmploymentHistory.employment_history_id)
            .where(
                EmploymentHistory.alumni_id == Alumni.alumni_id,
                _ilike_any(EmploymentHistory.employer_name, past_employers),
            )
            .exists()
        )
    industries = _as_values(industry)
    if industries:
        conditions.append(
            select(CurrentEmployment.current_employment_id)
            .where(
                CurrentEmployment.alumni_id == Alumni.alumni_id,
                or_(
                    _ilike_any(CurrentEmployment.current_industry, industries),
                    _ilike_any(
                        CurrentEmployment.current_industry_secondary, industries
                    ),
                ),
            )
            .exists()
        )
    titles = _as_values(title)
    if titles:
        conditions.append(
            select(CurrentEmployment.current_employment_id)
            .where(
                CurrentEmployment.alumni_id == Alumni.alumni_id,
                _ilike_any(CurrentEmployment.current_title, titles),
            )
            .exists()
        )
    seniorities = _as_values(seniority)
    if seniorities:
        conditions.append(
            select(CurrentEmployment.current_employment_id)
            .where(
                CurrentEmployment.alumni_id == Alumni.alumni_id,
                _ilike_any(CurrentEmployment.seniority_level, seniorities),
            )
            .exists()
        )
    cities = _as_values(city)
    if cities:
        conditions.append(
            select(AlumniContactInfo.contact_info_id)
            .where(
                AlumniContactInfo.alumni_id == Alumni.alumni_id,
                _ilike_any(AlumniContactInfo.city, cities),
            )
            .exists()
        )
    states = _as_values(state)
    if states:
        conditions.append(
            select(AlumniContactInfo.contact_info_id)
            .where(
                AlumniContactInfo.alumni_id == Alumni.alumni_id,
                _ilike_any(AlumniContactInfo.state, states),
            )
            .exists()
        )
    tags = _as_values(tag)
    if tags:
        conditions.append(
            select(AlumniTag.alumni_tag_id)
            .join(Tag, Tag.tag_id == AlumniTag.tag_id)
            .where(
                AlumniTag.alumni_id == Alumni.alumni_id,
                _ilike_any(Tag.tag_name, tags),
            )
            .exists()
        )
    status_labels = _as_values(status_label)
    if status_labels:
        conditions.append(
            select(AlumniStatusLabel.alumni_status_label_id)
            .join(
                StatusLabel,
                StatusLabel.status_label_id == AlumniStatusLabel.status_label_id,
            )
            .where(
                AlumniStatusLabel.alumni_id == Alumni.alumni_id,
                _ilike_any(StatusLabel.status_label_name, status_labels),
            )
            .exists()
        )
    leadership_roles = _as_values(leadership_role)
    if leadership_roles:
        conditions.append(
            select(FinanceSocietyLeadership.finance_society_leadership_id)
            .where(
                FinanceSocietyLeadership.alumni_id == Alumni.alumni_id,
                _ilike_any(
                    FinanceSocietyLeadership.leadership_role, leadership_roles
                ),
            )
            .exists()
        )
    survey_statuses = _as_values(survey_status)
    if survey_statuses:
        conditions.append(
            select(Survey.survey_id)
            .where(
                Survey.alumni_id == Alumni.alumni_id,
                _ilike_any(Survey.survey_status, survey_statuses),
            )
            .exists()
        )
    if needs_survey and survey_due_before is not None:
        # DUE = no survey COMPLETED within the cadence window. A correlated NOT
        # EXISTS over a recent completion captures both cases in one predicate:
        #   * never surveyed (no survey rows, or rows with a NULL completed_at)
        #     -> nothing satisfies the inner EXISTS -> DUE, and
        #   * most-recent completion older than the threshold -> still no row
        #     with completed_at >= threshold -> DUE.
        # ``survey_due_before`` is the server-computed cutoff (now - cadence);
        # comparing completed_at to it avoids re-deriving "most recent" — any
        # completion on/after the cutoff means NOT due. Stays in PostgreSQL with
        # a stable plan (indexable on surveys.alumni_id, completed_at).
        conditions.append(
            ~select(Survey.survey_id)
            .where(
                Survey.alumni_id == Alumni.alumni_id,
                Survey.completed_at.is_not(None),
                Survey.completed_at >= survey_due_before,
            )
            .exists()
        )
    if contacted_after is not None:
        conditions.append(
            select(Interaction.interaction_id)
            .where(
                Interaction.alumni_id == Alumni.alumni_id,
                Interaction.interaction_date_time >= contacted_after,
            )
            .exists()
        )
    if contacted_before is not None:
        # "Not contacted since": no interaction on/after the date (i.e. stale).
        conditions.append(
            ~select(Interaction.interaction_id)
            .where(
                Interaction.alumni_id == Alumni.alumni_id,
                Interaction.interaction_date_time >= contacted_before,
            )
            .exists()
        )
    if never_contacted:
        conditions.append(
            ~select(Interaction.interaction_id)
            .where(Interaction.alumni_id == Alumni.alumni_id)
            .exists()
        )
    if attended_event:
        has_attended = (
            select(EventAttendance.event_attendance_id)
            .where(EventAttendance.alumni_id == Alumni.alumni_id)
            .exists()
        )
        conditions.append(has_attended)
    if spoke_after is not None or spoke_before is not None:
        # Alumni who served as a guest speaker at an event in the window. Mirrors
        # the dashboard ``guest_speakers_this_month`` KPI exactly (same
        # attendance_status ILIKE '%speaker%' + event_date bounds) so the tile's
        # count equals this deep-linked list's length. Correlated EXISTS over the
        # attendance→event join keeps it in PostgreSQL with a stable plan.
        spoke = (
            select(EventAttendance.event_attendance_id)
            .join(Event, Event.event_id == EventAttendance.event_id)
            .where(
                EventAttendance.alumni_id == Alumni.alumni_id,
                EventAttendance.attendance_status.ilike("%speaker%"),
            )
        )
        if spoke_after is not None:
            spoke = spoke.where(Event.event_date >= spoke_after)
        if spoke_before is not None:
            spoke = spoke.where(Event.event_date <= spoke_before)
        conditions.append(spoke.exists())
    if donor:
        is_donor = (
            select(AlumniProgramEngagement.engagement_profile_id)
            .where(
                AlumniProgramEngagement.alumni_id == Alumni.alumni_id,
                AlumniProgramEngagement.piff_donor.is_(True),
            )
            .exists()
        )
        conditions.append(is_donor)
    if mentor_willing:
        is_mentor = (
            select(AlumniProgramEngagement.engagement_profile_id)
            .where(
                AlumniProgramEngagement.alumni_id == Alumni.alumni_id,
                AlumniProgramEngagement.mentor_willing.is_(True),
            )
            .exists()
        )
        conditions.append(is_mentor)
    if guest_speaker_willing:
        is_speaker = (
            select(AlumniProgramEngagement.engagement_profile_id)
            .where(
                AlumniProgramEngagement.alumni_id == Alumni.alumni_id,
                AlumniProgramEngagement.guest_speaker_willing.is_(True),
            )
            .exists()
        )
        conditions.append(is_speaker)
    if cfa:
        is_cfa = (
            select(AlumniProgramEngagement.engagement_profile_id)
            .where(
                AlumniProgramEngagement.alumni_id == Alumni.alumni_id,
                AlumniProgramEngagement.cfa_designation.is_(True),
            )
            .exists()
        )
        conditions.append(is_cfa)
    if cpa:
        is_cpa = (
            select(AlumniProgramEngagement.engagement_profile_id)
            .where(
                AlumniProgramEngagement.alumni_id == Alumni.alumni_id,
                AlumniProgramEngagement.cpa_designation.is_(True),
            )
            .exists()
        )
        conditions.append(is_cpa)
    if missing_email:
        has_email = (
            select(AlumniContactInfo.contact_info_id)
            .where(
                AlumniContactInfo.alumni_id == Alumni.alumni_id,
                or_(
                    AlumniContactInfo.personal_email.is_not(None),
                    AlumniContactInfo.work_email.is_not(None),
                ),
            )
            .exists()
        )
        conditions.append(~has_email)
    if missing_employer:
        has_employer = (
            select(CurrentEmployment.current_employment_id)
            .where(
                CurrentEmployment.alumni_id == Alumni.alumni_id,
                CurrentEmployment.current_employer.is_not(None),
            )
            .exists()
        )
        conditions.append(~has_employer)
    if duplicate:
        is_duplicate = (
            select(DuplicateCandidate.duplicate_candidate_id)
            .where(
                or_(
                    DuplicateCandidate.alumni_id_1 == Alumni.alumni_id,
                    DuplicateCandidate.alumni_id_2 == Alumni.alumni_id,
                )
            )
            .exists()
        )
        conditions.append(is_duplicate)

    stmt = select(Alumni)
    if conditions:
        stmt = stmt.where(and_(*conditions))
    return stmt


async def list_page(
    session: AsyncSession,
    *,
    limit: int,
    offset: int,
    **filters,
) -> tuple[list[Alumni], int]:
    """Return a filtered page of alumni and the total count for that filter.

    Each returned Alumni also gets ``current_employer`` / ``current_industry``
    (from ``current_employment``) and ``current_city`` / ``current_state`` (from
    ``alumni_contact_info`` — the same table the geography map shades by, so the
    list and the map agree on a record's location) set as plain instance
    attributes via correlated scalar subqueries, so the list view can show them
    without an N+1 or a row-multiplying join. The single-record schema ignores
    these.
    """
    sort = filters.pop("sort", "name") or "name"
    base = build_alumni_query(**filters)
    total = await session.scalar(select(func.count()).select_from(base.subquery()))

    current_employer = (
        select(CurrentEmployment.current_employer)
        .where(CurrentEmployment.alumni_id == Alumni.alumni_id)
        .limit(1)
        .scalar_subquery()
    )
    current_industry = (
        select(CurrentEmployment.current_industry)
        .where(CurrentEmployment.alumni_id == Alumni.alumni_id)
        .limit(1)
        .scalar_subquery()
    )
    # Location for the list's City/State columns. Sourced from the contact-info
    # row (NOT current_employment.current_city/state) so the list matches the
    # geography map, which shades exclusively off alumni_contact_info.
    current_city = (
        select(AlumniContactInfo.city)
        .where(AlumniContactInfo.alumni_id == Alumni.alumni_id)
        .limit(1)
        .scalar_subquery()
    )
    current_state = (
        select(AlumniContactInfo.state)
        .where(AlumniContactInfo.alumni_id == Alumni.alumni_id)
        .limit(1)
        .scalar_subquery()
    )
    rows_stmt = select(
        Alumni, current_employer, current_industry, current_city, current_state
    )
    if base.whereclause is not None:
        rows_stmt = rows_stmt.where(base.whereclause)

    # Sort options exposed by the list UI (see alumni_order_by). Unknown values
    # fall back to name.
    rows_stmt = rows_stmt.order_by(*alumni_order_by(sort)).limit(limit).offset(offset)
    result = await session.execute(rows_stmt)
    items: list[Alumni] = []
    for alumnus, employer, industry, city, state in result.all():
        alumnus.current_employer = employer
        alumnus.current_industry = industry
        alumnus.current_city = city
        alumnus.current_state = state
        items.append(alumnus)
    return items, int(total or 0)
