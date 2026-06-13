"""Cross-alumni follow-up task listing (admin Tasks page).

Assembles a paginated, read-only view of follow-up tasks across ALL alumni for
the admin "Tasks" page. Joins the owning alumnus and the assignee user in a
single query (no N+1): one statement for the page rows, one for the matching
total. Ordering surfaces the most actionable work first — open tasks before
completed ones, then soonest due date — but the caller can override the sort and
narrow the set with overdue / assignee / search filters.
"""

from __future__ import annotations

import datetime

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.alumni import Alumni
from app.models.crm import FollowUpTask
from app.models.user import User
from app.schemas.profile import AdminTaskItem, AdminTaskPage

# Allowed values for the ``sort`` param. Anything else is coerced to the default
# (``DEFAULT_SORT``) rather than raising — keeps deep links resilient.
ALLOWED_SORTS = ("due", "due_desc", "alumni", "created", "status")
DEFAULT_SORT = "due"

# Sentinel for the ``assignee`` filter meaning "no assignee on the task".
UNASSIGNED = "unassigned"


def _full_name(first: str | None, last: str | None, email: str | None) -> str | None:
    name = " ".join(p for p in (first, last) if p).strip()
    return name or email


def _order_by(sort: str):
    """Return the ORDER BY column list for a (validated) ``sort`` value.

    Every ordering keeps open-before-completed as the leading tiebreaker is NOT
    applied uniformly — only the default/``due`` family leads with completion so
    the most urgent open work surfaces first; explicit sorts honour the user's
    chosen key first, with a stable ``follow_up_task_id`` tiebreaker.
    """
    alumni_name = func.lower(
        func.coalesce(Alumni.first_name, "") + " " + func.coalesce(Alumni.last_name, "")
    )
    newest = FollowUpTask.follow_up_task_id.desc()
    if sort == "due":
        # Default: open before completed, then soonest due (nulls last).
        return (
            FollowUpTask.completed.asc(),
            FollowUpTask.due_date.asc().nullslast(),
            newest,
        )
    if sort == "due_desc":
        return (
            FollowUpTask.completed.asc(),
            FollowUpTask.due_date.desc().nullslast(),
            newest,
        )
    if sort == "alumni":
        return (alumni_name.asc(), newest)
    if sort == "created":
        return (FollowUpTask.follow_up_task_id.desc(),)
    if sort == "status":
        # Open (False) before completed (True), then soonest due.
        return (
            FollowUpTask.completed.asc(),
            FollowUpTask.due_date.asc().nullslast(),
            newest,
        )
    # Unreachable once the caller validates, but be defensive.
    return _order_by(DEFAULT_SORT)


async def list_all_tasks(
    session: AsyncSession,
    *,
    completed: bool | None = False,
    sort: str = DEFAULT_SORT,
    overdue: bool = False,
    assignee: str | None = None,
    q: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> AdminTaskPage:
    """Return a page of follow-up tasks across all alumni.

    ``completed`` filters by completion state: ``False`` (the default) shows
    only open tasks, ``True`` shows only completed tasks, ``None`` shows all.

    ``sort`` is one of :data:`ALLOWED_SORTS`; any other value is coerced to
    :data:`DEFAULT_SORT` (``"due"``). The default ordering puts open tasks
    before completed ones, then soonest due date (nulls last), then newest task
    id.

    ``overdue`` (when True) restricts to tasks whose ``due_date`` is strictly
    before today AND not completed. ``assignee`` filters by
    ``assigned_to_user_id`` (numeric string) or the literal ``"unassigned"`` for
    tasks with no assignee. ``q`` is a case-insensitive substring match over the
    task title OR the owning alumnus's first/last name.
    """
    if sort not in ALLOWED_SORTS:
        sort = DEFAULT_SORT

    conditions = []
    if completed is not None:
        conditions.append(FollowUpTask.completed.is_(completed))

    if overdue:
        conditions.append(FollowUpTask.due_date < datetime.date.today())
        conditions.append(FollowUpTask.completed.is_(False))

    if assignee is not None:
        if assignee == UNASSIGNED:
            conditions.append(FollowUpTask.assigned_to_user_id.is_(None))
        else:
            conditions.append(FollowUpTask.assigned_to_user_id == int(assignee))

    if q:
        like = f"%{q}%"
        conditions.append(
            or_(
                FollowUpTask.task_title.ilike(like),
                Alumni.first_name.ilike(like),
                Alumni.last_name.ilike(like),
                (
                    func.coalesce(Alumni.first_name, "")
                    + " "
                    + func.coalesce(Alumni.last_name, "")
                ).ilike(like),
            )
        )

    # The total counts rows after the same joins/filters. The ``q`` filter
    # references Alumni columns, so the count must join Alumni too — keep both
    # statements structurally identical (single joined query each, no N+1).
    total = await session.scalar(
        select(func.count())
        .select_from(FollowUpTask)
        .join(Alumni, Alumni.alumni_id == FollowUpTask.alumni_id)
        .where(*conditions)
    )
    rows = (
        await session.execute(
            select(FollowUpTask, Alumni, User)
            .join(Alumni, Alumni.alumni_id == FollowUpTask.alumni_id)
            .outerjoin(User, User.user_id == FollowUpTask.assigned_to_user_id)
            .where(*conditions)
            .order_by(*_order_by(sort))
            .limit(limit)
            .offset(offset)
        )
    ).all()

    items = [
        AdminTaskItem(
            follow_up_task_id=t.follow_up_task_id,
            alumni_id=t.alumni_id,
            alumni_name=_full_name(a.first_name, a.last_name, None)
            or f"Alumni #{a.alumni_id}",
            task_title=t.task_title,
            due_date=t.due_date,
            completed=t.completed,
            completed_at=t.completed_at,
            task_notes=t.task_notes,
            assigned_to_user_id=t.assigned_to_user_id,
            assigned_to=(
                _full_name(u.first_name, u.last_name, u.email) if u else None
            ),
        )
        for t, a, u in rows
    ]
    return AdminTaskPage(
        items=items, total=int(total or 0), limit=limit, offset=offset
    )
