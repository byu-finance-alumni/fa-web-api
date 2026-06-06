"""Dashboard summary metrics — KPIs, distributions, and a recent-activity feed.

All aggregation happens in PostgreSQL (counts / group-bys), never by loading rows
into the app, so it stays within the dashboard performance budget at scale.
"""

import datetime
from typing import Annotated

from fastapi import APIRouter, Depends
from sqlalchemy import func, or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies.auth import RequireViewAccess
from app.core.database import get_session
from app.models.alumni import Alumni
from app.models.contact import AlumniContactInfo
from app.models.crm import FollowUpTask, Interaction
from app.models.employment import CurrentEmployment
from app.models.user import User

router = APIRouter(prefix="/dashboard", tags=["dashboard"])

SessionDep = Annotated[AsyncSession, Depends(get_session)]


def _full_name(first: str | None, last: str | None, email: str | None) -> str | None:
    name = " ".join(p for p in (first, last) if p).strip()
    return name or email


@router.get("/summary")
async def summary(_: RequireViewAccess, session: SessionDep) -> dict:
    """KPIs, distributions (cohort / top employers / by state), and recent
    activity for the dashboard."""
    active = Alumni.archived.is_(False)
    now = datetime.datetime.now(datetime.UTC)
    month_ago = now - datetime.timedelta(days=30)
    today = now.date()

    total = await session.scalar(
        select(func.count()).select_from(Alumni).where(active)
    )
    archived = await session.scalar(
        select(func.count()).select_from(Alumni).where(Alumni.archived.is_(True))
    )
    deceased = await session.scalar(
        select(func.count())
        .select_from(Alumni)
        .where(active, Alumni.deceased.is_(True))
    )

    # Missing-data KPIs (active alumni lacking an email / a current employer).
    # NOT EXISTS (correlated) keeps a stable query plan at scale and avoids the
    # NULL pitfalls of NOT IN.
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
    missing_email = await session.scalar(
        select(func.count()).select_from(Alumni).where(active, ~has_email)
    )
    has_employer = (
        select(CurrentEmployment.current_employment_id)
        .where(
            CurrentEmployment.alumni_id == Alumni.alumni_id,
            CurrentEmployment.current_employer.is_not(None),
        )
        .exists()
    )
    missing_employer = await session.scalar(
        select(func.count()).select_from(Alumni).where(active, ~has_employer)
    )

    contacted_this_month = await session.scalar(
        select(func.count(func.distinct(Interaction.alumni_id))).where(
            Interaction.interaction_date_time >= month_ago
        )
    )
    upcoming_follow_ups = await session.scalar(
        select(func.count())
        .select_from(FollowUpTask)
        .where(FollowUpTask.completed.is_(False), FollowUpTask.due_date >= today)
    )
    duplicate_count = await session.scalar(
        text("SELECT count(*) FROM duplicate_candidates")
    )

    cohort = (
        await session.execute(
            select(Alumni.graduation_year, func.count())
            .where(active, Alumni.graduation_year.is_not(None))
            .group_by(Alumni.graduation_year)
            .order_by(Alumni.graduation_year)
        )
    ).all()

    top_employers = (
        await session.execute(
            select(CurrentEmployment.current_employer, func.count())
            .join(Alumni, Alumni.alumni_id == CurrentEmployment.alumni_id)
            .where(active, CurrentEmployment.current_employer.is_not(None))
            .group_by(CurrentEmployment.current_employer)
            .order_by(func.count().desc())
            .limit(8)
        )
    ).all()

    by_state = (
        await session.execute(
            select(AlumniContactInfo.state, func.count())
            .join(Alumni, Alumni.alumni_id == AlumniContactInfo.alumni_id)
            .where(active, AlumniContactInfo.state.is_not(None))
            .group_by(AlumniContactInfo.state)
            .order_by(func.count().desc())
            .limit(8)
        )
    ).all()

    activity_rows = (
        await session.execute(
            select(Interaction, Alumni, User)
            .join(Alumni, Alumni.alumni_id == Interaction.alumni_id)
            .outerjoin(User, User.user_id == Interaction.user_id)
            .order_by(Interaction.interaction_date_time.desc().nullslast())
            .limit(8)
        )
    ).all()

    return {
        "total_alumni": int(total or 0),
        "archived": int(archived or 0),
        "deceased": int(deceased or 0),
        "missing_email": int(missing_email or 0),
        "missing_employer": int(missing_employer or 0),
        "contacted_this_month": int(contacted_this_month or 0),
        "upcoming_follow_ups": int(upcoming_follow_ups or 0),
        "duplicate_count": int(duplicate_count or 0),
        "by_graduation_year": [{"year": r[0], "count": int(r[1])} for r in cohort],
        "top_employers": [
            {"employer": r[0], "count": int(r[1])} for r in top_employers
        ],
        "by_state": [{"state": r[0], "count": int(r[1])} for r in by_state],
        "recent_activity": [
            {
                "interaction_id": i.interaction_id,
                "alumni_id": i.alumni_id,
                "alumni_name": _full_name(a.first_name, a.last_name, None)
                or f"Alumni #{a.alumni_id}",
                "type": i.interaction_type,
                "when": (
                    i.interaction_date_time.isoformat()
                    if i.interaction_date_time
                    else None
                ),
                "by": _full_name(u.first_name, u.last_name, u.email) if u else None,
            }
            for i, a, u in activity_rows
        ],
    }
