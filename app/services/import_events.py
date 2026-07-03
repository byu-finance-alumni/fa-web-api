"""Bulk CSV event importer (#149, #156).

**One CSV = one event's attendee list.** The event's identity (title, date,
type, location, notes) is captured in the import wizard, NOT the CSV — the CSV is
purely the roster, one row per attendee, keyed on **Net ID** (the ``Name`` column
is for human confirmation only). Attendees are matched to existing alumni by Net
ID and committed with the new event in one transaction, through the same models
the single-event API uses (so the audit logging fires identically).

Three stages, mirroring ``import_csv``:

  1. :func:`parse_and_map` — pure parse + header validation + per-row coercion
     (blank Net ID). No DB.
  2. :func:`evaluate` — dry-run report for ONE event: validate the event
     identity, resolve every Net ID in ONE batch query, flag unmatched /
     duplicate attendees and a pre-existing event. NO writes.
  3. :func:`commit_import` — re-evaluate, then insert the event + its matched
     attendees under a SAVEPOINT.

Policy (confirmed): the event identity must be valid and not already exist (same
title + date). **Unmatched Net IDs never block the import** — the event is
created with its matched attendees and the unmatched rows are reported clearly
(strict integrity: no placeholder alumni are ever created).
"""

from __future__ import annotations

import csv
import datetime
import io
import logging

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.audit import AuditLog
from app.models.event import Event, EventAttendance
from app.repositories.net_id import match_net_ids, normalize_net_id

log = logging.getLogger(__name__)

# Upload guards. Byte cap is enforced at the route; the row cap is enforced here
# so /preview and /commit share it.
MAX_UPLOAD_BYTES = 5 * 1024 * 1024  # 5 MiB
MAX_IMPORT_ROWS = 5000  # attendee rows for ONE event
_TITLE_MAX = 255

# One CSV = one event's attendee list. The event identity lives in the wizard,
# NOT the CSV, so the only columns are the attendee's Net ID (the key) and Name
# (confirmation only). A drift here surfaces as a header error.
COL_NET_ID = "Net ID"
COL_NAME = "Name"
EXPECTED_HEADERS: list[str] = [COL_NET_ID, COL_NAME]
EXAMPLE_ROWS: list[list[str]] = [
    ["jdoe", "Jane Doe"],
    ["msmith", "Mark Smith"],
    ["alee", "Amy Lee"],
]


# --- Stage 1: parse + map ----------------------------------------------------


def parse_and_map(
    file_bytes: bytes, max_rows: int | None = MAX_IMPORT_ROWS
) -> tuple[list[dict], list[str]]:
    """Parse the CSV into per-attendee row dicts + header errors.

    Each row dict: ``{"row": int, "net_id": str, "attendee_name": str,
    "error": str|None}``. ``row`` is the 1-based spreadsheet row (header = row 1).
    ``error`` is set for a blank Net ID, which drops that attendee row.

    Decoded as ``utf-8-sig`` so an Excel BOM is stripped. Over ``max_rows`` (or a
    non-decodable / empty file / bad header set) returns ``([], [errors])``.
    """
    try:
        text = file_bytes.decode("utf-8-sig")
    except UnicodeDecodeError:
        return [], [
            "The file could not be read as UTF-8. Re-save it as UTF-8 (or "
            "UTF-8 with BOM) from Excel and re-upload."
        ]
    reader = csv.reader(io.StringIO(text))
    try:
        header_row = next(reader)
    except StopIteration:
        return [], ["The file is empty."]

    headers = [h.strip() for h in header_row]
    header_errors = _validate_headers(headers)
    if header_errors:
        return [], header_errors

    index = {h: i for i, h in enumerate(headers)}
    rows: list[dict] = []
    for offset, raw_row in enumerate(reader, start=2):
        if not any(cell.strip() for cell in raw_row):
            continue
        if max_rows is not None and len(rows) >= max_rows:
            return [], [
                f"File exceeds the {max_rows:,}-row import limit. Split into "
                "smaller batches."
            ]
        rows.append(_map_row(offset, index, raw_row))
    return rows, header_errors


def _validate_headers(headers: list[str]) -> list[str]:
    errors: list[str] = []
    seen = set(headers)
    expected = set(EXPECTED_HEADERS)
    for missing in EXPECTED_HEADERS:
        if missing not in seen:
            errors.append(f"Missing required column: {missing!r}.")
    for extra in headers:
        if extra and extra not in expected:
            errors.append(f"Unexpected column: {extra!r}.")
    # A duplicated header maps ambiguously (the header->index map is last-wins),
    # so reject rather than silently read the rightmost column.
    for dup in sorted({h for h in headers if h and headers.count(h) > 1}):
        errors.append(f"Duplicate column: {dup!r}.")
    return errors


