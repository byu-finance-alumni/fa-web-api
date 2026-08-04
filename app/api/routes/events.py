"""Event listing routes."""

import csv
import datetime
import io
from typing import Annotated

from fastapi import APIRouter, Depends, File, Form, Query, UploadFile, status
from fastapi.responses import JSONResponse, Response
from sqlalchemy import case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies.auth import RequireFullAccess, RequireViewAccess
from app.api.params import IdPath
from app.core.database import get_session
from app.core.errors import ConflictError, NotFoundError
from app.models.alumni import Alumni
from app.models.audit import AuditLog
from app.models.contact import AlumniContactInfo
from app.models.event import Event, EventAttendance
from app.schemas.event import AttendeeCreate, AttendeeRead, EventCreate, EventUpdate
from app.schemas.imports import (
    EventAttendeeImportPreview,
    EventAttendeeImportResult,
    EventImportPreview,
    EventImportResult,
)
from app.services import import_events

# Reuse the alumni export's formula-injection neutralizer (canonical source:
# alumni_export._FORMULA_LEAD) so attendee cells starting with = + - @ \t \r are
# tab-prefixed to plain text instead of executing as spreadsheet formulas (#169).
from app.services.alumni_export import _fmt
from app.utils.sql import escape_like

router = APIRouter(prefix="/events", tags=["events"])

SessionDep = Annotated[AsyncSession, Depends(get_session)]


def _serialize(e: Event, attendance: int) -> dict:
    return {
        "event_id": e.event_id,
        "event_name": e.event_name,
        "event_type": e.event_type,
        "event_date": e.event_date.isoformat() if e.event_date else None,
        "event_location": e.event_location,
        "event_notes": e.event_notes,
        "attendance_count": attendance,
    }


@router.get("")
async def list_events(
    _: RequireViewAccess,
    session: SessionDep,
    q: Annotated[
        str | None,
        Query(description="Substring match on event name or location (case-insensitive)."),
    ] = None,
    event_type: Annotated[
        str | None,
        Query(description="Event type (case-insensitive exact match)."),
    ] = None,
    date_from: Annotated[
        datetime.date | None,
        Query(description="Only events on or after this date (inclusive)."),
    ] = None,
    date_to: Annotated[
        datetime.date | None,
        Query(description="Only events on or before this date (inclusive)."),
    ] = None,
    sort: Annotated[
        str,
        Query(description="Sort order: date | upcoming | type."),
    ] = "date",
) -> list[dict]:
    count_sq = (
        select(
            EventAttendance.event_id,
            func.count().label("att"),
        )
        .group_by(EventAttendance.event_id)
        .subquery()
    )
    stmt = (
        select(Event, func.coalesce(count_sq.c.att, 0))
        .outerjoin(count_sq, Event.event_id == count_sq.c.event_id)
    )
    if q:
        term = q.strip()
        if term:
            pattern = f"%{escape_like(term)}%"
            stmt = stmt.where(
                Event.event_name.ilike(pattern, escape="\\")
                | Event.event_location.ilike(pattern, escape="\\")
            )
    if event_type:
        stmt = stmt.where(func.lower(Event.event_type) == event_type.strip().lower())
    if date_from is not None:
        stmt = stmt.where(Event.event_date >= date_from)
    if date_to is not None:
        stmt = stmt.where(Event.event_date <= date_to)
    if sort == "type":
        stmt = stmt.order_by(
            Event.event_type.asc().nullslast(),
            Event.event_date.desc().nullslast(),
        )
    elif sort == "upcoming":
        # Upcoming (today or later) first, soonest at the top; past events follow,
        # most recent first.
        today = datetime.date.today()
        is_past = case((Event.event_date >= today, 0), else_=1)
        within = case(
            (Event.event_date >= today, Event.event_date),
            else_=None,
        )
        stmt = stmt.order_by(
            is_past.asc(),
            within.asc().nullslast(),
            Event.event_date.desc().nullslast(),
        )
    else:  # "date" — newest first (default)
        stmt = stmt.order_by(Event.event_date.desc().nullslast())
    rows = (await session.execute(stmt)).all()
    return [_serialize(e, int(att)) for e, att in rows]


@router.get("/options")
async def event_options(_: RequireViewAccess, session: SessionDep) -> dict:
    """Distinct, sorted, non-null event types for the filter menu (view access)."""
    rows = (
        await session.execute(
            select(Event.event_type)
            .where(Event.event_type.isnot(None))
            .distinct()
            .order_by(Event.event_type)
        )
    ).all()
    return {"types": [r[0] for r in rows if r[0]]}


