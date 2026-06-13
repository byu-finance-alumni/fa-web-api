"""Bulk CSV alumni importer.

Turns a department-filled CSV (whose columns match the **Alumni** sheet of
``Alumni_Data_Intake_Template.xlsx``) into validated, cleaned, de-duplicated
``AlumniCreateFull`` payloads — then commits the importable ones in one
transaction through the same create path the single-record API uses (so the
data-hygiene cleaning and audit logging fire identically).

Three stages, mirroring the single-record hygiene preview/write split:

  1. :func:`parse_and_map` — pure(ish) parse + header validation + per-row
     header→payload mapping. No DB. Reports header errors and per-row mapping
     errors (bad date, unknown industry, etc.).
  2. :func:`evaluate` — dry-run report. For each row: build the Pydantic model
     (validation errors -> blocker), clean it, detect duplicates against the DB
     **and against earlier rows in the same file**, and collect recommended
     warnings. NO writes.
  3. :func:`commit_import` — re-evaluate, then insert every importable row in a
     single transaction (flush per row, commit once), recording per-row failures
     as rejects rather than aborting the whole import.

Column source: the header list is derived from
``scripts.export_intake_template._ALUMNI_COLUMNS`` so the importer, the xlsx
template, and the CSV template endpoint can never drift apart.

Nothing here mutates global state; the in-file dedup sets are per-call.
"""

from __future__ import annotations

import csv
import datetime
import io

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.dropdowns import validate_industry
from app.models.alumni import Alumni
from app.schemas.alumni import AlumniCreateFull
from app.services import alumni as alumni_service
from app.services import hygiene
from scripts.export_intake_template import _ALUMNI_COLUMNS

# --- Column mapping ----------------------------------------------------------
#
# Each Alumni-sheet header maps to a (section, field) target in the
# AlumniCreateFull payload. ``section`` is "core" for top-level fields or one of
# the nested-section keys (contact/career/education/engagement). ``kind`` selects
# the value coercion applied to the raw CSV cell.
#
#   "str"      -> trimmed string (empty -> omitted)
#   "int"      -> parsed integer (graduation_year, degree_year, ...)
#   "date"     -> parsed YYYY-MM-DD date (kept as ISO string for the schema)
#   "bool"     -> Yes/No/true/1 -> bool
#   "industry" -> validated against the controlled vocab (invalid -> row error)
#   "spouse"   -> Spouse BYU ID; resolved to spouse_alumni_id in evaluate()
#
# The (section, field, kind) tuples are keyed by the EXACT header text from the
# xlsx template so a drift in the template surfaces as a header error here.

