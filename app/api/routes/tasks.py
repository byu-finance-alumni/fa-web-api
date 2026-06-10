"""Cross-alumni follow-up task list (admin Tasks page).

A read-only, paginated list of follow-up tasks across ALL alumni, each carrying
the owning alumnus's id/name and the assignee's display name. Gated to
full_access / super_admin (the cross-alumni view is an admin tool, not a
view-only read).
"""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies.auth import RequireFullAccess
from app.core.database import get_session
from app.schemas.profile import AdminTaskPage
from app.services import tasks as service

router = APIRouter(prefix="/tasks", tags=["tasks"])

SessionDep = Annotated[AsyncSession, Depends(get_session)]


@router.get("", response_model=AdminTaskPage)
async def list_tasks(
    _: RequireFullAccess,
    session: SessionDep,
    completed: Annotated[
        bool | None,
        Query(
            description=(
                "Filter by completion state: false (default) = open tasks only, "
                "true = completed only, omitted via ?completed= is treated as "
                "false. Pass all=true to include both."
            )
        ),
    ] = False,
    all: Annotated[
        bool,
        Query(description="Include tasks of every completion state."),
    ] = False,
    sort: Annotated[
        str,
        Query(
            description=(
                "Sort order: due (default, soonest due first, open before "
                "completed) | due_desc | alumni (owning alumnus A–Z) | created "
                "(newest task first) | status (open before completed). Unknown "
                "values fall back to 'due'."
            )
        ),
    ] = service.DEFAULT_SORT,
    overdue: Annotated[
        bool,
        Query(
            description=(
                "Only tasks with a due date before today that are not completed."
            )
        ),
    ] = False,
    assignee: Annotated[
        str | None,
        Query(
            description=(
                "Filter by assignee: a user id, or the literal 'unassigned' for "
                "tasks with no assignee."
            )
        ),
    ] = None,
    q: Annotated[
        str | None,
        Query(description="Case-insensitive search over task title and alumnus name."),
    ] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> AdminTaskPage:
    """Paginated cross-alumni follow-up tasks, most urgent first by default
    (open before completed, then soonest due). Defaults to open tasks only; pass
    ``all=true`` to include completed tasks too. ``sort``, ``overdue``,
    ``assignee`` and ``q`` further order/filter the set."""
    # Validate assignee: either the "unassigned" sentinel or a numeric user id.
    if assignee is not None and assignee != service.UNASSIGNED:
        try:
            int(assignee)
        except ValueError:
            raise HTTPException(
                status_code=422,
                detail="assignee must be a user id or 'unassigned'.",
            ) from None

    return await service.list_all_tasks(
        session,
        completed=None if all else completed,
        sort=sort,
        overdue=overdue,
        assignee=assignee,
        q=q,
        limit=limit,
        offset=offset,
    )