# --- Bulk CSV import (full_access) -------------------------------------------
#
# Declared BEFORE the ``/{event_id}`` routes so the literal ``/import/...`` paths
# win over the ``/{event_id}`` patterns (route matching is declaration-ordered).


async def _read_capped(file: UploadFile) -> bytes | None:
    """Read an upload capped at ``MAX_UPLOAD_BYTES`` (one byte past to detect
    overage). Returns ``None`` if over the cap so the caller can 413. Bounds
    memory before any parsing (DoS)."""
    data = await file.read(import_events.MAX_UPLOAD_BYTES + 1)
    if len(data) > import_events.MAX_UPLOAD_BYTES:
        return None
    return data


def _too_large_response() -> JSONResponse:
    mib = import_events.MAX_UPLOAD_BYTES // (1024 * 1024)
    return JSONResponse(
        status_code=413,
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
async def events_import_template(_: RequireFullAccess) -> Response:
    """Download the events bulk-import CSV template (full_access): the exact
    columns plus a few example rows (two attendees share one event to show how
    rows group into a single event)."""
    return Response(
        content=import_events.build_template_csv(),
        media_type="text/csv",
        headers={
            "Content-Disposition": 'attachment; filename="events_import_template.csv"'
        },
    )


# The event's identity is entered in the wizard and posted alongside the file as
# multipart form fields (the CSV is only the attendee roster — #149).
EventNameForm = Annotated[str, Form()]
EventDateForm = Annotated[str | None, Form()]
EventTypeForm = Annotated[str | None, Form()]
EventLocationForm = Annotated[str | None, Form()]
EventNotesForm = Annotated[str | None, Form()]


def _headers_bad_preview(header_errors: list[str], meta: dict) -> dict:
    return {
        "columns_ok": False,
        "header_errors": header_errors,
        "event": {
            "event_name": meta["event_name"],
            "event_date": meta["event_date"],
            "event_type": meta["event_type"],
            "event_location": meta["event_location"],
            "event_notes": meta["event_notes"],
        },
        "importable": False,
        "event_errors": [],
        "summary": {
            "total_rows": 0,
            "attendees_matched": 0,
            "attendees_unmatched": 0,
        },
        "attendees": [],
        "warnings": [],
    }


@router.post("/import/preview", response_model=EventImportPreview)
async def preview_import_events(
    _: RequireFullAccess,
    session: SessionDep,
    file: Annotated[UploadFile, File()],
    event_name: EventNameForm,
    event_date: EventDateForm = None,
    event_type: EventTypeForm = None,
    event_location: EventLocationForm = None,
    event_notes: EventNotesForm = None,
) -> dict | JSONResponse:
    """Dry-run a single-event attendee CSV import (full_access, NO writes).

    The event's identity (title/date/type/…) comes from the wizard as form
    fields; the CSV is the attendee roster. Resolves attendees by Net ID and
    flags unmatched/duplicate attendees, a bad date, and a pre-existing event. A
    bad header set surfaces as ``columns_ok: false`` with ``header_errors``."""
    meta = import_events.normalize_event_meta(
        event_name, event_date, event_type, event_location, event_notes
    )
    file_bytes = await _read_capped(file)
    if file_bytes is None:
        return _too_large_response()
    rows, header_errors = import_events.parse_and_map(file_bytes)
    if header_errors:
        return _headers_bad_preview(header_errors, meta)
    return await import_events.evaluate(session, rows, meta)


@router.post("/import", response_model=EventImportResult)
async def import_events_commit(
    user: RequireFullAccess,
    session: SessionDep,
    file: Annotated[UploadFile, File()],
    event_name: EventNameForm,
    event_date: EventDateForm = None,
    event_type: EventTypeForm = None,
    event_location: EventLocationForm = None,
    event_notes: EventNotesForm = None,
) -> dict | JSONResponse:
    """Commit a single-event attendee CSV import (full_access). Re-evaluates and,
    if the event identity is valid and new, inserts the event + its matched
    attendees in one transaction (audit logging fires for the event and each
    attendee); unmatched attendees are skipped and reported. A bad header set
    imports nothing."""
    meta = import_events.normalize_event_meta(
        event_name, event_date, event_type, event_location, event_notes
    )
    file_bytes = await _read_capped(file)
    if file_bytes is None:
        return _too_large_response()
    rows, header_errors = import_events.parse_and_map(file_bytes)
    if header_errors:
        return {
            "imported": False,
            "event_id": None,
            "imported_attendees": 0,
            "unmatched": [],
            "event_error": header_errors[0],
        }
    return await import_events.commit_import(
        session, rows, meta, actor_user_id=user.user_id
    )


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_event(
    payload: EventCreate, user: RequireFullAccess, session: SessionDep
) -> dict:
    """Create an event (full_access). Stamps the acting user and audits the
    write (entity_type "event", action "create")."""
    event = Event(
        event_name=payload.event_name,
        event_type=payload.event_type,
        event_date=payload.event_date,
        event_location=payload.event_location,
        event_notes=payload.event_notes,
        logged_by_user_id=user.user_id,
    )
    session.add(event)
    await session.flush()
    session.add(
        AuditLog(
            user_id=user.user_id,
            action_type="create",
            entity_type="event",
            entity_id=event.event_id,
        )
    )
    await session.commit()
    await session.refresh(event)
    return _serialize(event, 0)


@router.get("/{event_id}")
async def get_event(
    event_id: IdPath, _: RequireViewAccess, session: SessionDep
) -> dict:
    event = await session.get(Event, event_id)
    if event is None:
        raise NotFoundError(f"Event {event_id} not found.")
    att = await session.scalar(
        select(func.count())
        .select_from(EventAttendance)
        .where(EventAttendance.event_id == event_id)
    )
    return _serialize(event, int(att or 0))


def _audit_value(value) -> str | None:
    """Normalise a field value for the audit log's text columns."""
    if value is None:
        return None
    if isinstance(value, datetime.date):
        return value.isoformat()
    return str(value)


@router.patch("/{event_id}")
async def update_event(
    event_id: IdPath,
    payload: EventUpdate,
    user: RequireFullAccess,
    session: SessionDep,
) -> dict:
    """Partially update an event (full_access). Only the fields present in the
    request body are applied; each changed field is audited with its old/new
    value (entity_type "event", action "update"). 404 if the event is unknown."""
    event = await session.get(Event, event_id)
    if event is None:
        raise NotFoundError(f"Event {event_id} not found.")

    changes = payload.model_dump(exclude_unset=True)
    applied: dict[str, tuple[object, object]] = {}
    for field, value in changes.items():
        old = getattr(event, field)
        if old != value:
            applied[field] = (old, value)
            setattr(event, field, value)

    if applied:
        for field, (old, new) in applied.items():
            session.add(
                AuditLog(
                    user_id=user.user_id,
                    action_type="update",
                    entity_type="event",
                    entity_id=event_id,
                    field_name=field,
                    old_value=_audit_value(old),
                    new_value=_audit_value(new),
                )
            )
        await session.commit()
        await session.refresh(event)

    att = await session.scalar(
        select(func.count())
        .select_from(EventAttendance)
        .where(EventAttendance.event_id == event_id)
    )
    return _serialize(event, int(att or 0))


@router.delete("/{event_id}")
async def delete_event(
    event_id: IdPath, user: RequireFullAccess, session: SessionDep
) -> dict:
    """Delete an event (full_access). Cascades to its attendance rows (and any
    attached notes) via the FK ``ON DELETE CASCADE``. 404 if the event is
    unknown. Audits the write (entity_type "event", action "delete")."""
    event = await session.get(Event, event_id)
    if event is None:
        raise NotFoundError(f"Event {event_id} not found.")
    name = event.event_name
    await session.delete(event)
    session.add(
        AuditLog(
            user_id=user.user_id,
            action_type="delete",
            entity_type="event",
            entity_id=event_id,
            old_value=name,
        )
    )
    await session.commit()
    return {"event_id": event_id, "deleted": True}


def _attendee_name(a: Alumni) -> str:
    name = " ".join(
        p for p in (a.preferred_first_name or a.first_name, a.last_name) if p
    ).strip()
    return name or f"Alumni #{a.alumni_id}"


@router.get("/{event_id}/attendees", response_model=list[AttendeeRead])
async def list_event_attendees(
    event_id: IdPath, _: RequireViewAccess, session: SessionDep
) -> list[AttendeeRead]:
    """Alumni who attended an event (view-access read). 404 if the event is
    unknown so callers can distinguish "no attendees" from "no such event".

    ``notes`` echoes the per-attendance ``attendance_notes`` (#181) so the notes
    the bulk importer writes are actually readable on the roster."""
    event = await session.get(Event, event_id)
    if event is None:
        raise NotFoundError(f"Event {event_id} not found.")
    rows = (
        await session.execute(
            select(
                Alumni,
                EventAttendance.attendance_status,
                EventAttendance.attendance_notes,
            )
            .join(EventAttendance, EventAttendance.alumni_id == Alumni.alumni_id)
            .where(EventAttendance.event_id == event_id)
            .order_by(Alumni.last_name, Alumni.first_name)
        )
    ).all()
    return [
        AttendeeRead(
            alumni_id=a.alumni_id,
            name=_attendee_name(a),
            graduation_year=a.graduation_year,
            attendance_status=status,
            notes=notes,
        )
        for a, status, notes in rows
    ]


@router.get("/{event_id}/attendees/export")
async def export_event_attendees(
    event_id: IdPath, user: RequireFullAccess, session: SessionDep
) -> Response:
    """Download an event's attendee list as CSV — columns **Name, Email, Net ID**
    (#219). Gated at ``full_access`` (a rung above the view-only attendee list)
    because bulk contact details — alumni PII — leave the system here, and audited
    as a disclosure (action ``export_event_attendees``, row count only, never the
    data itself). 404 if the event is unknown.

    Email is the alumnus's personal email, falling back to the work email. Rows
    are ordered by name, matching the on-screen roster."""
    event = await session.get(Event, event_id)
    if event is None:
        raise NotFoundError(f"Event {event_id} not found.")
    rows = (
        await session.execute(
            select(
                Alumni,
                AlumniContactInfo.personal_email,
                AlumniContactInfo.work_email,
            )
            .join(EventAttendance, EventAttendance.alumni_id == Alumni.alumni_id)
            .outerjoin(
                AlumniContactInfo, AlumniContactInfo.alumni_id == Alumni.alumni_id
            )
            .where(EventAttendance.event_id == event_id)
            .order_by(Alumni.last_name, Alumni.first_name)
        )
    ).all()

    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(["Name", "Email", "Net ID"])
    for alumni, personal_email, work_email in rows:
        # Neutralize every free-text cell (#169) — a name/email/net_id starting
        # with a formula lead char would otherwise export as an executable cell.
        writer.writerow(
            [
                _fmt(_attendee_name(alumni), "str"),
                _fmt(personal_email or work_email or "", "str"),
                _fmt(alumni.net_id or "", "str"),
            ]
        )

    # Disclosure record: WHAT left the system (row count + the fixed column set)
    # and WHICH event — never the data itself. Mirrors the alumni export's
    # self-contained audit summary so the trail stays reconstructable if the
    # column set ever changes.
    session.add(
        AuditLog(
            user_id=user.user_id,
            action_type="export_event_attendees",
            entity_type="event",
            entity_id=event_id,
            new_value=(
                f"rows={len(rows)}; columns=name,email,net_id; "
                f"event={event.event_name!r}"
            ),
        )
    )
    await session.commit()

    # event_id is a path int (FastAPI-validated), so this header value is always
    # a safe decimal string. Do NOT extend this filename with free-text DB fields
    # (e.g. event name) without RFC 5987 encoding — that would open header
    # injection into Content-Disposition.
    filename = f"event_{event_id}_attendees.csv"
    return Response(
        content=buffer.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.post(
    "/{event_id}/attendees/import/preview", response_model=EventAttendeeImportPreview
)
async def preview_event_attendee_import(
    event_id: IdPath,
    _: RequireFullAccess,
    session: SessionDep,
    file: Annotated[UploadFile, File()],
) -> dict | JSONResponse:
    """Dry-run an attendee CSV against an EXISTING event (full_access, NO writes).

    Same file shape as ``POST /events/import`` (one template serves both), but
    the event already exists — so nothing about the event's identity is read
    from the request and the event is never touched. Reports each row as
    matched-new, already-attending (skipped), or unmatched (skipped). 404 if the
    event is unknown; a bad header set surfaces as ``columns_ok: false``."""
    event = await session.get(Event, event_id)
    if event is None:
        raise NotFoundError(f"Event {event_id} not found.")
    file_bytes = await _read_capped(file)
    if file_bytes is None:
        return _too_large_response()
    rows, header_errors = import_events.parse_and_map(file_bytes)
    if header_errors:
        return import_events.headers_bad_attendee_preview(header_errors, event)
    return await import_events.evaluate_for_event(session, rows, event)


@router.post("/{event_id}/attendees/import", response_model=EventAttendeeImportResult)
async def import_event_attendees(
    event_id: IdPath,
    user: RequireFullAccess,
    session: SessionDep,
    file: Annotated[UploadFile, File()],
) -> dict | JSONResponse:
    """Add an attendee CSV's roster to an EXISTING event (full_access).

    ADDS to the event — it never creates, replaces, or edits the event itself.
    Attendees already on the roster are skipped rather than 409ing, so re-running
    the same file is safe; unmatched Net IDs are reported and skipped. Each added
    attendee is audited as ``event``/``add_attendee``, identical to the manual
    single-attendee route. 404 if the event is unknown."""
    event = await session.get(Event, event_id)
    if event is None:
        raise NotFoundError(f"Event {event_id} not found.")
    file_bytes = await _read_capped(file)
    if file_bytes is None:
        return _too_large_response()
    rows, header_errors = import_events.parse_and_map(file_bytes)
    if header_errors:
        return {
            "imported": False,
            "event_id": event_id,
            "added": 0,
            "skipped_existing": 0,
            "unmatched": [],
            "error": header_errors[0],
        }
    return await import_events.commit_attendees_for_event(
        session, rows, event, actor_user_id=user.user_id
    )


@router.post("/{event_id}/attendees", status_code=status.HTTP_201_CREATED)
async def add_event_attendee(
    event_id: IdPath,
    payload: AttendeeCreate,
    user: RequireFullAccess,
    session: SessionDep,
) -> dict:
    """Add an alumni to an event's attendance (full_access). 404 if the event or
    alumni is unknown; 409 if the (event, alumni) pair already exists. Audits the
    write (entity_type "event", action "add_attendee", entity_id event_id,
    new_value the alumni id/name).

    Note: this is the event-roster management surface and stays ``full_access``
    on purpose. Recording attendance from an alumnus's PROFILE
    (``POST /alumni/{id}/events``) is profile data-entry and is intentionally
    open to ``student`` via ``RequireAlumniEdit`` — a deliberate split, not an
    oversight. Students manage attendance per-alumnus, not from the event roster."""
    event = await session.get(Event, event_id)
    if event is None:
        raise NotFoundError(f"Event {event_id} not found.")
    alumni = await session.get(Alumni, payload.alumni_id)
    # Archived alumni are not valid attendees. The bulk importer already matches
    # active-only (repositories/net_id.match_net_ids), so reject them here too and
    # surface it as a 404 — an archived record is "not found" for this purpose,
    # keeping manual add and import consistent (#181).
    if alumni is None or alumni.archived:
        raise NotFoundError(f"Alumni {payload.alumni_id} not found.")

    existing = await session.scalar(
        select(EventAttendance.event_attendance_id).where(
            EventAttendance.event_id == event_id,
            EventAttendance.alumni_id == payload.alumni_id,
        )
    )
    if existing is not None:
        raise ConflictError(
            f"Alumni {payload.alumni_id} is already an attendee of event {event_id}."
        )

    attendance = EventAttendance(
        event_id=event_id,
        alumni_id=payload.alumni_id,
        attendance_status=payload.attendance_status,
        attendance_notes=payload.notes,
    )
    session.add(attendance)
    name = _attendee_name(alumni)
    session.add(
        AuditLog(
            user_id=user.user_id,
            action_type="add_attendee",
            entity_type="event",
            entity_id=event_id,
            new_value=f"{payload.alumni_id}: {name}",
        )
    )
    await session.commit()
    return {
        "alumni_id": alumni.alumni_id,
        "name": name,
        "graduation_year": alumni.graduation_year,
        "attendance_status": payload.attendance_status,
        "notes": payload.notes,
    }


@router.delete("/{event_id}/attendees/{alumni_id}")
async def remove_event_attendee(
    event_id: IdPath,
    alumni_id: IdPath,
    user: RequireFullAccess,
    session: SessionDep,
) -> dict:
    """Remove an alumni from an event's attendance (full_access). 404 if no such
    attendance row exists. Audits the write (entity_type "event", action
    "remove_attendee", entity_id event_id, old_value the alumni id)."""
    attendance = await session.scalar(
        select(EventAttendance).where(
            EventAttendance.event_id == event_id,
            EventAttendance.alumni_id == alumni_id,
        )
    )
    if attendance is None:
        raise NotFoundError(
            f"Alumni {alumni_id} is not an attendee of event {event_id}."
        )

    await session.delete(attendance)
    session.add(
        AuditLog(
            user_id=user.user_id,
            action_type="remove_attendee",
            entity_type="event",
            entity_id=event_id,
            old_value=str(alumni_id),
        )
    )
    await session.commit()
    return {"event_id": event_id, "alumni_id": alumni_id, "removed": True}
