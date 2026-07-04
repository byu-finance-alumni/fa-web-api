"""Bulk CSV donation importer (#148, #161).

Turns a super-admin-filled CSV into validated donation rows and commits the
importable ones in one transaction. Same parse/evaluate/commit shape as
``import_csv`` / ``import_events``.

Donor matching (#148): donation records are keyed on **MSTID**, not Net ID. Each
row is resolved to an active alumnus by:

  * **MSTID** (primary) — trimmed, case-insensitive; then
  * **last + first name** (fallback) — when the row has no MSTID or its MSTID
    doesn't resolve.

CSV columns: ``MSTID``, ``First name``, ``Last name``, ``Month``, ``Year``,
``Amount``. A row must carry an MSTID *or* both names (something to match on).

Policy (confirmed): a row is **rejected** on a bad/missing year, a bad month, a
non-numeric / non-positive amount, nothing to match on, an **unmatched** donor,
or an **ambiguous** match (an MSTID or name that resolves to more than one active
alumnus — surfaced so a human disambiguates rather than mis-attributing a
donation). Strict integrity — no placeholder alumni are created. This endpoint
set is super_admin-only, so the preview may echo the amount back.
"""

from __future__ import annotations

import csv
import io
import logging
from decimal import Decimal, InvalidOperation

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.audit import AuditLog
from app.models.donation import Donation
from app.repositories.donor_match import (
    match_mstids,
    match_names,
    normalize_mstid,
    normalize_name_key,
)

log = logging.getLogger(__name__)

# 4 MiB, deliberately BELOW Vercel's ~4.5 MB serverless Function request-body
# ceiling so the app's own friendly 413 fires instead of a raw platform error.
MAX_UPLOAD_BYTES = 4 * 1024 * 1024  # 4 MiB
MAX_IMPORT_ROWS = 5000
_AMOUNT_MAX = Decimal("9999999999.99")  # numeric(12,2) ceiling

COL_MSTID = "MSTID"
COL_FIRST = "First name"
COL_LAST = "Last name"
COL_MONTH = "Month"
COL_YEAR = "Year"
COL_AMOUNT = "Amount"
EXPECTED_HEADERS: list[str] = [
    COL_MSTID,
    COL_FIRST,
    COL_LAST,
    COL_MONTH,
    COL_YEAR,
    COL_AMOUNT,
]
EXAMPLE_ROWS: list[list[str]] = [
    ["100200300", "Jane", "Doe", "4", "2026", "250.00"],
    # No MSTID -> matched on first + last name.
    ["", "Mark", "Smith", "11", "2025", "1000"],
]


# --- Stage 1: parse + map ----------------------------------------------------


def parse_and_map(
    file_bytes: bytes, max_rows: int | None = MAX_IMPORT_ROWS
) -> tuple[list[dict], list[str]]:
    """Parse the CSV into per-row dicts + header errors.

    Each row: ``{"row": int, "mstid": str, "first_name": str, "last_name": str,
    "month": int|None, "year": int|None, "amount": Decimal|None,
    "error": str|None}``. ``error`` is set for a row with nothing to match on or
    a bad month / year / amount, which rejects the row downstream."""
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


def _parse_amount(raw: str) -> Decimal:
    """Parse a money cell ('$1,250.00' / '1000') to a 2dp non-negative Decimal."""
    cleaned = raw.replace("$", "").replace(",", "").strip()
    try:
        value = Decimal(cleaned)
    except InvalidOperation as exc:
        raise ValueError(f"{COL_AMOUNT}: expected a number, got {raw!r}.") from exc
    if value <= 0:
        raise ValueError(f"{COL_AMOUNT}: must be greater than 0.")
    if value > _AMOUNT_MAX:
        raise ValueError(f"{COL_AMOUNT}: is too large.")
    return value.quantize(Decimal("0.01"))


