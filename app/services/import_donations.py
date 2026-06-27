"""Bulk CSV donation importer (#161).

Turns a super-admin-filled CSV (columns ``Net ID``, ``Name``, ``Month``,
``Year``, ``Amount``) into validated donation rows — matched to existing alumni
**by Net ID** — and commits the importable ones in one transaction. Same
parse/evaluate/commit shape as ``import_csv`` / ``import_events``.

Policy (confirmed): an **unmatched Net ID**, a **bad/missing year**, a bad month,
or a **non-numeric / negative amount rejects that row**. Strict integrity — no
placeholder alumni are created. This endpoint set is super_admin-only, so the
preview may echo the amount back (the caller is authorized to see it).
"""

from __future__ import annotations

import csv
import io
import logging
from decimal import Decimal, InvalidOperation

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.audit import AuditLog
from app.models.donation import Donation
from app.repositories.net_id import match_net_ids, normalize_net_id

log = logging.getLogger(__name__)

MAX_UPLOAD_BYTES = 5 * 1024 * 1024  # 5 MiB
MAX_IMPORT_ROWS = 5000
_AMOUNT_MAX = Decimal("9999999999.99")  # numeric(12,2) ceiling

COL_NET_ID = "Net ID"
COL_NAME = "Name"
COL_MONTH = "Month"
COL_YEAR = "Year"
COL_AMOUNT = "Amount"
EXPECTED_HEADERS: list[str] = [COL_NET_ID, COL_NAME, COL_MONTH, COL_YEAR, COL_AMOUNT]
EXAMPLE_ROWS: list[list[str]] = [
    ["jdoe", "Jane Doe", "4", "2026", "250.00"],
    ["msmith", "Mark Smith", "11", "2025", "1000"],
]


# --- Stage 1: parse + map ----------------------------------------------------


def parse_and_map(
    file_bytes: bytes, max_rows: int | None = MAX_IMPORT_ROWS
) -> tuple[list[dict], list[str]]:
    """Parse the CSV into per-row dicts + header errors.

    Each row: ``{"row": int, "net_id": str, "name": str, "month": int|None,
    "year": int|None, "amount": Decimal|None, "error": str|None}``. ``error`` is
    set for a bad month / year / amount, which rejects the row downstream."""
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


def _parse_amount(raw: str) -> Decimal:
    """Parse a money cell ('$1,250.00' / '1000') to a 2dp non-negative Decimal."""
    cleaned = raw.replace("$", "").replace(",", "").strip()
    try:
        value = Decimal(cleaned)
    except InvalidOperation as exc:
        raise ValueError(f"{COL_AMOUNT}: expected a number, got {raw!r}.") from exc
    if value < 0:
        raise ValueError(f"{COL_AMOUNT}: must not be negative.")
    if value > _AMOUNT_MAX:
        raise ValueError(f"{COL_AMOUNT}: is too large.")
    return value.quantize(Decimal("0.01"))


def _map_row(row_num: int, index: dict[str, int], raw_row: list[str]) -> dict:
    net_id = _cell(index, raw_row, COL_NET_ID)
    name = _cell(index, raw_row, COL_NAME)
    raw_month = _cell(index, raw_row, COL_MONTH)
    raw_year = _cell(index, raw_row, COL_YEAR)
    raw_amount = _cell(index, raw_row, COL_AMOUNT)

    error: str | None = None
    month: int | None = None
    year: int | None = None
    amount: Decimal | None = None

    if not net_id:
        error = f"{COL_NET_ID}: required."

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
        "net_id": net_id,
        "name": name,
        "month": month,
        "year": year,
        "amount": amount,
        "error": error,
    }


# --- Stage 2: evaluate (dry-run) ---------------------------------------------


async def evaluate(session: AsyncSession, rows: list[dict]) -> dict:
    """Build the row-level dry-run report. Resolves every Net ID in one batch
    query and rejects any row with a coercion error or an unmatched Net ID. Exact
    repeats within the file (same net_id+month+year+amount) get a warning."""
    matched = await match_net_ids(session, [r["net_id"] for r in rows if r["net_id"]])

    seen: dict[tuple, int] = {}
    out_rows: list[dict] = []
    importable = rejected = 0

    for row in rows:
        blockers: list[dict] = []
        warnings: list[dict] = []

        if row["error"]:
            blockers.append({"code": "invalid_row", "message": row["error"]})
        else:
            norm = normalize_net_id(row["net_id"])
            alumni_id = matched.get(norm)
            if alumni_id is None:
                blockers.append(
                    {
                        "code": "unmatched_net_id",
                        "message": (
                            f"Net ID {row['net_id']!r} did not match an active "
                            "alumnus."
                        ),
                    }
                )
            else:
                key = (norm, row["month"], row["year"], row["amount"])
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
                "net_id": row["net_id"],
                "name": row["name"],
                "month": row["month"],
                "year": row["year"],
                "amount": float(row["amount"]) if row["amount"] is not None else None,
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
    matched = await match_net_ids(session, [r["net_id"] for r in rows if r["net_id"]])
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
        alumni_id = matched.get(normalize_net_id(parsed["net_id"]))
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
