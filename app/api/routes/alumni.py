"""Alumni CRUD routes.

Reads require view access (any role). Editing an EXISTING alumnus and their
nested records (interactions, employment, education, leadership, tags, status
labels, tasks, event attendance) requires edit access (``student`` and up, via
``RequireAlumniEdit``). Creating a new alumnus, archiving/restoring, and CSV
import require ``full_access`` and up (``RequireFullAccess``) — ``student`` is
deliberately excluded from those. ``DELETE`` on an alumnus is a soft-delete
(archive), never a hard delete — audit history depends on retained records.
"""

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
    AlumniPage,
    AlumniRead,
    AlumniUpdateFull,
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
from app.services import hygiene, import_csv
from app.services import profile as profile_service

router = APIRouter(prefix="/alumni", tags=["alumni"])

SessionDep = Annotated[AsyncSession, Depends(get_session)]


@router.get("", response_model=AlumniPage)
async def list_alumni(
    _: RequireViewAccess,
    session: SessionDep,
    q: Annotated[
        str | None,
        Query(description="Search names and external ids (case-insensitive)."),
    ] = None,
    graduation_year: int | None = None,
    grad_year_min: int | None = None,
    grad_year_max: int | None = None,
    deceased: Annotated[
        bool | None, Query(description="Filter by deceased flag.")
    ] = None,
    employer: Annotated[
        str | None,
        Query(description="Current employer (case-insensitive exact match)."),
    ] = None,
    industry: Annotated[
        str | None,
        Query(
            description=(
                "Current industry / work area, primary or secondary "
                "(case-insensitive exact match)."
            )
        ),
    ] = None,
    city: Annotated[
        str | None,
        Query(description="Current city (case-insensitive exact match)."),
    ] = None,
    tag: Annotated[
        str | None,
        Query(
            description=(
                "Engagement tag label, e.g. 'Speaker' or 'Highly Engaged' "
                "(case-insensitive exact match). Accepts any tag value."
            )
        ),
    ] = None,
    attended_event: Annotated[
        bool, Query(description="Only alumni who attended at least one event.")
    ] = False,
    donor: Annotated[
        bool, Query(description="Only PIFF donors.")
    ] = False,
    mentor_willing: Annotated[
        bool, Query(description="Only alumni willing to mentor.")
    ] = False,
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
        industry=industry,
        city=city,
        tag=tag,
        attended_event=attended_event,
        donor=donor,
        mentor_willing=mentor_willing,
        guest_speaker_willing=guest_speaker_willing,
        missing_email=missing_email,
        missing_employer=missing_employer,
        duplicate=duplicate,
        include_archived=include_archived,
        sort=sort,
    )
    return AlumniPage(items=items, total=total, limit=limit, offset=offset)


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
                "message": (
                    f"File exceeds the {mib} MB upload limit. Split into "
                    "smaller batches."
                ),
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
        headers={
            "Content-Disposition": (
                'attachment; filename="alumni_import_template.csv"'
            )
        },
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
    rows, header_errors = import_csv.parse_and_map(
        file_bytes, max_rows=import_csv.MAX_IMPORT_ROWS
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
    rows, header_errors = import_csv.parse_and_map(
        file_bytes, max_rows=import_csv.MAX_IMPORT_ROWS
    )
    if header_errors:
        return {
            "imported": 0,
            "skipped": 0,
            "created_ids": [],
            "rejects": [
                {"row": 0, "name": "(header)", "reason": msg}
                for msg in header_errors
            ],
        }
    return await import_csv.commit_import(
        session, rows, actor_user_id=user.user_id
    )


@router.get("/{alumni_id}", response_model=AlumniRead)
async def get_alumni(
    alumni_id: int, _: RequireViewAccess, session: SessionDep
) -> AlumniRead:
    return await service.get_alumni(session, alumni_id)


@router.get("/{alumni_id}/profile", response_model=ProfileRead)
async def get_alumni_profile(
    alumni_id: int, user: RequireViewAccess, session: SessionDep
) -> ProfileRead:
    """Full profile aggregate (core + contact, career, employment, leadership,
    engagement, surveys, interactions, tasks, attachments, audit) for the tabs.

    Follow-up tasks are edit-only: view_only ("Professor") users get an empty
    ``tasks`` list (enforced here, not just hidden in the UI). Anyone with edit
    access — engineer / super_admin / full_access / student — sees all."""
    include_tasks = user.can_edit_alumni
    return await profile_service.get_profile(
        session, alumni_id, include_tasks=include_tasks
    )


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
    return await profile_service.add_task(
        session, alumni_id, payload, actor_user_id=user.user_id
    )


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
    return await profile_service.add_tag(
        session, alumni_id, payload, actor_user_id=user.user_id
    )


@router.delete("/{alumni_id}/tags/{tag}", response_model=list[str])
async def remove_tag(
    alumni_id: int,
    tag: str,
    user: RequireAlumniEdit,
    session: SessionDep,
) -> list[str]:
    """Detach a tag from an alumni (full_access). Returns the resulting tag list.
    404 if the alumni doesn't have that tag."""
    return await profile_service.remove_tag(
        session, alumni_id, tag, actor_user_id=user.user_id
    )


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


@router.delete("/{alumni_id}/status-labels/{label}", response_model=list[str])
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
    payload: AlumniCreateFull, _: RequireFullAccess, session: SessionDep
) -> dict:
    """Dry-run data-hygiene preview for a NEW alumni (full_access, no writes).

    Returns ``{cleaned, changes, warnings, blockers}`` — the cleaned (normalized)
    payload, the per-field changes cleaning would make, soft warnings (completeness
    + fuzzy possible-duplicates), and exact-duplicate blockers (a non-empty list
    means the real POST would 409)."""
    return await hygiene.build_preview(session, payload)


@router.post("/{alumni_id}/preview")
async def preview_update_alumni(
    alumni_id: int,
    payload: AlumniUpdateFull,
    _: RequireAlumniEdit,
    session: SessionDep,
) -> dict:
    """Dry-run data-hygiene preview for an EDIT (full_access, no writes).

    Loads the current record (404 if missing/archived) and computes the preview
    against the EFFECTIVE record (the cleaned partial overlaid on the stored
    values) so duplicate + completeness checks reflect the resulting state."""
    existing = await service.get_alumni(session, alumni_id)
    if existing.archived:
        raise NotFoundError(f"Alumni {alumni_id} not found.")
    return await hygiene.build_preview(
        session, payload, existing=existing, exclude_alumni_id=alumni_id
    )


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
    return await service.update_alumni(
        session, alumni_id, payload, actor_user_id=user.user_id
    )


@router.delete("/{alumni_id}", response_model=AlumniRead)
async def archive_alumni(
    alumni_id: int, user: RequireFullAccess, session: SessionDep
) -> AlumniRead:
    """Soft-delete (archive) an alumni record."""
    return await service.archive_alumni(
        session, alumni_id, actor_user_id=user.user_id
    )


@router.post("/{alumni_id}/restore", response_model=AlumniRead)
async def restore_alumni(
    alumni_id: int, user: RequireFullAccess, session: SessionDep
) -> AlumniRead:
    """Restore (unarchive) a previously archived alumni record."""
    return await service.restore_alumni(
        session, alumni_id, actor_user_id=user.user_id
    )