def _map_row(row_num: int, index: dict[str, int], raw_row: list[str]) -> dict:
    mstid = _cell(index, raw_row, COL_MSTID)
    first_name = _cell(index, raw_row, COL_FIRST)
    last_name = _cell(index, raw_row, COL_LAST)
    raw_month = _cell(index, raw_row, COL_MONTH)
    raw_year = _cell(index, raw_row, COL_YEAR)
    raw_amount = _cell(index, raw_row, COL_AMOUNT)

    error: str | None = None
    month: int | None = None
    year: int | None = None
    amount: Decimal | None = None

    # A row needs SOMETHING to match a donor on: an MSTID, or both names.
    if not mstid and not (first_name and last_name):
        error = (
            f"{COL_MSTID}: required, or provide both {COL_FIRST} and "
            f"{COL_LAST} to match by name."
        )

    if error is None:
        if not raw_year:
            error = f"{COL_YEAR}: required."
        else:
            try:
                year = int(raw_year)
                if not 1900 <= year <= 2200:
                    raise ValueError
            except ValueError:
                error = f"{COL_YEAR}: expected a year 1900-2200, got {raw_year!r}."

    if error is None and raw_month:
        try:
            month = int(raw_month)
            if not 1 <= month <= 12:
                raise ValueError
        except ValueError:
            error = f"{COL_MONTH}: expected a month 1-12, got {raw_month!r}."

    if error is None:
        if not raw_amount:
            error = f"{COL_AMOUNT}: required."
        else:
            try:
                amount = _parse_amount(raw_amount)
            except ValueError as exc:
                error = str(exc)

    return {
        "row": row_num,
        "mstid": mstid,
        "first_name": first_name,
        "last_name": last_name,
        "month": month,
        "year": year,
        "amount": amount,
        "error": error,
    }


# --- Stage 2: evaluate (dry-run) ---------------------------------------------


def _display_name(row: dict) -> str:
    """Best-effort donor name for a row from its first + last cells."""
    name = " ".join(p for p in (row["first_name"], row["last_name"]) if p).strip()
    return name or "(unnamed)"


def _resolve(
    row: dict,
    by_mstid: dict[str, list[int]],
    by_name: dict[tuple[str, str], list[int]],
) -> tuple[int | None, str | None, dict | None]:
    """Resolve one row to (alumni_id, match_method, blocker).

    Tries MSTID first, then falls back to last+first name. Exactly one match ->
    (id, method, None); zero -> unmatched blocker; more than one -> ambiguous
    blocker (never auto-attributed)."""
    mstid = normalize_mstid(row["mstid"])
    if mstid:
        ids = by_mstid.get(mstid, [])
        if len(ids) == 1:
            return ids[0], "mstid", None
        if len(ids) > 1:
            return None, None, {
                "code": "ambiguous_mstid",
                "message": (
                    f"MSTID {row['mstid']!r} matches {len(ids)} active alumni — "
                    "resolve the duplicate before importing."
                ),
            }
        # MSTID present but unresolved: fall through to the name fallback.

    key = normalize_name_key(row["last_name"], row["first_name"])
    if key[0] and key[1]:
        ids = by_name.get(key, [])
        if len(ids) == 1:
            return ids[0], "name", None
        if len(ids) > 1:
            return None, None, {
                "code": "ambiguous_name",
                "message": (
                    f"{len(ids)} active alumni are named "
                    f"{row['first_name']} {row['last_name']} — add the MSTID to "
                    "pick the right one."
                ),
            }
        return None, None, {
            "code": "unmatched_donor",
            "message": (
                "No active alumnus matched "
                + (f"MSTID {row['mstid']!r} or " if row["mstid"] else "")
                + f"the name {row['first_name']} {row['last_name']}."
            ),
        }

    # MSTID was given but didn't resolve, and there's no name to fall back on.
    return None, None, {
        "code": "unmatched_donor",
        "message": (
            f"MSTID {row['mstid']!r} did not match an active alumnus, and no "
            "name was provided to fall back on."
        ),
    }


