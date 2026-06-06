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

from sqlalchemy import Select, and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.alumni import Alumni
from app.models.contact import AlumniContactInfo
from app.models.duplicate import DuplicateCandidate
from app.models.employment import CurrentEmployment
from app.models.engagement import AlumniProgramEngagement
from app.models.event import EventAttendance
from app.utils.sql import escape_like


async def get(session: AsyncSession, alumni_id: int) -> Alumni | None:
    return await session.get(Alumni, alumni_id)


def build_alumni_query(
    *,
    q: str | None = None,
    graduation_year: int | None = None,
    grad_year_min: int | None = None,
    grad_year_max: int | None = None,
    deceased: bool | None = None,
    employer: str | None = None,
    industry: str | None = None,
    attended_event: bool = False,
    donor: bool = False,
    mentor_willing: bool = False,
    guest_speaker_willing: bool = False,
    missing_email: bool = False,
    missing_employer: bool = False,
    duplicate: bool = False,
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
    if q:
        like = f"%{escape_like(q)}%"
        conditions.append(
            or_(
                Alumni.first_name.ilike(like, escape="\\"),
                Alumni.last_name.ilike(like, escape="\\"),
                Alumni.preferred_first_name.ilike(like, escape="\\"),
                Alumni.middle_name.ilike(like, escape="\\"),
                Alumni.byu_id.ilike(like, escape="\\"),
                Alumni.net_id.ilike(like, escape="\\"),
            )
        )
    if graduation_year is not None:
        conditions.append(Alumni.graduation_year == graduation_year)
    if grad_year_min is not None:
        conditions.append(Alumni.graduation_year >= grad_year_min)
    if grad_year_max is not None:
        conditions.append(Alumni.graduation_year <= grad_year_max)
    if deceased is not None:
        conditions.append(Alumni.deceased.is_(deceased))
    if employer:
        at_employer = (
            select(CurrentEmployment.current_employment_id)
            .where(
                CurrentEmployment.alumni_id == Alumni.alumni_id,
                CurrentEmployment.current_employer.ilike(
                    escape_like(employer), escape="\\"
                ),
            )
            .exists()
        )
        conditions.append(at_employer)
    if industry:
        in_industry = (
            select(CurrentEmployment.current_employment_id)
            .where(
                CurrentEmployment.alumni_id == Alumni.alumni_id,
                or_(
                    CurrentEmployment.current_industry.ilike(
                        escape_like(industry), escape="\\"
                    ),
                    CurrentEmployment.current_industry_secondary.ilike(
                        escape_like(industry), escape="\\"
                    ),
                ),
            )
            .exists()
        )
        conditions.append(in_industry)
    if attended_event:
        has_attended = (
            select(EventAttendance.event_attendance_id)
            .where(EventAttendance.alumni_id == Alumni.alumni_id)
            .exists()
        )
        conditions.append(has_attended)
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
    """Return a filtered page of alumni and the total count for that filter."""
    stmt = build_alumni_query(**filters)
    total = await session.scalar(select(func.count()).select_from(stmt.subquery()))
    rows = await session.scalars(
        stmt.order_by(Alumni.last_name, Alumni.alumni_id).limit(limit).offset(offset)
    )
    return list(rows.all()), int(total or 0)
