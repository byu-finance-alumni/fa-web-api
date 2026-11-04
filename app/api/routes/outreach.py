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

from app.api.params import ReportViewParam
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
# Control characters (NUL, CR/LF, ...) are never part of an industry name;
# refusing them here returns a 422 instead of failing in the database layer.
IndustryParam = Annotated[
    str | None, Query(max_length=255, pattern=r"^[^\x00-\x1f\x7f-\x9f]*$")
]
# Far past any real list; keeps the value inside the database integer range.
MAX_OFFSET = 1_000_000

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
    view: ReportViewParam,
    industry: IndustryParam = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0, le=MAX_OFFSET)] = 0,
) -> OutreachListPage:
    """One list's people, paginated — the NetTrek report rows, sorted by last
    name, or by ``sort`` and ``dir`` with blanks last, and narrowed by the ``f_*``
    filters. ``total`` and ``filter_options`` follow the filters. 404 for a list
    the group does not have, an ``industry`` list without ``industry``, or
    ``industry`` on any other list. Gender is null for a
    caller who cannot edit alumni, and ``sort=gender`` and ``f_gender`` are
    ignored for them. Audited as ``view`` on ``outreach_list``."""
    ref = _ref(group, key, industry)
    page = await svc.list_page(
        session,
        ref,
        limit=limit,
        offset=offset,
        can_edit=user.can_edit_alumni,
        view=view,
    )
    # After the page is built: the audit commit expires loaded ORM state.
    await alumni_service.log_read(
        session,
        actor_user_id=user.user_id,
        action="view",
        entity_type=svc.OUTREACH_ENTITY,
        detail=(
            f"{svc.audit_detail(ref)}; offset={offset}; total={page.total}"
            f"{view.for_caller(can_edit=user.can_edit_alumni).audit_suffix()}"
        ),
    )
    return page


@router.get("/{group}/{key}/export", response_model=None)
async def outreach_list_export(
    group: OutreachGroupKey,
    key: OutreachListKey,
    user: ExportReadRateLimit,
    session: SessionDep,
    view: ReportViewParam,
    industry: IndustryParam = None,
) -> Response:
    """The list as a CSV download, in the NetTrek reports' columns: the whole
    list, or as the page's ``sort``, ``dir`` and ``f_*`` params order and narrow
    it. Formula-injection-safe. Audit-logged as ``export_outreach_list``, with
    any filters, and counted against the global export budget. Same 404s as the
    list. Returns ``text/csv`` as
    ``<group>_<key>[_<industry>][_filtered]_<YYYY-MM-DD>.csv``."""
    ref = _ref(group, key, industry)
    csv_text = await svc.export_list_csv(
        session,
        ref,
        actor_user_id=user.user_id,
        can_edit=user.can_edit_alumni,
        view=view,
    )
    filtered = view.for_caller(can_edit=user.can_edit_alumni).is_filtered
    filename = svc.export_filename(ref, filtered=filtered)
    return Response(
        content=csv_text,
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
