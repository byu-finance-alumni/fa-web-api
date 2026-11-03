"""Outreach interest-list routes: Conference Interest, Event Speakers, Event
Sponsors and Alumni Mentors — see ``app.services.outreach_lists`` for who is on
each list.

Gated exactly like the NetTrek reports (``alumni.export``): these are lists of
named alumni with contact details, built to be downloaded.

  * ``GET /outreach/{group}`` — one landing page's lists with their counts.
    Counts only, no PII.
  * ``GET /outreach/{group}/{key}`` — one list's rows, paginated. Spends the
    contact-list budget (shared with the NetTrek reports, counted globally from
    the audit rows it writes) and writes a disclosure-audit row. Alumni
    Mentors' per-industry list takes ``?industry=<industry>``.
  * ``GET /outreach/{group}/{key}/export`` — the whole list as CSV. Spends the
    global export budget and is audit-logged as ``export_outreach_list``.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query
from fastapi.responses import Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_session
from app.core.errors import NotFoundError
from app.core.rate_limit import (
    ContactListReadRateLimit,
    ExportReadRateLimit,
    NetTreksBrowseReadRateLimit,
)
from app.schemas.outreach import (
    OutreachGroupKey,
    OutreachGroupSummary,
    OutreachListKey,
    OutreachListPage,
)
from app.services import alumni as alumni_service
from app.services import outreach_lists as svc

router = APIRouter(prefix="/outreach", tags=["outreach"])

SessionDep = Annotated[AsyncSession, Depends(get_session)]
IndustryParam = Annotated[str | None, Query(max_length=255)]

_NO_SUCH_LIST = "There is no such Outreach list."


def _ref(
    group: OutreachGroupKey, key: OutreachListKey, industry: str | None
) -> svc.ListRef:
    try:
        return svc.make_ref(group, key, industry)
    except svc.NoSuchList:
        raise NotFoundError(_NO_SUCH_LIST) from None


@router.get("/{group}", response_model=OutreachGroupSummary)
async def outreach_summary(
    group: OutreachGroupKey,
    user: NetTreksBrowseReadRateLimit,
    session: SessionDep,
) -> OutreachGroupSummary:
    """One Outreach landing page: its lists, each with a count. Alumni Mentors
    has one ``industry`` link per industry with a mentor (most first, the
    industry in ``industry``), then ``no_industry`` if anyone is in it, then
    ``women``."""
    return await svc.get_summary(session, group)


@router.get("/{group}/{key}", response_model=OutreachListPage)
async def outreach_list(
    group: OutreachGroupKey,
    key: OutreachListKey,
    user: ContactListReadRateLimit,
    session: SessionDep,
    industry: IndustryParam = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> OutreachListPage:
    """One list's people, sorted by last name, paginated — the NetTrek report
    rows. 404 for a list the group does not have, an ``industry`` list without
    ``industry``, or ``industry`` on any other list. Gender is null for a
    caller who cannot edit alumni. Audited as ``view`` on ``outreach_list``."""
    ref = _ref(group, key, industry)
    page = await svc.list_page(
        session, ref, limit=limit, offset=offset, can_edit=user.can_edit_alumni
    )
    # After the page is built: the audit commit expires loaded ORM state.
    await alumni_service.log_read(
        session,
        actor_user_id=user.user_id,
        action="view",
        entity_type=svc.OUTREACH_ENTITY,
        detail=f"{svc.audit_detail(ref)}; offset={offset}; total={page.total}",
    )
    return page


@router.get("/{group}/{key}/export", response_model=None)
async def outreach_list_export(
    group: OutreachGroupKey,
    key: OutreachListKey,
    user: ExportReadRateLimit,
    session: SessionDep,
    industry: IndustryParam = None,
) -> Response:
    """The whole list as a CSV download, in the NetTrek reports' columns.
    Formula-injection-safe. Audit-logged as ``export_outreach_list`` and counted
    against the global export budget. Same 404s as the list. Returns
    ``text/csv`` as ``<group>_<key>[_<industry>]_<YYYY-MM-DD>.csv``."""
    ref = _ref(group, key, industry)
    csv_text = await svc.export_list_csv(
        session, ref, actor_user_id=user.user_id, can_edit=user.can_edit_alumni
    )
    return Response(
        content=csv_text,
        media_type="text/csv",
        headers={
            "Content-Disposition": (
                f'attachment; filename="{svc.export_filename(ref)}"'
            )
        },
    )