_MAPPING: dict[str, tuple[str, str, str]] = {
    # --- Identity ---
    "BYU ID (9 digits)": ("core", "byu_id", "str"),
    "Net ID": ("core", "net_id", "str"),
    "First name": ("core", "first_name", "str"),
    "Middle name": ("core", "middle_name", "str"),
    "Last name": ("core", "last_name", "str"),
    "Preferred first name": ("core", "preferred_first_name", "str"),
    "Gender": ("core", "gender", "str"),
    "Birthday (YYYY-MM-DD)": ("core", "birth_date", "date"),
    "Graduation year": ("core", "graduation_year", "int"),
    "Finance program year": ("core", "finance_program_year", "int"),
    "Graduate degree": ("core", "graduate_degree", "str"),
    "LinkedIn URL": ("core", "linkedin_url", "str"),
    "Deceased? (Yes/No)": ("core", "deceased", "bool"),
    "Notes": ("core", "notes", "str"),
    # --- Spouse ---
    "Spouse first name": ("core", "spouse_first_name", "str"),
    "Spouse last name": ("core", "spouse_last_name", "str"),
    "Spouse birthday (YYYY-MM-DD)": ("core", "spouse_birth_date", "date"),
    "Spouse BYU ID (if also an alumnus)": ("core", "spouse_alumni_id", "spouse"),
    # --- Contact ---
    "Personal email": ("contact", "personal_email", "str"),
    "Work email": ("contact", "work_email", "str"),
    "Phone": ("contact", "phone", "str"),
    "Address line 1": ("contact", "address_line_1", "str"),
    "Address line 2": ("contact", "address_line_2", "str"),
    "City": ("contact", "city", "str"),
    "State": ("contact", "state", "str"),
    "ZIP": ("contact", "zip", "str"),
    "Country": ("contact", "country", "str"),
    "Region": ("contact", "region", "str"),
    # --- Current career ---
    "Current employer": ("career", "current_employer", "str"),
    "Current title": ("career", "current_title", "str"),
    "Current industry (see Reference sheet)": (
        "career",
        "current_industry",
        "industry",
    ),
    "Secondary industry (see Reference sheet)": (
        "career",
        "current_industry_secondary",
        "industry",
    ),
    "Current city": ("career", "current_city", "str"),
    "Current state": ("career", "current_state", "str"),
    "Current country": ("career", "current_country", "str"),
    "Current ZIP": ("career", "current_zip", "str"),
    "Seniority level": ("career", "seniority_level", "str"),
    # --- Education ---
    "University": ("education", "university", "str"),
    "College": ("education", "college", "str"),
    "Department": ("education", "department", "str"),
    "Degree": ("education", "degree", "str"),
    "Major": ("education", "major", "str"),
    "Degree status": ("education", "degree_status", "str"),
    "Degree year": ("education", "degree_year", "int"),
    # --- Program engagement ---
    "Willing to host NetTrek (Yes/No)": (
        "engagement",
        "nettrek_host_willing",
        "bool",
    ),
    "Willing to attend finance conference (Yes/No)": (
        "engagement",
        "finance_conference_willing",
        "bool",
    ),
    "Willing to mentor (Yes/No)": ("engagement", "mentor_willing", "bool"),
    "Willing to sponsor company event (Yes/No)": (
        "engagement",
        "company_event_sponsor_willing",
        "bool",
    ),
    "Willing to guest speak (Yes/No)": (
        "engagement",
        "guest_speaker_willing",
        "bool",
    ),
    "Willing to help at events (Yes/No)": (
        "engagement",
        "help_at_event_willing",
        "bool",
    ),
    "Willing to host case competition (Yes/No)": (
        "engagement",
        "case_competition_host_willing",
        "bool",
    ),
    "Willing to mentor — Women in Finance (Yes/No)": (
        "engagement",
        "women_in_finance_mentor_willing",
        "bool",
    ),
    "Hired a finance intern (Yes/No)": (
        "engagement",
        "hired_finance_intern",
        "bool",
    ),
    "Hired finance full-time (Yes/No)": (
        "engagement",
        "hired_finance_full_time",
        "bool",
    ),
    "PIFF donor (Yes/No)": ("engagement", "piff_donor", "bool"),
    "CFP designation (Yes/No)": ("engagement", "cfp_designation", "bool"),
    "CFA designation (Yes/No)": ("engagement", "cfa_designation", "bool"),
    "Engagement notes": ("engagement", "engagement_notes", "str"),
}

# Ordered list of expected headers — same source + order as the xlsx Alumni
# sheet (the template generator's _ALUMNI_COLUMNS). Used for header validation
# and to build the downloadable CSV template.
EXPECTED_HEADERS: list[str] = [header for header, _ in _ALUMNI_COLUMNS]
EXAMPLE_ROW: list[str] = [example for _, example in _ALUMNI_COLUMNS]

_TRUE_TOKENS = frozenset({"yes", "true", "1", "y", "t"})
_FALSE_TOKENS = frozenset({"no", "false", "0", "n", "f", ""})


# --- Value coercion ----------------------------------------------------------


class _CellError(ValueError):
    """A per-cell coercion failure carrying the human header for the message."""


def _coerce_bool(header: str, raw: str) -> bool:
    token = raw.strip().lower()
    if token in _TRUE_TOKENS:
        return True
    if token in _FALSE_TOKENS:
        return False
    raise _CellError(f"{header}: expected Yes or No, got {raw!r}.")


def _coerce_int(header: str, raw: str) -> int:
    try:
        return int(raw.strip())
    except ValueError as exc:
        raise _CellError(f"{header}: expected a whole number, got {raw!r}.") from exc


