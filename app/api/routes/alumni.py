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
from typing import Annotated

from fastapi import APIRouter, Depends, File, Query, UploadFile, status
from fastapi.responses import JSONResponse, Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies.auth import (
    RequireAlumniEdit,
    RequireFullAccess,
    RequireViewAccess,
)
from app.core.database import get_session
from app.core.errors import NotFoundError
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
from app.services import alumni_export, hygiene, import_csv
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
    donor: Annotated[bool, Query(description="Only PIFF donors.")] = False,
    mentor_willing: Annotated[bool, Query(description="Only alumni willing to mentor.")] = False,
    guest_speaker_willing: Annotated[
        bool, Query(description="Only alumni willing to guest speak.")
    ] = False,
    missing_email: Annotated[
        bool,
        Query(description="Only alumni with no contact-info email on file."),
    ] = False,
    missing_employer: Annotated[
        bool,
        Query(description="Only alumni with no current employer on file."),
    ] = False,
    duplicate: Annotated[
        bool,
        Query(description="Only alumni flagged as duplicate candidates."),
    ] = False,
    include_archived: bool = False,
    sort: Annotated[
        str,
        Query(description="Sort order: name | grad_desc | grad_asc."),
    ] = "name",
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> AlumniPage:
    # Archived rows are full_access-and-up only: a view_only / student caller
    # passing ``include_archived=true`` must NOT receive soft-deleted records.
    has_full_access = user.is_full_access or user.is_super_admin or user.is_engineer
    effective_include_archived = include_archived and has_full_access
    items, total = await service.list_alumni(
        session,
        limit=limit,
        offset=offset,
        q=q,
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
        contacted_after=contacted_after,
        contacted_before=contacted_before,
        never_contacted=never_contacted,
        attended_event=attended_event,
        donor=donor,
        mentor_willing=mentor_willing,
        guest_speaker_willing=guest_speaker_willing,
        missing_email=missing_email,
        missing_employer=missing_employer,
        duplicate=duplicate,
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
            "contacted_after": contacted_after.isoformat() if contacted_after else None,
            "contacted_before": (contacted_before.isoformat() if contacted_before else None),
            "never_contacted": never_contacted or None,
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


async def _read_capped(file: UploadFile) -> bytes | None:
    """Read an upload, capped at ``MAX_UPLOAD_BYTES``.

    Reads one byte past the cap so we can tell "exactly at the limit" from "over".
    Returns the bytes, or ``None`` if the file exceeds the cap (the caller turns
    that into a 413 response). Bounds memory before any parsing happens (DoS).
    """
    data = await file.read(import_csv.MAX_UPLOAD_BYTES + 1)
    if len(data) > import_csv.MAX_UPLOAD_BYTES:
        return None
    return data


def _too_large_response() -> JSONResponse:
    mib = import_csv.MAX_UPLOAD_BYTES // (1024 * 1024)
    return JSONResponse(
        status_code=413,  # Content Too Large
        content={
            "error": {
                "code": "payload_too_large",
                "message": (f"File exceeds the {mib} MB upload limit. Split into smaller batches."),
            }
        },
    )


@router.get("/import/template")
async def alumni_import_template(_: RequireFullAccess) -> Response:
    """Download the bulk-import CSV template: the exact Alumni columns plus one
    example row (full_access). Same column source as the xlsx intake template."""
    return Response(
        content=import_csv.build_template_csv(),
        media_type="text/csv",
        headers={"Content-Disposition": ('attachment; filename="alumni_import_template.csv"')},
    )


@router.post("/import/preview", response_model=None)
async def preview_import_alumni(
    _: RequireFullAccess,
    session: SessionDep,
    file: Annotated[UploadFile, File()],
) -> dict | JSONResponse:
    """Dry-run a bulk CSV import (full_access, NO writes).

    Parses + maps the uploaded CSV against the Alumni template columns, then
    evaluates every row (clean + duplicate-detect against the DB and earlier
    rows in the file + completeness warnings). Returns the full preview report;
    a bad header set surfaces as ``columns_ok: false`` with ``header_errors``."""
    file_bytes = await _read_capped(file)
    if file_bytes is None:
        return _too_large_response()
    rows, header_errors = import_csv.parse_and_map(file_bytes, max_rows=import_csv.MAX_IMPORT_ROWS)
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


@router.post("/import", response_model=None)
async def import_alumni(
    user: RequireFullAccess,
    session: SessionDep,
    file: Annotated[UploadFile, File()],
) -> dict | JSONResponse:
    """Commit a bulk CSV import (full_access). Re-evaluates and inserts every
    importable row in one transaction (audit logging fires per row); rejected
    rows are skipped and reported. A bad header set imports nothing."""
    file_bytes = await _read_capped(file)
    if file_bytes is None:
        return _too_large_response()
    rows, header_errors = import_csv.parse_and_map(file_bytes, max_rows=import_csv.MAX_IMPORT_ROWS)
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
async def get_alumni(alumni_id: int, user: RequireViewAccess, session: SessionDep) -> AlumniRead:
    """Single lightweight alumni core record.

    Archived records 404 (they were removed from the directory). view_only
    ("Professor") callers receive a FERPA-minimized record — sensitive PII,
    notes, and import provenance are nulled. This lightweight read is not
    audit-logged (the full profile aggregate is)."""
    alumnus = await service.get_alumni(session, alumni_id)
    return minimize_alumni_read(AlumniRead.model_validate(alumnus), can_edit=user.can_edit_alumni)


@router.get("/{alumni_id}/profile", response_model=ProfileRead)
async def get_alumni_profile(
    alumni_id: int, user: RequireViewAccess, session: SessionDep
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


@router.get("/{alumni_id}/export")
async def export_alumni_profile(
    alumni_id: int, user: RequireFullAccess, session: SessionDep
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
    alumni_id: int,
    payload: InteractionCreate,
    user: RequireAlumniEdit,
    session: SessionDep,
) -> InteractionRead:
    """Log an interaction on an alumni's timeline (full_access)."""
    return await profile_service.add_interaction(
        session, alumni_id, payload, actor_user_id=user.user_id
    )


@router.patch(
    "/{alumni_id}/interactions/{interaction_id}",
    response_model=InteractionRead,
)
async def update_interaction(
    alumni_id: int,
    interaction_id: int,
    payload: InteractionUpdate,
    user: RequireAlumniEdit,
    session: SessionDep,
) -> InteractionRead:
    """Edit an interaction on an alumni's timeline (full_access). 404 if the row
    is missing or belongs to another alumnus."""
    return await profile_service.update_interaction(
        session,
        alumni_id,
        interaction_id,
        payload,
        actor_user_id=user.user_id,
    )


@router.delete(
    "/{alumni_id}/interactions/{interaction_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def delete_interaction(
    alumni_id: int,
    interaction_id: int,
    user: RequireAlumniEdit,
    session: SessionDep,
) -> None:
    """Delete an interaction from an alumni's timeline (full_access). 404 if the
    row is missing or belongs to another alumnus."""
    await profile_service.delete_interaction(
        session, alumni_id, interaction_id, actor_user_id=user.user_id
    )


@router.post(
    "/{alumni_id}/tasks",
    response_model=TaskRead,
    status_code=status.HTTP_201_CREATED,
)
async def add_task(
    alumni_id: int,
    payload: TaskCreate,
    user: RequireAlumniEdit,
    session: SessionDep,
) -> TaskRead:
    """Create a follow-up task for an alumni (full_access)."""
    return await profile_service.add_task(session, alumni_id, payload, actor_user_id=user.user_id)


@router.patch("/{alumni_id}/tasks/{task_id}", response_model=TaskRead)
async def update_task_completion(
    alumni_id: int,
    task_id: int,
    payload: TaskCompleteUpdate,
    user: RequireAlumniEdit,
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
    alumni_id: int,
    payload: EmploymentHistoryCreate,
    user: RequireAlumniEdit,
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
    alumni_id: int,
    employment_history_id: int,
    payload: EmploymentHistoryUpdate,
    user: RequireAlumniEdit,
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
    alumni_id: int,
    employment_history_id: int,
    user: RequireAlumniEdit,
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
    alumni_id: int,
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
    alumni_id: int,
    education_id: int,
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
    alumni_id: int,
    education_id: int,
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
    alumni_id: int,
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
    alumni_id: int,
    finance_society_leadership_id: int,
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
    alumni_id: int,
    finance_society_leadership_id: int,
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
    alumni_id: int,
    payload: TagCreate,
    user: RequireAlumniEdit,
    session: SessionDep,
) -> list[str]:
    """Attach a canonical engagement tag to an alumni (full_access). Returns the
    resulting tag list. Idempotent."""
    return await profile_service.add_tag(session, alumni_id, payload, actor_user_id=user.user_id)


@router.delete("/{alumni_id}/tags/{tag:path}", response_model=list[str])
async def remove_tag(
    alumni_id: int,
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
    alumni_id: int,
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
    alumni_id: int,
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
    alumni_id: int,
    payload: EventAttendanceCreate,
    user: RequireAlumniEdit,
    session: SessionDep,
) -> EventAttendedRead:
    """Mark an alumni as an attendee of an existing event (full_access). 404 if
    the event/alumni is unknown; 409 if attendance already exists."""
    return await profile_service.add_event_attendance(
        session, alumni_id, payload, actor_user_id=user.user_id
    )


@router.post("/preview")
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


@router.post("/{alumni_id}/preview")
async def preview_update_alumni(
    alumni_id: int,
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
    alumni_id: int,
    payload: AlumniUpdateFull,
    user: RequireAlumniEdit,
    session: SessionDep,
) -> AlumniRead:
    return await service.update_alumni(session, alumni_id, payload, actor_user_id=user.user_id)


@router.delete("/{alumni_id}", response_model=AlumniRead)
async def archive_alumni(
    alumni_id: int, user: RequireFullAccess, session: SessionDep
) -> AlumniRead:
    """Soft-delete (archive) an alumni record."""
    return await service.archive_alumni(session, alumni_id, actor_user_id=user.user_id)


@router.post("/{alumni_id}/restore", response_model=AlumniRead)
async def restore_alumni(
    alumni_id: int, user: RequireFullAccess, session: SessionDep
) -> AlumniRead:
    """Restore (unarchive) a previously archived alumni record."""
    return await service.restore_alumni(session, alumni_id, actor_user_id=user.user_id)