def _cell(index: dict[str, int], raw_row: list[str], header: str) -> str:
    col = index.get(header)
    if col is None or col >= len(raw_row):
        return ""
    return (raw_row[col] or "").strip()


def _map_row(row_num: int, index: dict[str, int], raw_row: list[str]) -> dict:
    net_id = _cell(index, raw_row, COL_NET_ID)
    name = _cell(index, raw_row, COL_NAME)
    error = None if net_id else f"{COL_NET_ID}: required."
    return {
        "row": row_num,
        "net_id": net_id,
        "attendee_name": name,
        "error": error,
    }


# --- Event identity (from the wizard, not the CSV) ---------------------------


def normalize_event_meta(
    event_name: str,
    event_date: str | None = None,
    event_type: str | None = None,
    event_location: str | None = None,
    event_notes: str | None = None,
) -> dict:
    """Trim the wizard-supplied event fields into a normalized meta dict. Empty
    optional fields become ``None`` (so they don't write blank strings)."""

    def _clean(v: str | None) -> str | None:
        v = (v or "").strip()
        return v or None

    return {
        "event_name": (event_name or "").strip(),
        "event_date": _clean(event_date),
        "event_type": _clean(event_type),
        "event_location": _clean(event_location),
        "event_notes": _clean(event_notes),
    }


def _validate_event(meta: dict) -> list[dict]:
    """Blocker-level problems with the event identity itself (bad title/date)."""
    errors: list[dict] = []
    title = meta["event_name"]
    if not title:
        errors.append({"code": "invalid_event", "message": "Event title is required."})
    elif len(title) > _TITLE_MAX:
        errors.append(
            {
                "code": "invalid_event",
                "message": f"Event title must be at most {_TITLE_MAX} characters.",
            }
        )
    if meta["event_date"]:
        try:
            datetime.date.fromisoformat(meta["event_date"])
        except ValueError:
            errors.append(
                {
                    "code": "invalid_event",
                    "message": (
                        f"Event date must be YYYY-MM-DD, got "
                        f"{meta['event_date']!r}."
                    ),
                }
            )
    return errors


async def _event_exists(session: AsyncSession, meta: dict) -> bool:
    """True if an event with the same (case-insensitive title, date) exists."""
    date = (
        datetime.date.fromisoformat(meta["event_date"])
        if meta["event_date"]
        else None
    )
    stmt = select(Event.event_id).where(
        func.lower(func.trim(Event.event_name)) == meta["event_name"].strip().lower()
    )
    stmt = stmt.where(
        Event.event_date == date if date is not None else Event.event_date.is_(None)
    )
    return (await session.scalar(stmt.limit(1))) is not None


# --- Stage 2: evaluate (dry-run, ONE event) ----------------------------------


async def evaluate(session: AsyncSession, rows: list[dict], meta: dict) -> dict:
    """Build the dry-run report for ONE event + its attendee list. NO writes.

    Validates the event identity, resolves every Net ID in one batch query, and
    reports each attendee as matched or unmatched (unmatched are skipped, not
    blocking). The event is ``importable`` when its identity is valid and it
    doesn't already exist — unmatched attendees never block it."""
    event_errors = _validate_event(meta)
    if not event_errors and await _event_exists(session, meta):
        event_errors.append(
            {
                "code": "duplicate_event",
                "message": (
                    f"An event titled {meta['event_name']!r} on "
                    f"{meta['event_date'] or '(no date)'} already exists."
                ),
            }
        )

    matched = await match_net_ids(session, [r["net_id"] for r in rows if r["net_id"]])

    attendees: list[dict] = []
    warnings: list[dict] = []
    seen_net: set[str] = set()
    matched_count = unmatched_count = 0

    for r in rows:
        if r["error"]:
            # A blank Net ID row — surface as a skipped/invalid attendee.
            warnings.append({"code": "invalid_row", "message": r["error"]})
            continue
        net = r["net_id"]
        norm = normalize_net_id(net)
        if norm in seen_net:
            warnings.append(
                {
                    "code": "duplicate_attendee",
                    "message": (
                        f"Net ID {net!r} is listed more than once; counted once."
                    ),
                }
            )
            continue
        seen_net.add(norm)
        alumni_id = matched.get(norm)
        if alumni_id is None:
            unmatched_count += 1
        else:
            matched_count += 1
        attendees.append(
            {
                "row": r["row"],
                "net_id": net,
                "name": r["attendee_name"],
                "matched": alumni_id is not None,
                "alumni_id": alumni_id,
            }
        )

    # A non-empty roster where NOTHING matched almost always means the wrong CSV
    # was uploaded for this event — warn before it silently creates an empty
    # event. Importable stays true (per policy) so the operator can proceed if
    # it's genuinely intended.
    if attendees and matched_count == 0:
        warnings.append(
            {
                "code": "no_attendees_matched",
                "message": (
                    "None of the attendee Net IDs matched an active alumnus — "
                    "the event would be created with no attendees. Check you "
                    "uploaded the right CSV."
                ),
            }
        )

    return {
        "columns_ok": True,
        "header_errors": [],
        "event": {
            "event_name": meta["event_name"],
            "event_date": meta["event_date"],
            "event_type": meta["event_type"],
            "event_location": meta["event_location"],
            "event_notes": meta["event_notes"],
        },
        "importable": not event_errors,
        "event_errors": event_errors,
        "summary": {
            "total_rows": len(rows),
            "attendees_matched": matched_count,
            "attendees_unmatched": unmatched_count,
        },
        "attendees": attendees,
        "warnings": warnings,
    }