def _coerce_date(header: str, raw: str) -> str:
    value = raw.strip()
    try:
        datetime.date.fromisoformat(value)
    except ValueError as exc:
        raise _CellError(
            f"{header}: expected a date as YYYY-MM-DD, got {raw!r}."
        ) from exc
    # Hand the schema an ISO string; Pydantic coerces + range-validates it.
    return value


def _coerce_industry(header: str, raw: str) -> str:
    try:
        validated = validate_industry(raw)
    except ValueError as exc:
        raise _CellError(f"{header}: {exc}") from exc
    # validate_industry returns None for blank — but we only call it on a
    # non-empty cell, so a None here would be a logic error; guard anyway.
    if validated is None:  # pragma: no cover - blank handled by caller
        raise _CellError(f"{header}: empty industry.")
    return validated


# --- Stage 1: parse + map ----------------------------------------------------


def _display_name(core: dict) -> str:
    """Best-effort name for a row from its mapped core fields."""
    name = " ".join(
        part
        for part in (core.get("first_name"), core.get("last_name"))
        if part
    ).strip()
    if name:
        return name
    return core.get("byu_id") or core.get("net_id") or "(unnamed)"


def parse_and_map(file_bytes: bytes) -> tuple[list[dict], list[str]]:
    """Parse a CSV and map each data row to an ``AlumniCreateFull`` payload dict.

    Returns ``(rows, header_errors)``:

      * ``header_errors`` — a list of human messages for missing required
        headers and unknown extra headers. Non-empty means the file's columns
        don't match the template (``columns_ok`` is False downstream).
      * ``rows`` — one dict per data row, each:
            ``{"row": int, "name": str, "payload": dict,
               "spouse_byu_id": str|None, "error": str|None}``
        ``row`` is the 1-based spreadsheet row number (header = row 1, so the
        first data row is row 2). ``payload`` is shaped for AlumniCreateFull
        (core fields at top level, sections nested) using only non-empty cells.
        ``spouse_byu_id`` is the raw spouse BYU ID to resolve later (or None).
        ``error`` is set when a cell failed to coerce (bad date / number /
        industry), which marks the row rejected without building the model.

    The CSV is decoded as ``utf-8-sig`` so an Excel BOM is stripped.
    """
    text = file_bytes.decode("utf-8-sig")
    reader = csv.reader(io.StringIO(text))
    try:
        header_row = next(reader)
    except StopIteration:
        return [], ["The file is empty."]

    headers = [h.strip() for h in header_row]
    header_errors = _validate_headers(headers)
    if header_errors:
        # Columns are wrong; don't attempt to map rows against a bad header.
        return [], header_errors

    rows: list[dict] = []
    # csv row index: header consumed above is spreadsheet row 1, so data rows
    # start at spreadsheet row 2.
    for offset, raw_row in enumerate(reader, start=2):
        # Skip fully-blank lines (Excel often pads with empty trailing rows).
        if not any(cell.strip() for cell in raw_row):
            continue
        rows.append(_map_row(offset, headers, raw_row))
    return rows, header_errors


def _validate_headers(headers: list[str]) -> list[str]:
    """Compare the file's headers to the expected Alumni columns."""
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


def _map_row(row_num: int, headers: list[str], raw_row: list[str]) -> dict:
    """Map one CSV data row to a payload dict + metadata."""
    core: dict = {}
    sections: dict[str, dict] = {}
    spouse_byu_id: str | None = None
    error: str | None = None

    for col, header in enumerate(headers):
        target = _MAPPING.get(header)
        if target is None:
            continue
        raw = raw_row[col] if col < len(raw_row) else ""
        if raw is None or raw.strip() == "":
            continue  # omit empty cells entirely
        section, field, kind = target

        if kind == "spouse":
            # Captured for later DB resolution; not placed in the payload yet.
            spouse_byu_id = raw.strip()
            continue

        try:
            value = _coerce(kind, header, raw)
        except _CellError as exc:
            # First coercion error wins; the row is rejected but we keep
            # mapping the rest so the payload still carries a display name.
            if error is None:
                error = str(exc)
            continue

        if section == "core":
            core[field] = value
        else:
            sections.setdefault(section, {})[field] = value

    payload = dict(core)
    payload.update(sections)
    return {
        "row": row_num,
        "name": _display_name(core),
        "payload": payload,
        "spouse_byu_id": spouse_byu_id,
        "error": error,
    }


