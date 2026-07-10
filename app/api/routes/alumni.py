"""Alumni CRUD routes.

Reads require view access (any role). Editing an EXISTING alumnus and their
nested records (interactions, employment, education, leadership, tags, status
labels, tasks, event attendance) requires edit access (``student`` and up, via
``RequireAlumniEdit``). Creating a new alumnus, archiving/restoring, and CSV
import require ``full_access`` and up (``RequireFullAccess``) — ``student`` is
deliberately excluded from those. ``DELETE`` on an alumnus is a soft-delete
(archive), never a hard delete — audit history depends on retained records.
"""

import datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, File, Query, UploadFile, status
from fastapi.responses import JSONResponse, Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies.auth import (
    RequireAlumniEdit,
    RequireFullAccess,
    RequireViewAccess,
)
from app.api.params import IdPath
from app.core.database import get_session
from app.core.errors import InvalidRequestError, NotFoundError
from app.core.rate_limit import (
    EmploymentWriteRateLimit,
    HeadshotWriteRateLimit,
    InteractionWriteRateLimit,
    TaskWriteRateLimit,
)
from app.core.security import AuthorizationError
from app.models.alumni import Alumni
from app.repositories.alumni import SURVEY_CADENCE
from app.schemas.alumni import (
    AlumniCreateFull,
    AlumniListItem,
    AlumniPage,
    AlumniRead,
    AlumniUpdateFull,
    minimize_alumni_read,
)
from app.schemas.alumni_export import AlumniExportRequest, ExportColumnCatalog
from app.schemas.filters import FilterOptions
from app.schemas.imports import (
    AlumniHygienePreview,
    AlumniImportPreview,
    AlumniImportResult,
)
from app.schemas.profile import (
    EducationCreate,
    EducationRead,
    EducationUpdate,
    EmploymentHistoryCreate,
    EmploymentHistoryRead,
    EmploymentHistoryUpdate,
    EventAttendanceCreate,
    EventAttendedRead,
    InteractionCreate,
    InteractionRead,
    InteractionUpdate,
    LeadershipCreate,
    LeadershipRead,
    LeadershipUpdate,
    ProfileRead,
    StatusLabelCreate,
    TagCreate,
    TaskCompleteUpdate,
    TaskCreate,
    TaskRead,
)
from app.services import alumni as service
from app.services import alumni_export, hygiene, import_csv, supabase_storage
from app.services import profile as profile_service

router = APIRouter(prefix="/alumni", tags=["alumni"])

SessionDep = Annotated[AsyncSession, Depends(get_session)]


