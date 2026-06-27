"""Bulk CSV event importer (#156).

Turns a department-filled CSV into validated event groups and attaches their
attendees — matched to existing alumni **by Net ID** — committing the importable
events in one transaction through the same models the single-event API uses (so
the audit logging fires identically).

CSV shape: ONE ROW PER ATTENDEE. Rows that share the same (``Event title``,
``Event date``) form a single event group; the ``Attendee name`` column is for
human confirmation only — the Net ID is the key. An event with no attendee rows
is still creatable (leave the attendee columns blank on a single row).

Three stages, mirroring ``import_csv``:

  1. :func:`parse_and_map` — pure parse + header validation + per-row coercion
     (bad date, blank title). No DB.
  2. :func:`evaluate` — dry-run report grouped by event: resolve every Net ID in
     ONE batch query, flag unmatched / duplicate-in-DB, NO writes.
  3. :func:`commit_import` — re-evaluate, then insert each importable event + its
     matched attendees, each event under its own SAVEPOINT.

Policy (confirmed): **any unmatched Net ID, a bad/missing date, a missing title,
or an event that already exists (same title + date) rejects that whole event
group.** Strict integrity — no placeholder alumni are ever created.
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
MAX_IMPORT_ROWS = 5000  # attendee rows (many per event), so a touch higher
_TITLE_MAX = 255

# Exact, ordered template headers. A drift here surfaces as a header error.
COL_TITLE = "Event title"
COL_DATE = "Event date (YYYY-MM-DD)"
COL_NET_ID = "Attendee Net ID"
COL_NAME = "Attendee name"
EXPECTED_HEADERS: list[str] = [COL_TITLE, COL_DATE, COL_NET_ID, COL_NAME]
EXAMPLE_ROWS: list[list[str]] = [
    ["Spring Finance Banquet", "2026-04-15", "jdoe", "Jane Doe"],
    ["Spring Finance Banquet", "2026-04-15", "msmith", "Mark Smith"],
    ["Wall Street Trek", "2026-03-02", "alee", "Amy Lee"],
]


# --- Stage 1: parse + map ----------------------------------------------------


def parse_and_map(
    file_bytes: bytes, max_rows: int | None = MAX_IMPORT_ROWS
) -> tuple[list[dict], list[str]]:
    """Parse the CSV into per-attendee row dicts + header errors.

    Each row dict: ``{"row": int, "event_title": str, "event_date": str|None,
    "net_id": str, "attendee_name": str, "error": str|None}``. ``row`` is the
    1-based spreadsheet row (header = row 1). ``error`` is set for a bad date or
    a blank title, which rejects the row's whole event group downstream.

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
    return errors


def _cell(index: dict[str, int], raw_row: list[str], header: str) -> str:
    col = index.get(header)
    if col is None or col >= len(raw_row):
        return ""
    return (raw_row[col] or "").strip()


def _map_row(row_num: int, index: dict[str, int], raw_row: list[str]) -> dict:
    title = _cell(index, raw_row, COL_TITLE)
    raw_date = _cell(index, raw_row, COL_DATE)
    net_id = _cell(index, raw_row, COL_NET_ID)
    name = _cell(index, raw_row, COL_NAME)

    error: str | None = None
    event_date: str | None = None
    if not title:
        error = f"{COL_TITLE}: required."
    elif len(title) > _TITLE_MAX:
        error = f"{COL_TITLE}: must be at most {_TITLE_MAX} characters."
    if error is None and raw_date:
        try:
            datetime.date.fromisoformat(raw_date)
            event_date = raw_date
        except ValueError:
            error = f"{COL_DATE}: expected a date as YYYY-MM-DD, got {raw_date!r}."

    return {
        "row": row_num,
        "event_title": title,
        "event_date": event_date,
        "net_id": net_id,
        "attendee_name": name,
        "error": error,
    }


# --- Stage 2: evaluate (dry-run, grouped by event) ---------------------------


def _group_key(row: dict) -> tuple[str, str | None]:
    """Group identity: case-insensitive title + date (None date is its own bucket)."""
    return (row["event_title"].strip().lower(), row["event_date"])


async def _load_existing_events(
    session: AsyncSession,
) -> set[tuple[str, str | None]]:
    """Existing (lower(title), date-iso) pairs, to flag duplicate events in DB."""
    rows = (
        await session.execute(
            select(func.lower(Event.event_name), Event.event_date)
        )
    ).all()
    out: set[tuple[str, str | None]] = set()
    for name, date in rows:
        out.add((name, date.isoformat() if date else None))
    return out