def _coerce(kind: str, header: str, raw: str):
    if kind == "str":
        return raw.strip()
    if kind == "int":
        return _coerce_int(header, raw)
    if kind == "date":
        return _coerce_date(header, raw)
    if kind == "bool":
        return _coerce_bool(header, raw)
    if kind == "industry":
        return _coerce_industry(header, raw)
    raise _CellError(f"{header}: unsupported column type.")  # pragma: no cover


# --- Stage 2: evaluate (dry-run report) --------------------------------------


async def _load_existing_index(session: AsyncSession) -> dict:
    """Batch-load existing non-archived identity keys ONCE.

    Returns sets used for O(1) duplicate checks in the per-row loop:
      * ``byu_ids``  — lowercased existing byu_id -> alumni_id
      * ``net_ids``  — lowercased existing net_id -> alumni_id
      * ``names``    — (lower first, lower last, grad_year) -> alumni_id

    Mapping to alumni_id (not just membership) lets the in-DB duplicate
    messages point at the conflicting record. Only the first match per key is
    kept (any collision blocks anyway).
    """
    stmt = select(
        Alumni.alumni_id,
        Alumni.byu_id,
        Alumni.net_id,
        Alumni.first_name,
        Alumni.last_name,
        Alumni.graduation_year,
    ).where(Alumni.archived.is_(False))
    result = await session.execute(stmt)

    byu_ids: dict[str, int] = {}
    net_ids: dict[str, int] = {}
    names: dict[tuple[str, str, int], int] = {}
    for alumni_id, byu_id, net_id, first, last, grad_year in result.all():
        if byu_id:
            byu_ids.setdefault(byu_id.strip().lower(), alumni_id)
        if net_id:
            net_ids.setdefault(net_id.strip().lower(), alumni_id)
        if first and last and grad_year is not None:
            names.setdefault(
                (first.strip().lower(), last.strip().lower(), grad_year),
                alumni_id,
            )
    return {"byu_ids": byu_ids, "net_ids": net_ids, "names": names}


async def _resolve_spouse(
    session: AsyncSession, spouse_byu_id: str
) -> int | None:
    """Resolve a raw spouse BYU ID to an existing non-archived alumni_id."""
    cleaned = "".join(ch for ch in spouse_byu_id if ch.isdigit())
    if not cleaned:
        return None
    return await session.scalar(
        select(Alumni.alumni_id).where(
            Alumni.byu_id == cleaned, Alumni.archived.is_(False)
        )
    )


async def evaluate(session: AsyncSession, rows: list[dict]) -> dict:
    """Build the dry-run import report for already-parsed *rows*.

    For each row: build ``AlumniCreateFull`` (ValidationError -> blocker), clean
    it, resolve the spouse link (unresolved -> warning, never a blocker), then
    run duplicate detection against BOTH the DB and the rows seen earlier in
    this same file, plus the recommended completeness warnings.

    In-file dedup: a byu_id / net_id already used by an earlier row is an exact
    (blocking) duplicate; a (first, last, grad_year) already used earlier is a
    fuzzy (warning) duplicate — matching the DB rules.

    Returns the exact preview-report shape the frontend consumes (see module
    docstring / task contract).
    """
    existing = await _load_existing_index(session)

    # In-file accumulators (lowercased), populated as we accept rows.
    seen_byu: dict[str, int] = {}
    seen_net: dict[str, int] = {}
    seen_names: dict[tuple[str, str, int], int] = {}

    out_rows: list[dict] = []
    importable = rejected = with_warnings = cleaned_count = 0

    for row in rows:
        report = await _evaluate_row(
            session, row, existing, seen_byu, seen_net, seen_names
        )
        out_rows.append(report)
        if report["status"] == "importable":
            importable += 1
        else:
            rejected += 1
        if report["warnings"]:
            with_warnings += 1
        if report["changes"]:
            cleaned_count += 1

    return {
        "columns_ok": True,
        "header_errors": [],
        "summary": {
            "total": len(rows),
            "importable": importable,
            "rejected": rejected,
            "with_warnings": with_warnings,
            "cleaned": cleaned_count,
        },
        "rows": out_rows,
    }