@router.get("", response_model=AlumniPage)
async def list_alumni(
    user: RequireViewAccess,
    session: SessionDep,
    q: Annotated[
        str | None,
        Query(description="Search names and external ids (case-insensitive)."),
    ] = None,
    net_id: Annotated[
        str | None,
        Query(description="Net ID — case-insensitive partial match."),
    ] = None,
    first_name: Annotated[
        str | None,
        Query(description="First name — case-insensitive partial match."),
    ] = None,
    last_name: Annotated[
        str | None,
        Query(description="Last name — case-insensitive partial match."),
    ] = None,
    preferred_name: Annotated[
        str | None,
        Query(description="Preferred first name — case-insensitive partial match."),
    ] = None,
    email: Annotated[
        str | None,
        Query(description="Email (personal or work) — case-insensitive partial match."),
    ] = None,
    graduation_year: int | None = None,
    grad_year_min: int | None = None,
    grad_year_max: int | None = None,
    deceased: Annotated[bool | None, Query(description="Filter by deceased flag.")] = None,
    employer: Annotated[
        list[str] | None,
        Query(description="Current employer(s) — repeatable (OR), exact match."),
    ] = None,
    past_employer: Annotated[
        list[str] | None,
        Query(description="Prior employer(s) from employment history — repeatable."),
    ] = None,
    industry: Annotated[
        list[str] | None,
        Query(description="Industry / work area (primary or secondary) — repeatable."),
    ] = None,
    title: Annotated[
        list[str] | None,
        Query(description="Current job title(s) — repeatable, exact match."),
    ] = None,
    seniority: Annotated[
        list[str] | None,
        Query(description="Seniority level(s) — repeatable, exact match."),
    ] = None,
    city: Annotated[
        list[str] | None,
        Query(description="Current city/cities — repeatable, exact match."),
    ] = None,
    state: Annotated[
        list[str] | None,
        Query(description="Current state(s) — repeatable, exact match."),
    ] = None,
    tag: Annotated[
        list[str] | None,
        Query(description="Engagement tag(s) — repeatable, exact match."),
    ] = None,
    status_label: Annotated[
        list[str] | None,
        Query(description="Status label(s) — repeatable, exact match."),
    ] = None,
    leadership_role: Annotated[
        list[str] | None,
        Query(description="Finance Society leadership role(s) — repeatable."),
    ] = None,
    survey_status: Annotated[
        list[str] | None,
        Query(description="Survey status value(s) — repeatable, exact match."),
    ] = None,
    needs_survey: Annotated[
        bool,
        Query(
            description=(
                "'Needs surveying' view (admin tier and up only): alumni DUE for "
                "the biennial survey — never completed one, or whose most-recent "
                "completion is older than 2 years. The 2-year threshold is "
                "computed server-side. Forbidden for student / view_only roles "
                "(403)."
            )
        ),
    ] = False,
    contacted_after: Annotated[
        datetime.date | None,
        Query(description="Only alumni with an interaction on/after this date."),
    ] = None,
    contacted_before: Annotated[
        datetime.date | None,
        Query(description="Only alumni NOT contacted since this date (stale)."),
    ] = None,
    never_contacted: Annotated[
        bool, Query(description="Only alumni with no logged interactions.")
    ] = False,
    attended_event: Annotated[
        bool, Query(description="Only alumni who attended at least one event.")
    ] = False,
    spoke_after: Annotated[
        datetime.date | None,
        Query(
            description=(
                "Only alumni who served as a guest speaker at an event held "
                "on/after this date (matches the dashboard 'Guest speakers this "
                "month' KPI)."
            )
        ),
    ] = None,
    spoke_before: Annotated[
        datetime.date | None,
        Query(
            description=(
                "Only alumni who served as a guest speaker at an event held "
                "on/before this date."
            )
        ),
    ] = None,
    donor: Annotated[bool, Query(description="Only PIFF donors.")] = False,
    mentor_willing: Annotated[bool, Query(description="Only alumni willing to mentor.")] = False,
    guest_speaker_willing: Annotated[
        bool, Query(description="Only alumni willing to guest speak.")
    ] = False,
    cfa: Annotated[
        bool, Query(description="Only alumni holding the CFA designation.")
    ] = False,
    cpa: Annotated[
        bool, Query(description="Only alumni holding the CPA designation.")
    ] = False,
    graduate_degree: Annotated[
        bool, Query(description="Only alumni with a graduate degree recorded.")
    ] = False,
    missing_email: Annotated[
        bool,
        Query(description="Only alumni with no contact-info email on file."),
    ] = False,
    missing_employer: Annotated[
        bool,
        Query(description="Only alumni with no current employer on file."),
    ] = False,
    missing_phone: Annotated[
        bool,
        Query(description="Only alumni with no phone number on file."),
    ] = False,
    duplicate: Annotated[
        bool,
        Query(description="Only alumni flagged as duplicate candidates."),
    ] = False,
    include_archived: bool = False,
    kind: Annotated[
        Literal["alumni", "friend", "all"],
        Query(
            description=(
                "Which records to return (#218): 'alumni' (default) — only "
                "graduates (is_alumni=true); 'friend' — only friends of the "
                "program (is_alumni=false); 'all' — both. Defaults to 'alumni' so "
                "the Alumni page is unchanged."
            )
        ),
    ] = "alumni",
    sort: Annotated[
        Literal["name", "grad_desc", "grad_asc"],
        Query(description="Sort order: name | grad_desc | grad_asc."),
    ] = "name",
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> AlumniPage:
    # Archived rows are full_access-and-up only: a view_only / student caller
    # passing ``include_archived=true`` must NOT receive soft-deleted records.
    has_full_access = user.is_full_access or user.is_super_admin or user.is_engineer
    effective_include_archived = include_archived and has_full_access
    # The exact-email filter is a contact-PII enumeration oracle: a low-privilege
    # (view_only / student) caller could confirm an email belongs to a specific
    # alumnus even though no response body exposes that email to them. Gate it to
    # full_access-and-up; below that it's silently ignored (AND'd away like
    # include_archived) rather than 422'd, so a stray param can't leak.
    email = email if has_full_access else None
    # "Needs surveying" is an admin-tier view (engineer / super_admin /
    # full_access = "admin"). student and view_only ("professor") are denied
    # server-side — a 403, not a silent ignore, so the access decision is
    # explicit and audit-visible rather than relying on the UI to hide the tile.
    survey_due_before: datetime.datetime | None = None
    if needs_survey:
        if not has_full_access:
            raise AuthorizationError(
                "The 'needs surveying' view is restricted to admin users."
            )
        survey_due_before = datetime.datetime.now(datetime.UTC) - SURVEY_CADENCE
    # Map the friends/alumni split (#218) to the repository's tri-state filter:
    # alumni-only (True), friends-only (False), or both (None).
    is_alumni_filter = {"alumni": True, "friend": False, "all": None}[kind]
    items, total = await service.list_alumni(
        session,
        limit=limit,
        offset=offset,
        q=q,
        net_id=net_id,
        first_name=first_name,
        last_name=last_name,
        preferred_name=preferred_name,
        email=email,
        graduation_year=graduation_year,
        grad_year_min=grad_year_min,
        grad_year_max=grad_year_max,
        deceased=deceased,
        employer=employer,
        past_employer=past_employer,
        industry=industry,
        title=title,
        seniority=seniority,
        city=city,
        state=state,
        tag=tag,
        status_label=status_label,
        leadership_role=leadership_role,
        survey_status=survey_status,
        needs_survey=needs_survey,
        survey_due_before=survey_due_before,
        contacted_after=contacted_after,
        contacted_before=contacted_before,
        never_contacted=never_contacted,
        attended_event=attended_event,
        spoke_after=spoke_after,
        spoke_before=spoke_before,
        donor=donor,
        mentor_willing=mentor_willing,
        guest_speaker_willing=guest_speaker_willing,
        cfa=cfa,
        cpa=cpa,
        graduate_degree=graduate_degree,
        missing_email=missing_email,
        missing_employer=missing_employer,
        missing_phone=missing_phone,
        duplicate=duplicate,
        is_alumni=is_alumni_filter,
        include_archived=effective_include_archived,
        sort=sort,
    )
    # Search/disclosure audit: record the actor + a short filter summary (never
    # the result payload). One lightweight row per search.
    await service.log_search(
        session,
        actor_user_id=user.user_id,
        filters={
            "q": q,
            "net_id": net_id,
            "first_name": first_name,
            "last_name": last_name,
            "preferred_name": preferred_name,
            "email": email,
            "graduation_year": graduation_year,
            "grad_year_min": grad_year_min,
            "grad_year_max": grad_year_max,
            "employer": "|".join(employer) if employer else None,
            "past_employer": "|".join(past_employer) if past_employer else None,
            "industry": "|".join(industry) if industry else None,
            "title": "|".join(title) if title else None,
            "seniority": "|".join(seniority) if seniority else None,
            "city": "|".join(city) if city else None,
            "state": "|".join(state) if state else None,
            "tag": "|".join(tag) if tag else None,
            "status_label": "|".join(status_label) if status_label else None,
            "leadership_role": "|".join(leadership_role) if leadership_role else None,
            "survey_status": "|".join(survey_status) if survey_status else None,
            "needs_survey": needs_survey or None,
            "contacted_after": contacted_after.isoformat() if contacted_after else None,
            "contacted_before": (contacted_before.isoformat() if contacted_before else None),
            "never_contacted": never_contacted or None,
            "cfa": cfa or None,
            "cpa": cpa or None,
            "graduate_degree": graduate_degree or None,
            "spoke_after": spoke_after.isoformat() if spoke_after else None,
            "spoke_before": spoke_before.isoformat() if spoke_before else None,
            # Only record the friends/alumni split when it deviates from the
            # default alumni-only view (keeps the audit summary terse).
            "kind": kind if kind != "alumni" else None,
            "limit": limit,
            "offset": offset,
            "sort": sort,
        },
    )
    rows = [
        minimize_alumni_read(AlumniListItem.model_validate(item), can_edit=user.can_edit_alumni)
        for item in items
    ]
    return AlumniPage(items=rows, total=total, limit=limit, offset=offset)


@router.get("/filter-options", response_model=FilterOptions)
async def alumni_filter_options(_: RequireViewAccess, session: SessionDep) -> FilterOptions:
    """Distinct option lists for the advanced-filter panel's multi-selects.

    Declared before the ``/{alumni_id}`` routes so the literal path always wins
    (``alumni_id`` is int-typed, so a non-numeric segment can't match it anyway).
    """
    return FilterOptions.model_validate(await service.filter_options(session))


# --- Bulk CSV import (full_access) -------------------------------------------
#
# Declared BEFORE the ``/{alumni_id}`` routes so the literal ``/import/...``
# paths win over the ``/{alumni_id}`` / ``/{alumni_id}/preview`` patterns (route
# matching is declaration-ordered).


async def _read_capped(file: UploadFile, cap: int = import_csv.MAX_UPLOAD_BYTES) -> bytes | None:
    """Read an upload, capped at ``cap`` bytes (default ``MAX_UPLOAD_BYTES``).

    Reads one byte past the cap so we can tell "exactly at the limit" from "over".
    Returns the bytes, or ``None`` if the file exceeds the cap (the caller turns
    that into a 413 response). Bounds memory before any parsing happens (DoS).
    """
    data = await file.read(cap + 1)
    if len(data) > cap:
        return None
    return data


def _too_large_response(cap: int = import_csv.MAX_UPLOAD_BYTES) -> JSONResponse:
    mib = cap // (1024 * 1024)
    return JSONResponse(
        status_code=413,  # Content Too Large
        content={
            "error": {
                "code": "payload_too_large",
                "message": (f"File exceeds the {mib} MB upload limit. Split into smaller batches."),
            }
        },
    )


# --- Headshots ---------------------------------------------------------------

_HEADSHOT_BUCKET = "headshots"
# We gate the type here too (the bucket enforces the same allow-list) so a bad
# type never reaches storage.
_HEADSHOT_MIME_TYPES = frozenset({"image/jpeg", "image/png", "image/webp"})
# Headshots get a larger cap than CSV imports — high-res phone photos are easily
# several MB. Independent of ``MAX_UPLOAD_BYTES`` so it doesn't loosen imports.
_HEADSHOT_MAX_BYTES = 20 * 1024 * 1024  # 20 MiB


async def _alumnus_net_id(session: AsyncSession, alumni_id: int) -> str:
    """Return the alumnus's net_id (the headshot object key), or raise 404/400."""
    alumnus = await session.scalar(select(Alumni).where(Alumni.alumni_id == alumni_id))
    if alumnus is None:
        raise NotFoundError(f"Alumni {alumni_id} not found.")
    net_id = (alumnus.net_id or "").strip()
    if not net_id:
        raise InvalidRequestError(
            "This alumnus has no net ID; a headshot is stored under the net ID."
        )
    return net_id


@router.put("/{alumni_id}/headshot", status_code=204)
async def upload_headshot(
    alumni_id: IdPath,
    user: RequireFullAccess,
    session: SessionDep,
    file: Annotated[UploadFile, File()],
) -> Response:
    """Upload or replace an alumnus's headshot (full_access and up).

    Stored PRIVATELY in the ``headshots`` bucket, keyed by the alumnus's net ID,
    overwriting any existing image. Only JPEG/PNG/WebP within the upload cap are
    accepted; the image is only ever served back via a short-lived signed URL.
    """
    content_type = (file.content_type or "").split(";")[0].strip().lower()
    if content_type not in _HEADSHOT_MIME_TYPES:
        raise InvalidRequestError("Headshot must be a JPEG, PNG, or WebP image.")
    data = await _read_capped(file, _HEADSHOT_MAX_BYTES)
    if data is None:
        return _too_large_response(_HEADSHOT_MAX_BYTES)
    if not data:
        raise InvalidRequestError("The uploaded image is empty.")
    net_id = await _alumnus_net_id(session, alumni_id)
    await supabase_storage.upload_object(_HEADSHOT_BUCKET, net_id, data, content_type)
    service._audit(session, user.user_id, "upload_headshot", alumni_id, new_value=net_id)
    await session.commit()
    return Response(status_code=204)


@router.post("/{alumni_id}/headshot/upload-url")
async def create_headshot_upload_url(
    alumni_id: IdPath,
    user: HeadshotWriteRateLimit,
    session: SessionDep,
) -> dict:
    """Mint a short-lived signed URL the browser PUTs the image to DIRECTLY
    (full_access and up). This bypasses the ~4.5 MB request-body cap on our
    serverless functions, so headshots up to the bucket limit (20 MB) work.

    The token is scoped to exactly this alumnus's object key (their net ID), so
    the browser can only write that one path and never sees the service key.
    Supabase enforces the bucket's size + JPEG/PNG/WebP allow-list on the PUT.

    We log an ``upload_headshot_started`` audit HERE: minting is the necessary
    precondition for any image change and is fully attributable, so the FERPA
    trail can't be lost if the browser never reaches confirm (dropped connection
    / closed tab). Confirm writes the terminal ``upload_headshot`` (success) or
    ``upload_headshot_rejected`` once the object is validated, so this "started"
    row never masquerades as a completed, conforming upload.
    """
    net_id = await _alumnus_net_id(session, alumni_id)
    upload_url = await supabase_storage.create_signed_upload_url(_HEADSHOT_BUCKET, net_id)
    service._audit(
        session, user.user_id, "upload_headshot_started", alumni_id, new_value=net_id
    )
    await session.commit()
    return {"upload_url": upload_url, "object_key": net_id}


@router.post("/{alumni_id}/headshot/confirm", status_code=204)
async def confirm_headshot_upload(
    alumni_id: IdPath,
    user: HeadshotWriteRateLimit,
    session: SessionDep,
) -> Response:
    """Validate + record the outcome of a direct-to-storage headshot upload
    (full_access and up). Writes the terminal audit so the trail reflects reality:
    ``upload_headshot`` when a conforming object is present, or
    ``upload_headshot_rejected`` when the uploaded object violates the contract
    (the attribution for the attempt is already on the mint's
    ``upload_headshot_started`` row).

    Defense-in-depth: the bucket's own allow-list/size-limit is the primary guard
    on the direct PUT, but we re-check the object's type + size here and delete
    anything outside the contract, so a bucket misconfig can't silently let a bad
    file through. The probe FAILS OPEN — if it can't read the object we fall back
    to a plain existence check rather than reject a legitimate upload."""
    net_id = await _alumnus_net_id(session, alumni_id)
    content_type, size = await supabase_storage.probe_object(_HEADSHOT_BUCKET, net_id)
    if content_type is None and size is None:
        # Couldn't read metadata — confirm the object at least exists so a
        # never-uploaded key can't be "confirmed", then record completion.
        if await supabase_storage.create_signed_url(_HEADSHOT_BUCKET, net_id) is None:
            raise InvalidRequestError("No uploaded image was found to confirm.")
    elif content_type is not None and content_type not in _HEADSHOT_MIME_TYPES:
        await supabase_storage.delete_object(_HEADSHOT_BUCKET, net_id)
        service._audit(
            session,
            user.user_id,
            "upload_headshot_rejected",
            alumni_id,
            field_name="content_type",
            new_value=content_type,
        )
        await session.commit()
        raise InvalidRequestError("Headshot must be a JPEG, PNG, or WebP image.")
    elif size is not None and size > _HEADSHOT_MAX_BYTES:
        await supabase_storage.delete_object(_HEADSHOT_BUCKET, net_id)
        service._audit(
            session,
            user.user_id,
            "upload_headshot_rejected",
            alumni_id,
            field_name="size",
            new_value=size,
        )
        await session.commit()
        return _too_large_response(_HEADSHOT_MAX_BYTES)
    service._audit(session, user.user_id, "upload_headshot", alumni_id, new_value=net_id)
    await session.commit()
    return Response(status_code=204)


@router.get("/{alumni_id}/headshot")
async def get_headshot(
    alumni_id: IdPath,
    user: RequireViewAccess,
    session: SessionDep,
) -> dict:
    """Return a short-lived signed URL for the alumnus's headshot, or
    ``{"url": null}`` when none is set. Any authenticated view role may fetch it
    (the headshot shows on the profile); the bucket is private so the signed URL
    is the only way to view the image and it expires within the hour."""
    alumnus = await session.scalar(select(Alumni).where(Alumni.alumni_id == alumni_id))
    if alumnus is None:
        raise NotFoundError(f"Alumni {alumni_id} not found.")
    net_id = (alumnus.net_id or "").strip()
    if not net_id:
        return {"url": None}
    return {"url": await supabase_storage.create_signed_url(_HEADSHOT_BUCKET, net_id)}


@router.delete("/{alumni_id}/headshot", status_code=204)
async def delete_headshot(
    alumni_id: IdPath,
    user: RequireFullAccess,
    session: SessionDep,
) -> Response:
    """Remove an alumnus's headshot (full_access and up). A missing image is a
    no-op (still 204)."""
    net_id = await _alumnus_net_id(session, alumni_id)
    await supabase_storage.delete_object(_HEADSHOT_BUCKET, net_id)
    service._audit(session, user.user_id, "delete_headshot", alumni_id, old_value=net_id)
    await session.commit()
    return Response(status_code=204)


@router.get("/import/template")
async def alumni_import_template(
    _: RequireFullAccess,
    kind: Annotated[Literal["alumni", "friend"], Query()] = "alumni",
) -> Response:
    """Download the bulk-import CSV template (full_access). ``kind=alumni`` (the
    default) returns the full Alumni columns; ``kind=friend`` returns the curated
    friend (non-alumni contact) column set (#294). Same column source as the xlsx
    intake template."""
    friend = kind == "friend"
    filename = "friend_import_template.csv" if friend else "alumni_import_template.csv"
    return Response(
        content=import_csv.build_template_csv(friend=friend),
        media_type="text/csv",
        headers={"Content-Disposition": (f'attachment; filename="{filename}"')},
    )


@router.post("/import/preview", response_model=AlumniImportPreview)
async def preview_import_alumni(
    _: RequireFullAccess,
    session: SessionDep,
    file: Annotated[UploadFile, File()],
    kind: Annotated[Literal["alumni", "friend"], Query()] = "alumni",
) -> dict | JSONResponse:
    """Dry-run a bulk CSV import (full_access, NO writes).

    Parses + maps the uploaded CSV against the template columns for ``kind``
    (``alumni`` default, or ``friend`` for non-alumni contacts, #294), then
    evaluates every row (clean + duplicate-detect against the DB and earlier
    rows in the file + completeness warnings). Returns the full preview report;
    a bad header set surfaces as ``columns_ok: false`` with ``header_errors``."""
    file_bytes = await _read_capped(file)
    if file_bytes is None:
        return _too_large_response()
    rows, header_errors = import_csv.parse_and_map(
        file_bytes, max_rows=import_csv.MAX_IMPORT_ROWS, friend=kind == "friend"
    )
    if header_errors:
        return {
            "columns_ok": False,
            "header_errors": header_errors,
            "summary": {
                "total": 0,
                "importable": 0,
                "rejected": 0,
                "with_warnings": 0,
                "cleaned": 0,
            },
            "rows": [],
        }
    return await import_csv.evaluate(session, rows)


@router.post("/import", response_model=AlumniImportResult)
async def import_alumni(
    user: RequireFullAccess,
    session: SessionDep,
    file: Annotated[UploadFile, File()],
    kind: Annotated[Literal["alumni", "friend"], Query()] = "alumni",
) -> dict | JSONResponse:
    """Commit a bulk CSV import (full_access). ``kind=friend`` imports non-alumni
    contacts (``is_alumni=false``, #294); ``kind=alumni`` (default) imports
    alumni. Re-evaluates and inserts every importable row in one transaction
    (audit logging fires per row); rejected rows are skipped and reported. A bad
    header set imports nothing."""
    file_bytes = await _read_capped(file)
    if file_bytes is None:
        return _too_large_response()
    rows, header_errors = import_csv.parse_and_map(
        file_bytes, max_rows=import_csv.MAX_IMPORT_ROWS, friend=kind == "friend"
    )
    if header_errors:
        return {
            "imported": 0,
            "skipped": 0,
            "created_ids": [],
            "rejects": [{"row": 0, "name": "(header)", "reason": msg} for msg in header_errors],
        }
    return await import_csv.commit_import(session, rows, actor_user_id=user.user_id)


# --- Customizable CSV export (full_access) -----------------------------------
#
# Declared BEFORE the ``/{alumni_id}`` routes so the literal ``/export/...`` paths
# win over the ``/{alumni_id}`` pattern (route matching is declaration-ordered).


@router.get("/export/columns", response_model=ExportColumnCatalog)
async def alumni_export_columns(_: RequireFullAccess) -> ExportColumnCatalog:
    """The catalog of exportable columns + the default-checked selection, for the
    export column picker (full_access)."""
    return alumni_export.build_catalog()


@router.post("/export", response_model=None)
async def export_alumni(
    payload: AlumniExportRequest,
    user: RequireFullAccess,
    session: SessionDep,
) -> Response | JSONResponse:
    """Export the filtered alumni list as CSV with the chosen columns
    (full_access). Hits the SAME population the list view shows (same filters).
    An unknown column key is a 422; a result set larger than the export cap is a
    413 asking the caller to narrow filters. Audit-logged (``export_alumni``)."""
    try:
        columns = alumni_export.validate_columns(payload.columns)
    except ValueError as exc:
        return JSONResponse(
            status_code=422,
            content={"error": {"code": "validation_error", "message": str(exc)}},
        )
    total = await alumni_export.count_matching(session, payload.filters)
    if total > alumni_export.MAX_EXPORT_ROWS:
        return JSONResponse(
            status_code=413,
            content={
                "error": {
                    "code": "payload_too_large",
                    "message": (
                        f"This export matches {total:,} alumni, over the "
                        f"{alumni_export.MAX_EXPORT_ROWS:,}-row limit. Narrow the "
                        "filters and try again."
                    ),
                }
            },
        )
    csv_text = await alumni_export.export_csv(
        session,
        columns=columns,
        filters=payload.filters,
        actor_user_id=user.user_id,
    )
    return Response(
        content=csv_text,
        media_type="text/csv",
        headers={"Content-Disposition": 'attachment; filename="alumni_export.csv"'},
    )


@router.get("/{alumni_id}", response_model=AlumniRead)
async def get_alumni(alumni_id: IdPath, user: RequireViewAccess, session: SessionDep) -> AlumniRead:
    """Single lightweight alumni core record.

    Archived records 404 (they were removed from the directory). view_only
    ("Professor") callers receive a FERPA-minimized record — sensitive PII,
    notes, and import provenance are nulled. This lightweight read is not
    audit-logged (the full profile aggregate is)."""
    alumnus = await service.get_alumni(session, alumni_id)
    return minimize_alumni_read(AlumniRead.model_validate(alumnus), can_edit=user.can_edit_alumni)


@router.get("/{alumni_id}/profile", response_model=ProfileRead)
async def get_alumni_profile(
    alumni_id: IdPath, user: RequireViewAccess, session: SessionDep
) -> ProfileRead:
    """Full profile aggregate (core + contact, career, employment, leadership,
    engagement, surveys, interactions, tasks, attachments, audit) for the tabs.

    Archived records 404. Follow-up tasks are edit-only: view_only ("Professor")
    users get an empty ``tasks`` list AND a FERPA-minimized aggregate (sensitive
    PII nulled, free-text notes and audit trail stripped) — enforced here, not
    just hidden in the UI. Anyone with edit access — engineer / super_admin /
    full_access / student — sees all. The disclosure is audit-logged
    (``view_profile``)."""
    include_tasks = user.can_edit_alumni
    return await profile_service.get_profile(
        session,
        alumni_id,
        include_tasks=include_tasks,
        can_edit=user.can_edit_alumni,
        actor_user_id=user.user_id,
    )


@router.get(
    "/{alumni_id}/export",
    response_model=ProfileRead,
    # The exported body is the full profile aggregate MINUS the embedded audit
    # trail (the service drops it). Exclude it here too so the declared model
    # matches the actual output exactly and the ``audit`` key is never re-added.
    response_model_exclude={"audit"},
)
async def export_alumni_profile(
    alumni_id: IdPath, user: RequireFullAccess, session: SessionDep
) -> dict:
    """Server-side, audited profile export (full_access).

    Returns the full profile aggregate as a MINIMIZED JSON body: the embedded
    ``audit`` trail is excluded and internal user PKs (interaction ``user_id``,
    task ``assigned_to_user_id``) are never present. Writes an ``export_profile``
    audit row before returning. Archived records 404. The frontend calls this
    instead of doing a client-side export."""
    return await profile_service.export_profile(session, alumni_id, actor_user_id=user.user_id)


@router.post(
    "/{alumni_id}/interactions",
    response_model=InteractionRead,
    status_code=status.HTTP_201_CREATED,
)
async def add_interaction(
    alumni_id: IdPath,
    payload: InteractionCreate,
    user: InteractionWriteRateLimit,
    session: SessionDep,
) -> InteractionRead:
    """Log an interaction on an alumni's timeline.

    Open to every authenticated role, including view_only ("Professor"): adding
    an interaction is the one timeline write a professor may perform (#129). The
    row is stamped with the actor's user id so ownership can later gate edit /
    delete for view_only users."""
    return await profile_service.add_interaction(
        session, alumni_id, payload, actor_user_id=user.user_id
    )


@router.patch(
    "/{alumni_id}/interactions/{interaction_id}",
    response_model=InteractionRead,
)
async def update_interaction(
    alumni_id: IdPath,
    interaction_id: IdPath,
    payload: InteractionUpdate,
    user: InteractionWriteRateLimit,
    session: SessionDep,
) -> InteractionRead:
    """Edit an interaction on an alumni's timeline. 404 if the row is missing or
    belongs to another alumnus.

    Edit-tier roles (engineer / super_admin / full_access / student) may edit ANY
    interaction. A view_only ("Professor") user may edit only the interactions
    they logged themselves; editing another user's interaction is 403 (#129)."""
    return await profile_service.update_interaction(
        session,
        alumni_id,
        interaction_id,
        payload,
        actor_user_id=user.user_id,
        can_edit_others=user.can_edit_alumni,
    )


@router.delete(
    "/{alumni_id}/interactions/{interaction_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def delete_interaction(
    alumni_id: IdPath,
    interaction_id: IdPath,
    user: InteractionWriteRateLimit,
    session: SessionDep,
) -> None:
    """Delete an interaction from an alumni's timeline. 404 if the row is missing
    or belongs to another alumnus.

    Edit-tier roles (engineer / super_admin / full_access / student) may delete
    ANY interaction. A view_only ("Professor") user may delete only the
    interactions they logged themselves; deleting another user's interaction is
    403 (#129)."""
    await profile_service.delete_interaction(
        session,
        alumni_id,
        interaction_id,
        actor_user_id=user.user_id,
        can_edit_others=user.can_edit_alumni,
    )


@router.post(
    "/{alumni_id}/tasks",
    response_model=TaskRead,
    status_code=status.HTTP_201_CREATED,
)
async def add_task(
    alumni_id: IdPath,
    payload: TaskCreate,
    user: TaskWriteRateLimit,
    session: SessionDep,
) -> TaskRead:
    """Create a follow-up task for an alumni (full_access)."""
    return await profile_service.add_task(session, alumni_id, payload, actor_user_id=user.user_id)


@router.patch("/{alumni_id}/tasks/{task_id}", response_model=TaskRead)
async def update_task_completion(
    alumni_id: IdPath,
    task_id: IdPath,
    payload: TaskCompleteUpdate,
    user: TaskWriteRateLimit,
    session: SessionDep,
) -> TaskRead:
    """Toggle a follow-up task's completion state (full_access)."""
    return await profile_service.set_task_completed(
        session, alumni_id, task_id, payload.completed, actor_user_id=user.user_id
    )


@router.post(
    "/{alumni_id}/employment",
    response_model=EmploymentHistoryRead,
    status_code=status.HTTP_201_CREATED,
)
async def add_employment(
    alumni_id: IdPath,
    payload: EmploymentHistoryCreate,
    user: EmploymentWriteRateLimit,
    session: SessionDep,
) -> EmploymentHistoryRead:
    """Add a prior role to an alumni's employment history (full_access)."""
    return await profile_service.add_employment(
        session, alumni_id, payload, actor_user_id=user.user_id
    )


@router.patch(
    "/{alumni_id}/employment/{employment_history_id}",
    response_model=EmploymentHistoryRead,
)
async def update_employment(
    alumni_id: IdPath,
    employment_history_id: IdPath,
    payload: EmploymentHistoryUpdate,
    user: EmploymentWriteRateLimit,
    session: SessionDep,
) -> EmploymentHistoryRead:
    """Edit a prior role on an alumni's employment history (full_access). 404 if
    the row is missing or belongs to another alumnus."""
    return await profile_service.update_employment(
        session,
        alumni_id,
        employment_history_id,
        payload,
        actor_user_id=user.user_id,
    )


@router.delete(
    "/{alumni_id}/employment/{employment_history_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def delete_employment(
    alumni_id: IdPath,
    employment_history_id: IdPath,
    user: EmploymentWriteRateLimit,
    session: SessionDep,
) -> None:
    """Delete a prior role from an alumni's employment history (full_access). 404
    if the row is missing or belongs to another alumnus."""
    await profile_service.delete_employment(
        session, alumni_id, employment_history_id, actor_user_id=user.user_id
    )


@router.post(
    "/{alumni_id}/education",
    response_model=EducationRead,
    status_code=status.HTTP_201_CREATED,
)
async def add_education(
    alumni_id: IdPath,
    payload: EducationCreate,
    user: RequireAlumniEdit,
    session: SessionDep,
) -> EducationRead:
    """Add an education entry to an alumni's record (full_access)."""
    return await profile_service.add_education(
        session, alumni_id, payload, actor_user_id=user.user_id
    )


@router.patch(
    "/{alumni_id}/education/{education_id}",
    response_model=EducationRead,
)
async def update_education(
    alumni_id: IdPath,
    education_id: IdPath,
    payload: EducationUpdate,
    user: RequireAlumniEdit,
    session: SessionDep,
) -> EducationRead:
    """Edit an education entry on an alumni's record (full_access). 404 if the
    row is missing or belongs to another alumnus."""
    return await profile_service.update_education(
        session, alumni_id, education_id, payload, actor_user_id=user.user_id
    )


@router.delete(
    "/{alumni_id}/education/{education_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def delete_education(
    alumni_id: IdPath,
    education_id: IdPath,
    user: RequireAlumniEdit,
    session: SessionDep,
) -> None:
    """Delete an education entry from an alumni's record (full_access). 404 if the
    row is missing or belongs to another alumnus."""
    await profile_service.delete_education(
        session, alumni_id, education_id, actor_user_id=user.user_id
    )


@router.post(
    "/{alumni_id}/leadership",
    response_model=LeadershipRead,
    status_code=status.HTTP_201_CREATED,
)
async def add_leadership(
    alumni_id: IdPath,
    payload: LeadershipCreate,
    user: RequireAlumniEdit,
    session: SessionDep,
) -> LeadershipRead:
    """Add a Finance Society leadership entry to an alumni (full_access)."""
    return await profile_service.add_leadership(
        session, alumni_id, payload, actor_user_id=user.user_id
    )


@router.patch(
    "/{alumni_id}/leadership/{finance_society_leadership_id}",
    response_model=LeadershipRead,
)
async def update_leadership(
    alumni_id: IdPath,
    finance_society_leadership_id: IdPath,
    payload: LeadershipUpdate,
    user: RequireAlumniEdit,
    session: SessionDep,
) -> LeadershipRead:
    """Edit a Finance Society leadership entry (full_access). 404 if the row is
    missing or belongs to another alumnus."""
    return await profile_service.update_leadership(
        session,
        alumni_id,
        finance_society_leadership_id,
        payload,
        actor_user_id=user.user_id,
    )


@router.delete(
    "/{alumni_id}/leadership/{finance_society_leadership_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def delete_leadership(
    alumni_id: IdPath,
    finance_society_leadership_id: IdPath,
    user: RequireAlumniEdit,
    session: SessionDep,
) -> None:
    """Delete a Finance Society leadership entry (full_access). 404 if the row is
    missing or belongs to another alumnus."""
    await profile_service.delete_leadership(
        session,
        alumni_id,
        finance_society_leadership_id,
        actor_user_id=user.user_id,
    )


@router.post(
    "/{alumni_id}/tags",
    response_model=list[str],
    status_code=status.HTTP_201_CREATED,
)
async def add_tag(
    alumni_id: IdPath,
    payload: TagCreate,
    user: RequireAlumniEdit,
    session: SessionDep,
) -> list[str]:
    """Attach a canonical engagement tag to an alumni (full_access). Returns the
    resulting tag list. Idempotent."""
    return await profile_service.add_tag(session, alumni_id, payload, actor_user_id=user.user_id)


@router.delete("/{alumni_id}/tags/{tag:path}", response_model=list[str])
async def remove_tag(
    alumni_id: IdPath,
    tag: str,
    user: RequireAlumniEdit,
    session: SessionDep,
) -> list[str]:
    """Detach a tag from an alumni (full_access). Returns the resulting tag list.
    404 if the alumni doesn't have that tag."""
    return await profile_service.remove_tag(session, alumni_id, tag, actor_user_id=user.user_id)


@router.post(
    "/{alumni_id}/status-labels",
    response_model=list[str],
    status_code=status.HTTP_201_CREATED,
)
async def add_status_label(
    alumni_id: IdPath,
    payload: StatusLabelCreate,
    user: RequireAlumniEdit,
    session: SessionDep,
) -> list[str]:
    """Attach a canonical status label to an alumni (full_access). Returns the
    resulting label list. Idempotent."""
    return await profile_service.add_status_label(
        session, alumni_id, payload, actor_user_id=user.user_id
    )


@router.delete("/{alumni_id}/status-labels/{label:path}", response_model=list[str])
async def remove_status_label(
    alumni_id: IdPath,
    label: str,
    user: RequireAlumniEdit,
    session: SessionDep,
) -> list[str]:
    """Detach a status label from an alumni (full_access). Returns the resulting
    label list. 404 if the alumni doesn't have that label."""
    return await profile_service.remove_status_label(
        session, alumni_id, label, actor_user_id=user.user_id
    )


@router.post(
    "/{alumni_id}/events",
    response_model=EventAttendedRead,
    status_code=status.HTTP_201_CREATED,
)
async def add_event_attendance(
    alumni_id: IdPath,
    payload: EventAttendanceCreate,
    user: RequireAlumniEdit,
    session: SessionDep,
) -> EventAttendedRead:
    """Mark an alumni as an attendee of an existing event (full_access). 404 if
    the event/alumni is unknown; 409 if attendance already exists."""
    return await profile_service.add_event_attendance(
        session, alumni_id, payload, actor_user_id=user.user_id
    )


@router.post("/preview", response_model=AlumniHygienePreview)
async def preview_create_alumni(
    payload: AlumniCreateFull, user: RequireFullAccess, session: SessionDep
) -> dict:
    """Dry-run data-hygiene preview for a NEW alumni (full_access, no writes).

    Returns ``{cleaned, changes, warnings, blockers}`` — the cleaned (normalized)
    payload, the per-field changes cleaning would make, soft warnings (completeness
    + fuzzy possible-duplicates), and exact-duplicate blockers (a non-empty list
    means the real POST would 409). The preview reads stored data, so it is
    audit-logged (``preview``)."""
    result = await hygiene.build_preview(session, payload)
    await service.log_preview(session, actor_user_id=user.user_id)
    return result


@router.post("/{alumni_id}/preview", response_model=AlumniHygienePreview)
async def preview_update_alumni(
    alumni_id: IdPath,
    payload: AlumniUpdateFull,
    user: RequireAlumniEdit,
    session: SessionDep,
) -> dict:
    """Dry-run data-hygiene preview for an EDIT (full_access, no writes).

    Loads the current record (404 if missing/archived) and computes the preview
    against the EFFECTIVE record (the cleaned partial overlaid on the stored
    values) so duplicate + completeness checks reflect the resulting state. The
    preview reads stored data, so it is audit-logged (``preview``)."""
    existing = await service.get_alumni(session, alumni_id, include_archived=True)
    if existing.archived:
        raise NotFoundError(f"Alumni {alumni_id} not found.")
    result = await hygiene.build_preview(
        session, payload, existing=existing, exclude_alumni_id=alumni_id
    )
    await service.log_preview(session, actor_user_id=user.user_id, alumni_id=alumni_id)
    return result


@router.post("", response_model=AlumniRead, status_code=status.HTTP_201_CREATED)
async def create_alumni(
    payload: AlumniCreateFull, user: RequireFullAccess, session: SessionDep
) -> AlumniRead:
    return await service.create_alumni(session, payload, actor_user_id=user.user_id)


@router.patch("/{alumni_id}", response_model=AlumniRead)
async def update_alumni(
    alumni_id: IdPath,
    payload: AlumniUpdateFull,
    user: RequireAlumniEdit,
    session: SessionDep,
) -> AlumniRead:
    return await service.update_alumni(session, alumni_id, payload, actor_user_id=user.user_id)


@router.delete("/{alumni_id}", response_model=AlumniRead)
async def archive_alumni(
    alumni_id: IdPath, user: RequireFullAccess, session: SessionDep
) -> AlumniRead:
    """Soft-delete (archive) an alumni record."""
    return await service.archive_alumni(session, alumni_id, actor_user_id=user.user_id)


@router.post("/{alumni_id}/restore", response_model=AlumniRead)
async def restore_alumni(
    alumni_id: IdPath, user: RequireFullAccess, session: SessionDep
) -> AlumniRead:
    """Restore (unarchive) a previously archived alumni record."""
    return await service.restore_alumni(session, alumni_id, actor_user_id=user.user_id)
