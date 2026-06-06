"""Dashboard summary metrics — KPIs, distributions, and a recent-activity feed.

All aggregation happens in PostgreSQL (counts / group-bys), never by loading rows
into the app, so it stays within the dashboard performance budget at scale.
"""

import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

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


def _has_email_exists():
    """Correlated EXISTS: the alumnus has a personal or work email on file."""
    return (
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


def _has_employer_exists():
    """Correlated EXISTS: the alumnus has a current employer on file."""
    return (
        select(CurrentEmployment.current_employment_id)
        .where(
            CurrentEmployment.alumni_id == Alumni.alumni_id,
            CurrentEmployment.current_employer.is_not(None),
        )
        .exists()
    )


def _serialize_interaction(i, a, u) -> dict:
    return {
        "interaction_id": i.interaction_id,
        "alumni_id": i.alumni_id,
        "alumni_name": _full_name(a.first_name, a.last_name, None)
        or f"Alumni #{a.alumni_id}",
        "type": i.interaction_type,
        "when": (
            i.interaction_date_time.isoformat() if i.interaction_date_time else None
        ),
        "by": _full_name(u.first_name, u.last_name, u.email) if u else None,
    }


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
    missing_email = await session.scalar(
        select(func.count())
        .select_from(Alumni)
        .where(active, ~_has_email_exists())
    )
    missing_employer = await session.scalar(
        select(func.count())
        .select_from(Alumni)
        .where(active, ~_has_employer_exists())
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
    }


@router.get("/activity")
async def activity_feed(
    _: RequireViewAccess,
    session: SessionDep,
    q: Annotated[
        str | None,
        Query(
            description=(
                "Case-insensitive substring matched against the alumnus's "
                "first / last / preferred name OR the interaction type."
            )
        ),
    ] = None,
    type: Annotated[
        str | None,
        Query(description="Interaction type (case-insensitive exact)."),
    ] = None,
    date_from: Annotated[
        datetime.date | None,
        Query(description="Only interactions on/after this date (inclusive)."),
    ] = None,
    date_to: Annotated[
        datetime.date | None,
        Query(description="Only interactions on/before this date (inclusive)."),
    ] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> dict:
    """Paginated all-time interaction feed (newest first) — the full version
    of the dashboard's old recent-activity panel, now on its own page. Supports
    optional server-side filtering by free-text search, interaction type, and an
    inclusive date range; all filtering happens in PostgreSQL."""
    # Build the shared filter predicates once so the count and the page agree.
    conditions = []
    if q and q.strip():
        like = f"%{q.strip()}%"
        conditions.append(
            or_(
                Alumni.first_name.ilike(like),
                Alumni.last_name.ilike(like),
                Alumni.preferred_first_name.ilike(like),
                Interaction.interaction_type.ilike(like),
            )
        )
    if type and type.strip():
        conditions.append(Interaction.interaction_type.ilike(type.strip()))
    # A bare date covers the whole day: expand to full-day UTC bounds so
    # same-day interactions are included regardless of their time.
    if date_from is not None:
        conditions.append(
            Interaction.interaction_date_time
            >= datetime.datetime.combine(
                date_from, datetime.time.min, tzinfo=datetime.UTC
            )
        )
    if date_to is not None:
        conditions.append(
            Interaction.interaction_date_time
            <= datetime.datetime.combine(
                date_to, datetime.time.max, tzinfo=datetime.UTC
            )
        )

    total = await session.scalar(
        select(func.count())
        .select_from(Interaction)
        .join(Alumni, Alumni.alumni_id == Interaction.alumni_id)
        .where(*conditions)
    )
    rows = (
        await session.execute(
            select(Interaction, Alumni, User)
            .join(Alumni, Alumni.alumni_id == Interaction.alumni_id)
            .outerjoin(User, User.user_id == Interaction.user_id)
            .where(*conditions)
            .order_by(
                Interaction.interaction_date_time.desc().nullslast(),
                Interaction.interaction_id.desc(),
            )
            .limit(limit)
            .offset(offset)
        )
    ).all()
    # Distinct non-null interaction types (sorted) seed the toolbar's type
    # dropdown — independent of the active filters so every option stays
    # reachable.
    type_rows = (
        await session.execute(
            select(Interaction.interaction_type)
            .where(Interaction.interaction_type.is_not(None))
            .distinct()
            .order_by(Interaction.interaction_type)
        )
    ).all()
    return {
        "items": [_serialize_interaction(i, a, u) for i, a, u in rows],
        "types": [r[0] for r in type_rows],
        "total": int(total or 0),
        "limit": limit,
        "offset": offset,
    }


@router.get("/data-quality")
async def data_quality(_: RequireViewAccess, session: SessionDep) -> dict:
    """The data-quality alert counts (same predicates as the summary KPIs),
    for the dedicated data-quality page."""
    active = Alumni.archived.is_(False)
    total = await session.scalar(
        select(func.count()).select_from(Alumni).where(active)
    )
    missing_email = await session.scalar(
        select(func.count())
        .select_from(Alumni)
        .where(active, ~_has_email_exists())
    )
    missing_employer = await session.scalar(
        select(func.count())
        .select_from(Alumni)
        .where(active, ~_has_employer_exists())
    )
    duplicate_count = await session.scalar(
        text("SELECT count(*) FROM duplicate_candidates")
    )
    return {
        "total_alumni": int(total or 0),
        "missing_email": int(missing_email or 0),
        "missing_employer": int(missing_employer or 0),
        "duplicate_count": int(duplicate_count or 0),
    }


@router.get("/contacted-this-month")
async def contacted_this_month_list(
    _: RequireViewAccess, session: SessionDep
) -> list[dict]:
    """The alumni behind the "Contacted this month" KPI — one row per distinct
    alumnus contacted in the last 30 days, carrying their most recent
    interaction in the window (DISTINCT ON matches the KPI's distinct count)."""
    now = datetime.datetime.now(datetime.UTC)
    month_ago = now - datetime.timedelta(days=30)
    latest = (
        select(Interaction)
        .where(Interaction.interaction_date_time >= month_ago)
        .distinct(Interaction.alumni_id)
        .order_by(
            Interaction.alumni_id, Interaction.interaction_date_time.desc()
        )
        .subquery()
    )
    li = aliased(Interaction, latest)
    rows = (
        await session.execute(
            select(li, Alumni, User)
            .join(Alumni, Alumni.alumni_id == li.alumni_id)
            .outerjoin(User, User.user_id == li.user_id)
            .order_by(li.interaction_date_time.desc())
            .limit(200)
        )
    ).all()
    return [
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
        for i, a, u in rows
    ]


@router.get("/follow-ups")
async def upcoming_follow_ups_list(
    _: RequireViewAccess, session: SessionDep
) -> list[dict]:
    """The open tasks behind the "Upcoming follow-ups" KPI (incomplete, due
    today or later), soonest due first — same predicate as the KPI count."""
    today = datetime.datetime.now(datetime.UTC).date()
    rows = (
        await session.execute(
            select(FollowUpTask, Alumni, User)
            .join(Alumni, Alumni.alumni_id == FollowUpTask.alumni_id)
            .outerjoin(User, User.user_id == FollowUpTask.assigned_to_user_id)
            .where(
                FollowUpTask.completed.is_(False),
                FollowUpTask.due_date >= today,
            )
            .order_by(FollowUpTask.due_date.asc(), FollowUpTask.follow_up_task_id)
            .limit(200)
        )
    ).all()
    return [
        {
            "task_id": t.follow_up_task_id,
            "alumni_id": t.alumni_id,
            "alumni_name": _full_name(a.first_name, a.last_name, None)
            or f"Alumni #{a.alumni_id}",
            "title": t.task_title,
            "due_date": t.due_date.isoformat() if t.due_date else None,
            "assigned_to": (
                _full_name(u.first_name, u.last_name, u.email) if u else None
            ),
        }
        for t, a, u in rows
    ]