async def _evaluate_row(
    session: AsyncSession,
    row: dict,
    existing: dict,
    seen_byu: dict[str, int],
    seen_net: dict[str, int],
    seen_names: dict[tuple[str, str, int], int],
) -> dict:
    """Evaluate a single mapped row into a report entry."""
    base = {
        "row": row["row"],
        "name": row["name"],
        "status": "rejected",
        "changes": [],
        "warnings": [],
        "blockers": [],
        "error": None,
    }

    # A mapping-stage coercion error rejects the row before model building.
    if row["error"]:
        base["error"] = row["error"]
        return base

    # Build + validate the Pydantic model.
    try:
        model = AlumniCreateFull(**row["payload"])
    except ValidationError as exc:
        base["error"] = _format_validation_error(exc)
        base["blockers"] = [
            {
                "code": "validation_error",
                "field": None,
                "message": base["error"],
                "alumni_id": None,
            }
        ]
        return base

    cleaned, changes = hygiene.clean_alumni_payload(model)
    base["changes"] = changes

    warnings: list[dict] = []
    blockers: list[dict] = []

    # Spouse link resolution (warn-only; never blocks the row).
    if row["spouse_byu_id"]:
        spouse_id = await _resolve_spouse(session, row["spouse_byu_id"])
        if spouse_id is not None:
            cleaned["spouse_alumni_id"] = spouse_id
        else:
            warnings.append(
                {
                    "code": "spouse_not_found",
                    "message": (
                        f"Spouse BYU ID {row['spouse_byu_id']} did not match an "
                        "existing alumnus; the record was imported unlinked."
                    ),
                    "alumni_id": None,
                }
            )

    # Duplicate detection against the DB (reuses the hygiene query logic).
    db_blockers, db_warnings = await hygiene.detect_duplicates(session, cleaned)
    blockers.extend(db_blockers)
    warnings.extend(db_warnings)

    # Duplicate detection against earlier rows in THIS file (in-memory).
    byu = (cleaned.get("byu_id") or "").strip().lower()
    net = (cleaned.get("net_id") or "").strip().lower()
    first = (cleaned.get("first_name") or "").strip().lower()
    last = (cleaned.get("last_name") or "").strip().lower()
    grad = cleaned.get("graduation_year")

    if byu and byu in seen_byu:
        blockers.append(
            {
                "code": "duplicate_byu_id_in_file",
                "field": "byu_id",
                "message": (
                    f"BYU ID {cleaned['byu_id']} also appears earlier in this "
                    f"file (row {seen_byu[byu]})."
                ),
                "alumni_id": None,
            }
        )
    if net and net in seen_net:
        blockers.append(
            {
                "code": "duplicate_net_id_in_file",
                "field": "net_id",
                "message": (
                    f"Net ID {cleaned['net_id']} also appears earlier in this "
                    f"file (row {seen_net[net]})."
                ),
                "alumni_id": None,
            }
        )
    if first and last and grad is not None:
        key = (first, last, grad)
        if key in seen_names:
            warnings.append(
                {
                    "code": "possible_duplicate_in_file",
                    "message": (
                        f"Possible duplicate of row {seen_names[key]} in this "
                        f"file ({cleaned.get('first_name')} "
                        f"{cleaned.get('last_name')}, Class of {grad})."
                    ),
                    "alumni_id": None,
                }
            )

    # Recommended (soft) completeness warnings over the cleaned record.
    warnings.extend(hygiene.recommended_warnings(cleaned))

    base["warnings"] = warnings
    base["blockers"] = blockers
    status = "rejected" if blockers else "importable"
    base["status"] = status

    # Only an IMPORTABLE row contributes its identity keys to the in-file dedup
    # accumulators, so a rejected duplicate doesn't shadow later good rows.
    if status == "importable":
        if byu:
            seen_byu.setdefault(byu, row["row"])
        if net:
            seen_net.setdefault(net, row["row"])
        if first and last and grad is not None:
            seen_names.setdefault((first, last, grad), row["row"])

    return base


