"""NetTreks tab routes (fa-web-app #887).

Amy's per-area NetTrek reports — see ``app.services.nettreks`` for how the
areas and the Hosts / Area Alumni / Other populations are computed.

Every route is gated by ``alumni.export``: the reports are lists of named alumni
with contact details, built to be downloaded, so they sit with the other files
of alumni data rather than with plain view access.

  * ``GET /nettreks`` — every report with its count. Counts only, no PII.
  * ``GET /nettreks/{area}/{kind}`` — one report's rows, paginated. Spends the
    browse budget (a page view) and writes a disclosure-audit row.
  * ``GET /nettreks/{area}/{kind}/export`` — the whole report as CSV. Spends the
    global export budget and is audit-logged as ``export_nettrek_report``.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query
from fastapi.responses import Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_session
from app.core.errors import NotFoundError
from app.core.rate_limit import ExportReadRateLimit, NetTreksBrowseReadRateLimit
from app.schemas.nettreks import (
    NetTrekAreaKey,
    NetTrekReportKind,
    NetTrekReportPage,
    NetTrekSummary,
)
from app.services import alumni as alumni_service
from app.services import nettreks as svc

router = APIRouter(prefix="/nettreks", tags=["nettreks"])

SessionDep = Annotated[AsyncSession, Depends(get_session)]

_NO_SUCH_REPORT = "Other has only a NetTrek Hosts report."


@router.get("", response_model=NetTrekSummary)
async def nettreks_summary(
    user: NetTreksBrowseReadRateLimit, session: SessionDep
) -> NetTrekSummary:
    """Every NetTrek report with its count, in landing-page order: the seven
    areas (each with a Hosts and an Area Alumni report), then Other (Hosts
    only; ``area_alumni`` is null)."""
    return await svc.get_summary(session)


@router.get("/{area}/{kind}", response_model=NetTrekReportPage)
async def nettreks_report(
    area: NetTrekAreaKey,
    kind: NetTrekReportKind,
    user: NetTreksBrowseReadRateLimit,
    session: SessionDep,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> NetTrekReportPage:
    """One report's people, sorted by last name, paginated. 404 for Other's
    Area Alumni (it has none). Gender is null for a caller who cannot edit
    alumni. Audited as ``view`` on ``nettrek_report`` (who looked at which
    report — never the rows)."""
    if not svc.report_exists(area, kind):
        raise NotFoundError(_NO_SUCH_REPORT)
    page = await svc.list_report(
        session,
        area,
        kind,
        limit=limit,
        offset=offset,
        can_edit=user.can_edit_alumni,
    )
    # After the page is built: the audit commit expires loaded ORM state.
    await alumni_service.log_read(
        session,
        actor_user_id=user.user_id,
        action="view",
        entity_type="nettrek_report",
        detail=f"report={area.value}:{kind.value}; offset={offset}; total={page.total}",
    )
    return page


@router.get("/{area}/{kind}/export", response_model=None)
async def nettreks_report_export(
    area: NetTrekAreaKey,
    kind: NetTrekReportKind,
    user: ExportReadRateLimit,
    session: SessionDep,
) -> Response:
    """The whole report as a CSV download, columns in the issue's order: First
    Name, Middle Name, Last Name, Company, Industry, Secondary Industry, Company
    City, Company State, Permanent Email, Work Email, Phone, LinkedIn URL,
    Gender, Graduation Year, Willing to Host a NetTrek (Yes/blank).
    Formula-injection-safe. Audit-logged as ``export_nettrek_report`` and
    counted against the global export budget. 404 for Other's Area Alumni.
    Returns ``text/csv`` as ``<area>_nettrek_hosts_<YYYY-MM-DD>.csv`` or
    ``<area>_area_alumni_<YYYY-MM-DD>.csv``."""
    if not svc.report_exists(area, kind):
        raise NotFoundError(_NO_SUCH_REPORT)
    csv_text = await svc.export_report_csv(
        session,
        area,
        kind,
        actor_user_id=user.user_id,
        can_edit=user.can_edit_alumni,
    )
    return Response(
        content=csv_text,
        media_type="text/csv",
        headers={
            "Content-Disposition": (
                f'attachment; filename="{svc.export_filename(area, kind)}"'
            )
        },
    )