async def evaluate(session: AsyncSession, rows: list[dict]) -> dict:
    """Build the row-level dry-run report. Resolves donors by MSTID then name in
    two batch queries, rejecting any row with a coercion error, nothing to match
    on, an unmatched donor, or an ambiguous (multi-alumnus) match. Exact repeats
    within the file (same alumnus + month + year + amount) get a warning."""
    by_mstid = await match_mstids(session, [r["mstid"] for r in rows if r["mstid"]])
    by_name = await match_names(
        session, [(r["last_name"], r["first_name"]) for r in rows]
    )

    seen: dict[tuple, int] = {}
    out_rows: list[dict] = []
    importable = rejected = 0

    for row in rows:
        blockers: list[dict] = []
        warnings: list[dict] = []
        alumni_id: int | None = None
        match_method: str | None = None

        if row["error"]:
            blockers.append({"code": "invalid_row", "message": row["error"]})
        else:
            alumni_id, match_method, blocker = _resolve(row, by_mstid, by_name)
            if blocker is not None:
                blockers.append(blocker)
            else:
                key = (alumni_id, row["month"], row["year"], row["amount"])
                if key in seen:
                    warnings.append(
                        {
                            "code": "possible_duplicate_in_file",
                            "message": (
                                f"Identical donation also appears in row "
                                f"{seen[key]} of this file."
                            ),
                        }
                    )
                else:
                    seen[key] = row["row"]

        status = "rejected" if blockers else "importable"
        if status == "importable":
            importable += 1
        else:
            rejected += 1

        out_rows.append(
            {
                "row": row["row"],
                "mstid": row["mstid"],
                "name": _display_name(row),
                # How the donor was resolved ("mstid" high-confidence, "name" a
                # fallback worth a human glance); None when rejected.
                "match_method": match_method if status == "importable" else None,
                "month": row["month"],
                "year": row["year"],
                "amount": float(row["amount"]) if row["amount"] is not None else None,
                # Resolved alumnus for an importable row (None when rejected) —
                # commit reuses this instead of re-querying, so the row written is
                # exactly the row evaluated (no stale second match).
                "alumni_id": alumni_id if status == "importable" else None,
                "status": status,
                "blockers": blockers,
                "warnings": warnings,
            }
        )

    return {
        "columns_ok": True,
        "header_errors": [],
        "summary": {
            "total": len(rows),
            "importable": importable,
            "rejected": rejected,
        },
        "rows": out_rows,
    }


# --- Stage 3: commit ---------------------------------------------------------


async def commit_import(
    session: AsyncSession,
    rows: list[dict],
    actor_user_id: int | None = None,
) -> dict:
    """Re-evaluate and insert every importable donation in ONE transaction.

    Each row is created under its own SAVEPOINT so a failure rolls back only that
    row. Each insert is audited (``donation``/``create``). Returns:
        ``{"imported": int, "skipped": int, "rejects": [{"row", "name",
           "reason"}]}``"""
    report = await evaluate(session, rows)
    parsed_by_row = {r["row"]: r for r in rows}

    imported = skipped = 0
    rejects: list[dict] = []

    for evaluated in report["rows"]:
        row_num = evaluated["row"]
        if evaluated["status"] != "importable":
            skipped += 1
            rejects.append(
                {
                    "row": row_num,
                    "name": evaluated["name"],
                    "reason": _reject_reason(evaluated),
                }
            )
            continue

        parsed = parsed_by_row[row_num]
        # Reuse the alumnus resolved during evaluation (single Net-ID match) so
        # the inserted row matches exactly what was evaluated as importable.
        alumni_id = evaluated["alumni_id"]
        async with session.begin_nested() as savepoint:
            try:
                donation = Donation(
                    alumni_id=alumni_id,
                    amount=parsed["amount"],
                    donation_month=parsed["month"],
                    donation_year=parsed["year"],
                    logged_by_user_id=actor_user_id,
                )
                session.add(donation)
                await session.flush()
                # Record the actual amount + period (not a fixed sentinel) so the
                # FERPA disclosure trail can reconstruct exactly what financial
                # value this bulk row wrote, matching the single-row audit path.
                session.add(
                    AuditLog(
                        user_id=actor_user_id,
                        action_type="create",
                        entity_type="donation",
                        entity_id=donation.donation_id,
                        new_value=(
                            f"{parsed['amount']} "
                            f"({parsed['month'] or '-'}/{parsed['year']}) [bulk_import]"
                        ),
                    )
                )
                imported += 1
            except Exception as exc:  # noqa: BLE001 - record + continue per row
                await savepoint.rollback()
                skipped += 1
                rejects.append(
                    {
                        "row": row_num,
                        "name": evaluated["name"],
                        "reason": f"Unexpected error ({exc.__class__.__name__})",
                    }
                )
                log.exception("Unexpected error importing donation row %s", row_num)

    if imported:
        await session.commit()

    return {"imported": imported, "skipped": skipped, "rejects": rejects}


def _reject_reason(evaluated: dict) -> str:
    blockers = evaluated.get("blockers") or []
    if blockers:
        return blockers[0]["message"]
    return "Rejected."


# --- CSV template ------------------------------------------------------------


def build_template_csv() -> str:
    """Return the donations import template as CSV text: the exact headers plus a
    couple of example rows."""
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(EXPECTED_HEADERS)
    for row in EXAMPLE_ROWS:
        writer.writerow(row)
    return buffer.getvalue()
