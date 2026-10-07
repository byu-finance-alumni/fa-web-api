"""Per-record version history route (#45) — read-only.

Kept out of ``routes/alumni.py`` (already ~2.7k lines) in its own module; it
shares the ``/alumni`` prefix and tag, so the API surface is unchanged in shape.
Restore is not built yet; see ``app/services/alumni_history.py``.
"""

from typing import Annotated

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.params import IdPath
from app.core.database import get_session
from app.core.rate_limit import HistoryReadRateLimit
from app.schemas.alumni_history import AlumniHistoryPage
from app.services import alumni_history

router = APIRouter(prefix="/alumni", tags=["alumni"])

SessionDep = Annotated[AsyncSession, Depends(get_session)]


@router.get("/{alumni_id}/history", response_model=AlumniHistoryPage)
async def get_alumni_history(
    alumni_id: IdPath,
    user: HistoryReadRateLimit,
    session: SessionDep,
    limit: Annotated[
        int,
        Query(ge=1, le=alumni_history.MAX_LIMIT, description="Saves per page."),
    ] = alumni_history.DEFAULT_LIMIT,
    before: Annotated[
        str | None,
        Query(
            max_length=200,
            description="Opaque cursor: the previous page's ``next_before``.",
        ),
    ] = None,
) -> AlumniHistoryPage:
    """Recorded changes to one alumnus, one entry per save, newest first.

    Editor tier: requires ``alumni.edit`` (student and up by default; NOT
    view_only). Only changes are returned — read/disclosure rows never are.
    Each field change carries its ``audit_id`` for a future restore. Values are
    scoped like the profile read for the caller's role, archived records 404,
    and the read is audit-logged (``view_history``). Field-level history begins
    2026-08-18 (``history_starts``); there is no backfill."""
    return await alumni_history.get_history(
        session,
        alumni_id,
        can_edit=user.can_edit_alumni,
        limit=limit,
        before=before,
        actor_user_id=user.user_id,
    )