# --- Stage 3: commit ---------------------------------------------------------


async def commit_import(
    session: AsyncSession,
    rows: list[dict],
    meta: dict,
    actor_user_id: int | None = None,
) -> dict:
    """Re-evaluate and, if importable, insert the event + its MATCHED attendees in
    ONE transaction under a SAVEPOINT. Unmatched attendees are skipped and
    reported. Audits the event (``event``/``create``) and each attendee
    (``event``/``add_attendee``), mirroring the manual API. Returns:
        ``{"imported": bool, "event_id": int|None, "imported_attendees": int,
           "unmatched": [{"row", "net_id", "name"}], "event_error": str|None}``
    """
    report = await evaluate(session, rows, meta)
    unmatched = [
        {"row": a["row"], "net_id": a["net_id"], "name": a["name"]}
        for a in report["attendees"]
        if not a["matched"]
    ]

    if not report["importable"]:
        return {
            "imported": False,
            "event_id": None,
            "imported_attendees": 0,
            "unmatched": unmatched,
            "event_error": _reject_reason(report),
        }

    ev = report["event"]
    imported_attendees = 0
    async with session.begin_nested() as savepoint:
        try:
            event_date = (
                datetime.date.fromisoformat(ev["event_date"])
                if ev["event_date"]
                else None
            )
            event = Event(
                event_name=ev["event_name"],
                event_date=event_date,
                event_type=ev["event_type"],
                event_location=ev["event_location"],
                event_notes=ev["event_notes"],
                logged_by_user_id=actor_user_id,
            )
            session.add(event)
            await session.flush()
            session.add(
                AuditLog(
                    user_id=actor_user_id,
                    action_type="create",
                    entity_type="event",
                    entity_id=event.event_id,
                    new_value="bulk_import",
                )
            )
            for att in report["attendees"]:
                if not att["matched"]:
                    continue
                session.add(
                    EventAttendance(
                        event_id=event.event_id,
                        alumni_id=att["alumni_id"],
                    )
                )
                session.add(
                    AuditLog(
                        user_id=actor_user_id,
                        action_type="add_attendee",
                        entity_type="event",
                        entity_id=event.event_id,
                        new_value=f"{att['alumni_id']}: {att['name']}",
                    )
                )
                imported_attendees += 1
        except Exception as exc:  # noqa: BLE001 - record + report
            await savepoint.rollback()
            log.exception("Unexpected error importing event %r", ev["event_name"])
            return {
                "imported": False,
                "event_id": None,
                "imported_attendees": 0,
                "unmatched": unmatched,
                "event_error": f"Unexpected error ({exc.__class__.__name__}).",
            }

    await session.commit()
    return {
        "imported": True,
        "event_id": event.event_id,
        "imported_attendees": imported_attendees,
        "unmatched": unmatched,
        "event_error": None,
    }


def _reject_reason(report: dict) -> str:
    errors = report.get("event_errors") or []
    if errors:
        return errors[0]["message"]
    return "Rejected."


# --- CSV template ------------------------------------------------------------


def build_template_csv() -> str:
    """Return the attendee-list CSV template as text: the exact headers plus a
    few example attendee rows (Net ID is the key; Name is confirmation only)."""
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(EXPECTED_HEADERS)
    for row in EXAMPLE_ROWS:
        writer.writerow(row)
    return buffer.getvalue()