async def evaluate(session: AsyncSession, rows: list[dict]) -> dict:
    """Build the grouped dry-run report. NO writes.

    Groups attendee rows by (title, date); resolves every Net ID in one batch
    query; rejects a group if it has a coercion error, an unmatched Net ID, or
    already exists in the DB. Duplicate Net IDs within a group are de-duplicated
    with a warning (not a blocker)."""
    all_net_ids = [r["net_id"] for r in rows if r["net_id"]]
    matched = await match_net_ids(session, all_net_ids)
    existing = await _load_existing_events(session)

    # Preserve first-seen order of event groups.
    order: list[tuple[str, str | None]] = []
    grouped: dict[tuple[str, str | None], list[dict]] = {}
    for row in rows:
        key = _group_key(row)
        if key not in grouped:
            grouped[key] = []
            order.append(key)
        grouped[key].append(row)

    events_out: list[dict] = []
    importable = rejected = matched_count = unmatched_count = 0

    for key in order:
        group = grouped[key]
        first = group[0]
        blockers: list[dict] = []
        warnings: list[dict] = []

        # Coercion errors anywhere in the group reject it.
        row_errors = [r["error"] for r in group if r["error"]]
        for msg in row_errors:
            blockers.append({"code": "invalid_row", "message": msg})

        # Already in the DB (same title + date).
        if (key[0], key[1]) in existing:
            blockers.append(
                {
                    "code": "duplicate_event",
                    "message": (
                        f"An event titled {first['event_title']!r} on "
                        f"{first['event_date'] or '(no date)'} already exists."
                    ),
                }
            )

        # Resolve + dedupe attendees.
        attendees: list[dict] = []
        seen_net: set[str] = set()
        for r in group:
            net = r["net_id"]
            if not net:
                continue  # an event with no attendees is allowed
            norm = normalize_net_id(net)
            if norm in seen_net:
                warnings.append(
                    {
                        "code": "duplicate_attendee",
                        "message": (
                            f"Net ID {net!r} is listed more than once for this "
                            "event; counted once."
                        ),
                    }
                )
                continue
            seen_net.add(norm)
            alumni_id = matched.get(norm)
            if alumni_id is None:
                unmatched_count += 1
                blockers.append(
                    {
                        "code": "unmatched_net_id",
                        "message": (
                            f"Net ID {net!r} did not match an active alumnus."
                        ),
                    }
                )
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

        status = "rejected" if blockers else "importable"
        if status == "importable":
            importable += 1
        else:
            rejected += 1

        events_out.append(
            {
                "event_title": first["event_title"],
                "event_date": first["event_date"],
                "status": status,
                "attendee_count": len(attendees),
                "attendees": attendees,
                "blockers": blockers,
                "warnings": warnings,
            }
        )

    return {
        "columns_ok": True,
        "header_errors": [],
        "summary": {
            "total_rows": len(rows),
            "events": len(order),
            "importable_events": importable,
            "rejected_events": rejected,
            "attendees_matched": matched_count,
            "attendees_unmatched": unmatched_count,
        },
        "events": events_out,
    }


# --- Stage 3: commit ---------------------------------------------------------


async def commit_import(
    session: AsyncSession,
    rows: list[dict],
    actor_user_id: int | None = None,
) -> dict:
    """Re-evaluate and insert every importable event + attendees in ONE txn.

    Each event is created under its own SAVEPOINT (``begin_nested``) so a failure
    on one event rolls back only that event, leaving earlier ones intact. Each
    created event is audited (``event``/``create``) and each attendee
    (``event``/``add_attendee``), mirroring the manual API. Returns:
        ``{"imported_events": int, "imported_attendees": int, "skipped_events":
           int, "rejects": [{"event": str, "date": str|None, "reason": str}]}``
    """
    report = await evaluate(session, rows)

    imported_events = imported_attendees = skipped = 0
    rejects: list[dict] = []

    for ev in report["events"]:
        if ev["status"] != "importable":
            skipped += 1
            rejects.append(
                {
                    "event": ev["event_title"],
                    "date": ev["event_date"],
                    "reason": _reject_reason(ev),
                }
            )
            continue

        async with session.begin_nested() as savepoint:
            try:
                event_date = (
                    datetime.date.fromisoformat(ev["event_date"])
                    if ev["event_date"]
                    else None
                )
                event = Event(
                    event_name=ev["event_title"].strip(),
                    event_date=event_date,
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
                for att in ev["attendees"]:
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
                imported_events += 1
            except Exception as exc:  # noqa: BLE001 - record + continue per event
                await savepoint.rollback()
                skipped += 1
                rejects.append(
                    {
                        "event": ev["event_title"],
                        "date": ev["event_date"],
                        "reason": f"Unexpected error ({exc.__class__.__name__})",
                    }
                )
                log.exception("Unexpected error importing event %r", ev["event_title"])

    if imported_events:
        await session.commit()

    return {
        "imported_events": imported_events,
        "imported_attendees": imported_attendees,
        "skipped_events": skipped,
        "rejects": rejects,
    }


def _reject_reason(ev: dict) -> str:
    blockers = ev.get("blockers") or []
    if blockers:
        return blockers[0]["message"]
    return "Rejected."


# --- CSV template ------------------------------------------------------------


def build_template_csv() -> str:
    """Return the events import template as CSV text: the exact headers plus a
    few example rows (two attendees share one event to show grouping)."""
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(EXPECTED_HEADERS)
    for row in EXAMPLE_ROWS:
        writer.writerow(row)
    return buffer.getvalue()