def _format_validation_error(exc: ValidationError) -> str:
    """Condense a Pydantic ValidationError into one human line."""
    parts: list[str] = []
    for err in exc.errors():
        loc = ".".join(str(p) for p in err.get("loc", ()) if p != "__root__")
        msg = err.get("msg", "invalid value")
        parts.append(f"{loc}: {msg}" if loc else msg)
    return "; ".join(parts) or "Invalid row."


# --- Stage 3: commit ---------------------------------------------------------


async def commit_import(
    session: AsyncSession,
    rows: list[dict],
    actor_user_id: int | None = None,
) -> dict:
    """Re-evaluate *rows* and insert every importable one in ONE transaction.

    Re-evaluating (rather than trusting a client-supplied preview) is the same
    defense-in-depth stance the single-record create path takes. Each importable
    row is created through ``alumni_service.create_alumni`` so cleaning + audit
    logging fire exactly as for a manual create — but committed together at the
    end. ``create_alumni`` commits internally; to keep a single transaction we
    temporarily neutralize its commit and run one real commit after the loop.

    Per-row exceptions are caught and recorded as rejects so one bad row never
    aborts the whole import. Returns:
        ``{"imported": int, "skipped": int, "created_ids": [int],
           "rejects": [{"row": int, "name": str, "reason": str}]}``
    """
    report = await evaluate(session, rows)
    # Index the parsed rows by row number so we can rebuild each payload.
    parsed_by_row = {r["row"]: r for r in rows}

    created_ids: list[int] = []
    rejects: list[dict] = []
    imported = 0
    skipped = 0

    # Suppress the per-record commit/refresh inside create_alumni so the whole
    # batch lands in one transaction; restore them afterward.
    real_commit = session.commit
    real_refresh = getattr(session, "refresh", None)

    async def _noop_commit() -> None:
        # Flush instead of commit: makes the row (and its generated id) visible
        # to later rows' duplicate queries within this transaction.
        await session.flush()

    async def _noop_refresh(_obj: object) -> None:
        return None

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
        try:
            model = AlumniCreateFull(**parsed["payload"])
            # Resolve the spouse link onto the model (evaluate proved it valid;
            # an unresolved id stays None, matching the warn-only contract).
            if parsed["spouse_byu_id"]:
                spouse_id = await _resolve_spouse(
                    session, parsed["spouse_byu_id"]
                )
                if spouse_id is not None:
                    model = model.model_copy(
                        update={"spouse_alumni_id": spouse_id}
                    )

            session.commit = _noop_commit  # type: ignore[method-assign]
            if real_refresh is not None:
                session.refresh = _noop_refresh  # type: ignore[method-assign]
            created = await alumni_service.create_alumni(
                session, model, actor_user_id=actor_user_id
            )
            created_ids.append(created.alumni_id)
            imported += 1
        except Exception as exc:  # noqa: BLE001 - record + continue per row
            skipped += 1
            rejects.append(
                {
                    "row": row_num,
                    "name": evaluated["name"],
                    "reason": str(exc) or exc.__class__.__name__,
                }
            )
        finally:
            session.commit = real_commit  # type: ignore[method-assign]
            if real_refresh is not None:
                session.refresh = real_refresh  # type: ignore[method-assign]

    # One real commit for the whole importable batch.
    if imported:
        await session.commit()

    return {
        "imported": imported,
        "skipped": skipped,
        "created_ids": created_ids,
        "rejects": rejects,
    }


def _reject_reason(evaluated: dict) -> str:
    """Human reason a row was skipped (error, else first blocker message)."""
    if evaluated.get("error"):
        return evaluated["error"]
    blockers = evaluated.get("blockers") or []
    if blockers:
        return blockers[0]["message"]
    return "Rejected."


# --- CSV template ------------------------------------------------------------


def build_template_csv() -> str:
    """Return the import template as CSV text: the exact Alumni headers plus one
    example row. Derived from the same column source as the xlsx template."""
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(EXPECTED_HEADERS)
    writer.writerow(EXAMPLE_ROW)
    return buffer.getvalue()
