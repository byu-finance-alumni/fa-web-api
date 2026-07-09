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
import logging
import re

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.dropdowns import validate_industry
from app.core.errors import ConflictError, NotFoundError
from app.models.alumni import Alumni
from app.schemas.alumni import AlumniCreateFull
from app.services import alumni as alumni_service
from app.services import hygiene
from scripts.export_intake_template import _ALUMNI_COLUMNS, _FRIEND_COLUMNS

log = logging.getLogger(__name__)

# Upload guards (also enforced at the route layer for the byte cap). The row cap
# is enforced here in parse_and_map so both /preview and /import share it.
# 4 MiB, deliberately BELOW Vercel's ~4.5 MB serverless Function request-body
# ceiling so the app's own friendly 413 fires instead of a raw platform error.
MAX_UPLOAD_BYTES = 4 * 1024 * 1024  # 4 MiB
MAX_IMPORT_ROWS = 2000

# --- Column mapping ----------------------------------------------------------
#
# Each Alumni-sheet header maps to a (section, field) target in the
# AlumniCreateFull payload. ``section`` is "core" for top-level fields or one of
# the nested-section keys (contact/career/education/former/leadership/engagement).
# ``kind`` selects the value coercion applied to the raw CSV cell.
#
#   "str"      -> trimmed string (empty -> omitted)
#   "int"      -> parsed integer (graduation_year, degree_year, ...)
#   "date"     -> parsed YYYY-MM-DD date (kept as ISO string for the schema)
#   "bool"     -> Yes/No/true/1 -> bool
#   "industry" -> validated against the controlled vocab (invalid -> row error)
#
# Section targets: core -> alumni; contact -> alumni_contact_info; career ->
# current_employment (employer/title/industry only — the single address lives on
# the CONTACT record, so "Current city/state/ZIP/country" map to contact.*, NOT
# career, and drive the map); education -> education_history; former ->
# employment_history (is_current=false); leadership -> finance_society_leadership;
# engagement -> alumni_program_engagement.
#
# Keys are the EXACT header text from the finalized 64-column intake template, in
# that order, so a drift in the template surfaces as a header error here.

_MAPPING: dict[str, tuple[str, str, str]] = {
    "Filled out Survey": ("core", "survey_completed_date", "date"),
    "MSTID (from OneAccord)": ("core", "mst_id", "str"),
    "BYU ID (9 digits)": ("core", "byu_id", "str"),
    "Net ID": ("core", "net_id", "str"),
    "Preferred first name": ("core", "preferred_first_name", "str"),
    "First name": ("core", "first_name", "str"),
    "Middle name": ("core", "middle_name", "str"),
    "Last Name": ("core", "last_name", "str"),
    "Gender": ("core", "gender", "str"),
    "Personal Email": ("contact", "personal_email", "str"),
    "Birthday (YYYY-MM-DD)": ("core", "birth_date", "date"),
    "Graduation Semester": ("core", "graduation_semester", "str"),
    "Graduation Year": ("core", "graduation_year", "int"),
    "Class of": ("core", "graduation_class", "int"),
    "LinkedIn URL": ("core", "linkedin_url", "str"),
    "Finance program admitted year": ("core", "finance_program_year", "int"),
    "Employment Status": ("core", "employment_status", "str"),
    "Profile Updated By": ("core", "profile_updated_by", "str"),
    "Profile Updated Date": ("core", "profile_updated_date", "date"),
    "Finance Leadership Position": ("leadership", "leadership_role", "str"),
    "Graduate degree": ("core", "graduate_degree", "str"),
    "Deceased? (Yes/No)": ("core", "deceased", "bool"),
    "Notes": ("core", "notes", "str"),
    "Citizenship": ("core", "citizenship", "str"),
    "Marital Status": ("core", "marital_status", "str"),
    "Spouse First Name": ("core", "spouse_first_name", "str"),
    "Spouse Last Name": ("core", "spouse_last_name", "str"),
    "Phone #": ("contact", "phone", "str"),
    "Current employer": ("career", "current_employer", "str"),
    "Current title": ("career", "current_title", "str"),
    "Current industry (see Reference sheet)": (
        "career",
        "current_industry",
        "industry",
    ),
    # Secondary industry is FREE TEXT (open response), not the controlled vocab —
    # so it maps as a plain string; placeholders are blanked (see below).
    "Secondary industry (see Reference sheet)": (
        "career",
        "current_industry_secondary",
        "str",
    ),
    "Work Email": ("contact", "work_email", "str"),
    "Address line 1": ("contact", "address_line_1", "str"),
    "Address line 2": ("contact", "address_line_2", "str"),
    # The single address lives on the CONTACT record and drives the map, so the
    # "Current city/state/ZIP/country" headers bind to contact.*, NOT career.
    "Current city": ("contact", "city", "str"),
    "Current state": ("contact", "state", "str"),
    "Region (Northeast, Southeast, Midwest, Southwest, and West)": (
        "contact",
        "region",
        "str",
    ),
    "Current country": ("contact", "country", "str"),
    "Current ZIP": ("contact", "zip", "str"),
    "Home country": ("core", "home_country", "str"),
    "Degree": ("education", "degree", "str"),
    "Major": ("education", "major", "str"),
    "Degree status": ("education", "degree_status", "str"),
    "Degree year": ("education", "degree_year", "int"),
    "Former Company": ("former", "employer_name", "str"),
    "Former Title": ("former", "employment_title", "str"),
    "Former Industry": ("former", "employment_industry", "str"),
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
    "Willing to host case competition (yes/no)": (
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
    "Willing to be a PIFF donor (Yes/No)": ("engagement", "piff_donor", "bool"),
    "CFP designation (Yes/No)": ("engagement", "cfp_designation", "bool"),
    "CFA designation (Yes/No)": ("engagement", "cfa_designation", "bool"),
    "Other Designations:": ("core", "other_designations", "str"),
    "Engagement notes": ("engagement", "engagement_notes", "str"),
    "Best Contact": ("contact", "best_contact", "str"),
}

# Ordered list of expected headers — same source + order as the xlsx Alumni
# sheet (the template generator's _ALUMNI_COLUMNS). Used for header validation
# and to build the downloadable CSV template.
EXPECTED_HEADERS: list[str] = [header for header, _ in _ALUMNI_COLUMNS]
EXAMPLE_ROW: list[str] = [example for _, example in _ALUMNI_COLUMNS]

# --- Friend (non-alumni contact) column set (#294) ---------------------------
#
# Friends are imported through this SAME pipeline; a friend row is just an alumni
# row with ``is_alumni = False`` injected. The friend template is the curated
# subset of alumni columns declared in the intake template (identity by name; no
# academic / spouse-link fields). Its mapping is the matching slice of _MAPPING,
# so friend headers bind to the exact same model columns — no parallel mapping to
# drift.
FRIEND_EXPECTED_HEADERS: list[str] = [header for header, _ in _FRIEND_COLUMNS]
FRIEND_EXAMPLE_ROW: list[str] = [example for _, example in _FRIEND_COLUMNS]
_FRIEND_MAPPING: dict[str, tuple[str, str, str]] = {
    header: _MAPPING[header] for header in FRIEND_EXPECTED_HEADERS
}


def _expected_headers(friend: bool) -> list[str]:
    return FRIEND_EXPECTED_HEADERS if friend else EXPECTED_HEADERS


def _mapping_for(friend: bool) -> dict[str, tuple[str, str, str]]:
    return _FRIEND_MAPPING if friend else _MAPPING

_TRUE_TOKENS = frozenset({"yes", "true", "1", "y", "t"})
# Empty cells are skipped by _map_row before coercion, so "" is intentionally
# NOT a token here — a blank bool cell never reaches _coerce_bool.
_FALSE_TOKENS = frozenset({"no", "false", "0", "n", "f"})


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


# Date input forms accepted by the importer, tried in order and all normalized
# to ISO (YYYY-MM-DD) for the schema. Zero-padded ISO is handled by the fast
# path below; these cover the messy real-world spreadsheet forms. Named-month
# formats (%b/%B) are unambiguous about day/month order. For the purely-numeric
# slash/dash forms we assume US month/day/year ordering, matching the intake
# spreadsheets. Two-digit years pivot per POSIX %y (00-68 -> 2000-2068,
# 69-99 -> 1969-1999).
_DATE_INPUT_FORMATS = (
    "%Y-%m-%d",    # 2002-3-3   (unpadded ISO)
    "%Y/%m/%d",    # 2002/03/03
    "%d-%b-%Y",    # 3-Mar-2002
    "%d-%b-%y",    # 3-Mar-02
    "%d %b %Y",    # 3 Mar 2002
    "%d %b %y",    # 3 Mar 02
    "%d-%B-%Y",    # 3-March-2002
    "%d-%B-%y",    # 3-March-02
    "%d %B %Y",    # 3 March 2002
    "%b %d, %Y",   # Mar 3, 2002
    "%B %d, %Y",   # March 3, 2002
    "%b %d %Y",    # Mar 3 2002
    "%m/%d/%Y",    # 03/15/1990 (US month/day/year)
    "%m/%d/%y",    # 03/15/90
    "%m-%d-%Y",    # 03-15-1990
    "%m-%d-%y",    # 03-15-90
)


def _coerce_date(header: str, raw: str) -> str:
    """Normalize a spreadsheet date cell to an ISO (YYYY-MM-DD) string.

    Accepts ISO plus common real-world forms (e.g. ``3-Mar-02``, ``3 Mar 2002``,
    ``03/15/1990``). Hands the schema an ISO string; Pydantic then coerces +
    range-validates it.
    """
    value = raw.strip()
    # Fast path: already zero-padded ISO.
    try:
        return datetime.date.fromisoformat(value).isoformat()
    except ValueError:
        pass
    for fmt in _DATE_INPUT_FORMATS:
        try:
            parsed = datetime.datetime.strptime(value, fmt).date()
        except ValueError:
            continue
        # Guard against %Y greedily reading a 2-digit number as year 1-99
        # (e.g. "01-02-03" must not become year 1). Every date this importer
        # handles is a modern people-date, so anything outside this window is a
        # misparse — fall through to the next candidate format.
        if 1900 <= parsed.year <= 2100:
            return parsed.isoformat()
    raise _CellError(
        f"{header}: unrecognized date {raw!r}. Use YYYY-MM-DD (e.g. 2002-03-03) "
        f"or a common form like 3-Mar-2002, 3 Mar 2002, or 03/15/1990."
    )


# Tokens real intake sheets use to mean "not known". Import-only leniency (the
# manual create/edit form's PRIMARY industry stays strict): in the primary
# INDUSTRY cell we map them to the catch-all "Other"; in the free-text fields
# below we blank them out (see the map loop) rather than storing the literal.
_PLACEHOLDER_TOKENS = frozenset({"unknown", "n/a", "na"})

# Free-text fields where a placeholder token means "leave blank": the address/
# location columns plus the open-response secondary industry.
_PLACEHOLDER_BLANK_FIELDS = frozenset(
    {
        "address_line_1",
        "address_line_2",
        "city",
        "state",
        "region",
        "country",
        "zip",
        "current_industry_secondary",
    }
)


def _coerce_industry(header: str, raw: str) -> str:
    if raw.strip().lower() in _PLACEHOLDER_TOKENS:
        return "Other"
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


def parse_and_map(
    file_bytes: bytes,
    max_rows: int | None = MAX_IMPORT_ROWS,
    *,
    friend: bool = False,
) -> tuple[list[dict], list[str]]:
    """Parse a CSV and map each data row to an ``AlumniCreateFull`` payload dict.

    Returns ``(rows, header_errors)``:

      * ``header_errors`` — a list of human messages for missing required
        headers and unknown extra headers. Non-empty means the file's columns
        don't match the template (``columns_ok`` is False downstream). A
        non-decodable file or a file exceeding ``max_rows`` is reported here too
        (as a single header-style error) and ``rows`` is empty.
      * ``rows`` — one dict per data row, each:
            ``{"row": int, "name": str, "payload": dict,
               "spouse_byu_id": str|None, "error": str|None}``
        ``row`` is the 1-based spreadsheet row number (header = row 1, so the
        first data row is row 2). ``payload`` is shaped for AlumniCreateFull
        (core fields at top level, sections nested) using only non-empty cells.
        ``spouse_byu_id`` is the raw spouse BYU ID to resolve later (or None).
        ``error`` is set when a cell failed to coerce (bad date / number /
        industry), which marks the row rejected without building the model.

    ``max_rows`` caps the number of data rows processed (``None`` disables the
    cap); over the cap, a header-style error is returned instead of processing,
    so a hostile/huge file can't exhaust memory or DB time.

    ``friend=True`` maps against the curated FRIEND column set (non-alumni
    contacts, #294) and stamps ``is_alumni = False`` onto every row's payload.

    The CSV is decoded as ``utf-8-sig`` so an Excel BOM is stripped.
    """
    expected = _expected_headers(friend)
    mapping = _mapping_for(friend)
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
    header_errors = _validate_headers(headers, expected)
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
        if max_rows is not None and len(rows) >= max_rows:
            return [], [
                f"File exceeds the {max_rows:,}-row import limit. Split into "
                "smaller batches."
            ]
        rows.append(_map_row(offset, headers, raw_row, mapping, friend))
    return rows, header_errors


def _validate_headers(headers: list[str], expected_headers: list[str]) -> list[str]:
    """Compare the file's headers to the expected columns (alumni or friend)."""
    errors: list[str] = []
    seen = set(headers)
    expected = set(expected_headers)
    # Reject duplicated column names (last-wins would otherwise silently drop
    # the earlier column's data). Mirrors import_events / import_donations.
    for dup in sorted({h for h in headers if h and headers.count(h) > 1}):
        errors.append(f"Duplicate column: {dup!r}.")
    for missing in expected_headers:
        if missing not in seen:
            errors.append(f"Missing required column: {missing!r}.")
    for extra in headers:
        if extra and extra not in expected:
            errors.append(f"Unexpected column: {extra!r}.")
    # Wrong-delimiter hint: a single "column" carrying several ';'/tab separators
    # is almost always a semicolon/tab-delimited file fed to a comma parser.
    if len(headers) == 1 and (
        headers[0].count(";") >= 2 or headers[0].count("\t") >= 2
    ):
        errors.append(
            "This looks like a semicolon- or tab-delimited file. Re-save it as "
            "a comma-delimited CSV and try again."
        )
    return errors


def _map_row(
    row_num: int,
    headers: list[str],
    raw_row: list[str],
    mapping: dict[str, tuple[str, str, str]],
    friend: bool = False,
) -> dict:
    """Map one CSV data row to a payload dict + metadata.

    ``mapping`` selects the header→target set (alumni or friend). ``friend=True``
    stamps ``is_alumni = False`` onto the row's core payload so the shared create
    path persists a non-alumni contact."""
    core: dict = {}
    sections: dict[str, dict] = {}
    spouse_byu_id: str | None = None
    error: str | None = None

    for col, header in enumerate(headers):
        target = mapping.get(header)
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

        # A free-text location / secondary-industry cell filled with a
        # placeholder ("unknown", "n/a") isn't known — store blank, not the
        # literal token.
        if (
            field in _PLACEHOLDER_BLANK_FIELDS
            and raw.strip().lower() in _PLACEHOLDER_TOKENS
        ):
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

    if friend:
        # Non-alumni contact: force is_alumni False so the shared create path
        # writes a friend record instead of an alumnus (#294).
        core["is_alumni"] = False

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


_IndexRec = tuple[int, "str | None", "str | None"]


async def _load_existing_index(session: AsyncSession) -> dict:
    """Batch-load existing identity keys ONCE for in-memory duplicate detection.

    Loads BOTH active and archived rows in a SINGLE query so the per-row loop can
    reproduce :func:`hygiene.detect_duplicates` (exact byu_id/net_id blockers,
    archived "ghost" warnings, fuzzy name warnings) and the spouse-link lookup with
    ZERO further DB round trips — the whole ``evaluate`` pass costs one query, not
    ~6 per row. The authoritative duplicate check still fires at write time in
    ``create_alumni`` (DB partial-unique index), so this stays a fast pre-filter.

    Each value is ``(alumni_id, first, last)`` so duplicate messages can name the
    conflicting record:
      * ``active_byu`` / ``archived_byu``  — DIGIT-STRIPPED byu_id -> record
      * ``active_net`` / ``archived_net``  — lowercased net_id -> record
      * ``active_names`` — (lower first, lower last, grad_year) -> LIST of records
        (a fuzzy key can match several; each becomes its own warning)
    ``active_byu`` also backs the spouse-link lookup (active only), matching the
    old per-row ``_resolve_spouse`` query.
    """
    stmt = select(
        Alumni.alumni_id,
        Alumni.byu_id,
        Alumni.net_id,
        Alumni.first_name,
        Alumni.last_name,
        Alumni.graduation_year,
        Alumni.archived,
    )
    result = await session.execute(stmt)

    active_byu: dict[str, _IndexRec] = {}
    active_net: dict[str, _IndexRec] = {}
    archived_byu: dict[str, _IndexRec] = {}
    archived_net: dict[str, _IndexRec] = {}
    active_names: dict[tuple[str, str, int], list[_IndexRec]] = {}
    for alumni_id, byu_id, net_id, first, last, grad_year, archived in result.all():
        rec: _IndexRec = (alumni_id, first, last)
        # Digit-strip byu_id to match the cleaner (stores digits-only) so a stored
        # formatted id still collides with an incoming one.
        byu_key = re.sub(r"\D", "", byu_id.strip()) if byu_id else ""
        net_key = net_id.strip().lower() if net_id else ""
        if archived:
            if byu_key:
                archived_byu.setdefault(byu_key, rec)
            if net_key:
                archived_net.setdefault(net_key, rec)
            continue
        if byu_key:
            active_byu.setdefault(byu_key, rec)
        if net_key:
            active_net.setdefault(net_key, rec)
        if first and last and grad_year is not None:
            active_names.setdefault(
                (first.strip().lower(), last.strip().lower(), grad_year), []
            ).append(rec)
    return {
        "active_byu": active_byu,
        "active_net": active_net,
        "archived_byu": archived_byu,
        "archived_net": archived_net,
        "active_names": active_names,
    }


def _detect_duplicates_indexed(
    cleaned: dict, existing: dict
) -> tuple[list[dict], list[dict]]:
    """In-memory twin of :func:`hygiene.detect_duplicates` for bulk import.

    Reproduces the exact blocker/warning codes and messages using the once-loaded
    ``existing`` index (no per-row DB queries). Kept byte-for-byte aligned with the
    live query version so preview and single-record create report identically.
    """
    blockers: list[dict] = []
    blocker_ids: set[int] = set()
    warnings: list[dict] = []

    byu_id = cleaned.get("byu_id")
    if byu_id:
        key = re.sub(r"\D", "", str(byu_id).strip())
        match = existing["active_byu"].get(key) if key else None
        if match is not None:
            aid, first, last = match
            blockers.append(
                {
                    "code": "duplicate_byu_id",
                    "field": "byu_id",
                    "message": (
                        f"BYU ID {byu_id} already belongs to "
                        f"{hygiene._display_name(first, last)}."
                    ),
                    "alumni_id": aid,
                }
            )
            blocker_ids.add(aid)
        else:
            ghost = existing["archived_byu"].get(key) if key else None
            if ghost is not None:
                aid, first, last = ghost
                warnings.append(
                    {
                        "code": "duplicate_archived",
                        "message": (
                            f"BYU ID {byu_id} matches an archived record for "
                            f"{hygiene._display_name(first, last)}"
                            " — restoring it would create a duplicate."
                        ),
                        "alumni_id": aid,
                    }
                )

    net_id = cleaned.get("net_id")
    if net_id:
        key = str(net_id).strip().lower()
        match = existing["active_net"].get(key) if key else None
        if match is not None:
            aid, first, last = match
            blockers.append(
                {
                    "code": "duplicate_net_id",
                    "field": "net_id",
                    "message": (
                        f"Net ID {net_id} already belongs to "
                        f"{hygiene._display_name(first, last)}."
                    ),
                    "alumni_id": aid,
                }
            )
            blocker_ids.add(aid)
        else:
            ghost = existing["archived_net"].get(key) if key else None
            if ghost is not None:
                aid, first, last = ghost
                warnings.append(
                    {
                        "code": "duplicate_archived",
                        "message": (
                            f"Net ID {net_id} matches an archived record for "
                            f"{hygiene._display_name(first, last)}"
                            " — restoring it would create a duplicate."
                        ),
                        "alumni_id": aid,
                    }
                )

    first = cleaned.get("first_name")
    last = cleaned.get("last_name")
    grad_year = cleaned.get("graduation_year")
    if first and last and grad_year is not None:
        key = (first.lower(), last.lower(), grad_year)
        for aid, mfirst, mlast in existing["active_names"].get(key, []):
            if aid in blocker_ids:
                continue  # already a hard blocker; don't double-report
            warnings.append(
                {
                    "code": "possible_duplicate",
                    "message": (
                        "Possible duplicate of "
                        f"{hygiene._display_name(mfirst, mlast)} "
                        f"(Class of {grad_year})."
                    ),
                    "alumni_id": aid,
                }
            )
    return blockers, warnings


def _spouse_from_index(spouse_byu_id: str, existing: dict) -> int | None:
    """Resolve a raw spouse BYU ID to an ACTIVE alumni_id via the preloaded index."""
    key = "".join(ch for ch in spouse_byu_id if ch.isdigit())
    if not key:
        return None
    match = existing["active_byu"].get(key)
    return match[0] if match else None


async def _resolve_spouse(
    session: AsyncSession, spouse_byu_id: str
) -> int | None:
    """Resolve a raw spouse BYU ID to an existing non-archived alumni_id.

    Still used by ``commit_import`` (per importable row, at write time). The
    ``evaluate`` preview path uses :func:`_spouse_from_index` instead to stay
    query-free.
    """
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

    NOTE: this is a PRE-FILTER / gate, not the last word. The authoritative
    duplicate check fires inside ``create_alumni`` at flush time (backed by the
    DB partial-unique index on byu_id/net_id), so ``commit_import`` re-runs this
    AND relies on the write-time check as defense in depth.
    """
    existing = await _load_existing_index(session)

    # In-file accumulators (lowercased), populated as we accept rows.
    seen_byu: dict[str, int] = {}
    seen_net: dict[str, int] = {}
    seen_names: dict[tuple[str, str, int], int] = {}

    out_rows: list[dict] = []
    importable = rejected = with_warnings = cleaned_count = 0

    for row in rows:
        report = _evaluate_row(
            row, existing, seen_byu, seen_net, seen_names
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


def _evaluate_row(
    row: dict,
    existing: dict,
    seen_byu: dict[str, int],
    seen_net: dict[str, int],
    seen_names: dict[tuple[str, str, int], int],
) -> dict:
    """Evaluate a single mapped row into a report entry.

    Pure/query-free: all duplicate and spouse-link resolution runs against the
    once-loaded ``existing`` index (see :func:`_load_existing_index`)."""
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
        spouse_id = _spouse_from_index(row["spouse_byu_id"], existing)
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

    # Duplicate detection against the DB, done in-memory via the preloaded index
    # (byte-identical to hygiene.detect_duplicates, but zero per-row queries).
    db_blockers, db_warnings = _detect_duplicates_indexed(cleaned, existing)
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
    defense-in-depth stance the single-record create path takes. ``evaluate()``
    here is only a PRE-FILTER / gate: the AUTHORITATIVE duplicate check fires
    inside ``create_alumni`` at flush time (and ultimately the DB unique index),
    so a row that slips past the gate is still rejected at write time. The spouse
    link is re-resolved here on purpose — spouse warnings are produced only by
    the preview step, so the commit path resolves the id fresh and silently
    imports unlinked if it no longer matches (warn-only contract).

    Each importable row is created through ``alumni_service.create_alumni`` so
    cleaning + audit logging fire exactly as for a manual create. ``create_alumni``
    commits internally; to keep a single batch transaction we temporarily
    neutralize its commit (flush instead) and run one real commit after the loop.

    Crash-safety: each row's insert runs inside its OWN SAVEPOINT
    (``begin_nested``). If a row raises mid-batch, only that savepoint rolls back
    — the outer transaction stays valid so later rows still commit. ``imported``
    / ``created_ids`` therefore count ONLY rows that actually flushed cleanly;
    the final ``commit`` persists exactly those. Returns:
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
        # Per-row SAVEPOINT: a failure rolls back ONLY this row, leaving the
        # outer transaction (and the already-inserted rows) intact.
        async with session.begin_nested() as savepoint:
            try:
                model = AlumniCreateFull(**parsed["payload"])
                # Re-resolve the spouse link onto the model (warn-only: an
                # unresolved id stays None, importing the record unlinked).
                if parsed["spouse_byu_id"]:
                    spouse_id = await _resolve_spouse(
                        session, parsed["spouse_byu_id"]
                    )
                    if spouse_id is not None:
                        model = model.model_copy(
                            update={"spouse_alumni_id": spouse_id}
                        )

                # Keep the commit/refresh no-op swap INSIDE the savepoint so the
                # create flushes (not commits) within this nested transaction;
                # restore in finally so the outer commit/refresh are unaffected.
                session.commit = _noop_commit  # type: ignore[method-assign]
                if real_refresh is not None:
                    session.refresh = _noop_refresh  # type: ignore[method-assign]
                try:
                    created = await alumni_service.create_alumni(
                        session, model, actor_user_id=actor_user_id
                    )
                finally:
                    session.commit = real_commit  # type: ignore[method-assign]
                    if real_refresh is not None:
                        session.refresh = real_refresh  # type: ignore[method-assign]
                created_ids.append(created.alumni_id)
                imported += 1
            except Exception as exc:  # noqa: BLE001 - record + continue per row
                await savepoint.rollback()
                skipped += 1
                rejects.append(
                    {
                        "row": row_num,
                        "name": evaluated["name"],
                        "reason": _classify_reject(exc, row_num),
                    }
                )

    # One real commit for the whole importable batch.
    if imported:
        await session.commit()

    return {
        "imported": imported,
        "skipped": skipped,
        "created_ids": created_ids,
        "rejects": rejects,
    }


def _classify_reject(exc: Exception, row_num: int) -> str:
    """Turn a per-row insert failure into a SAFE reject reason.

    Domain errors (``ConflictError`` / ``NotFoundError`` / pydantic
    ``ValidationError``) carry client-safe messages and are surfaced verbatim.
    Anything else (DB driver errors, etc.) may leak internal/SQL detail, so it is
    logged server-side and reported as a generic, class-only message.
    """
    if isinstance(exc, (ConflictError, NotFoundError)):
        return str(exc) or exc.__class__.__name__
    if isinstance(exc, ValidationError):
        return _format_validation_error(exc)
    log.exception("Unexpected error importing row %s", row_num)
    return f"Unexpected error ({exc.__class__.__name__})"


def _reject_reason(evaluated: dict) -> str:
    """Human reason a row was skipped (error, else first blocker message)."""
    if evaluated.get("error"):
        return evaluated["error"]
    blockers = evaluated.get("blockers") or []
    if blockers:
        return blockers[0]["message"]
    return "Rejected."


# --- CSV template ------------------------------------------------------------


def build_template_csv(friend: bool = False) -> str:
    """Return the import template as CSV text: the exact headers plus one example
    row. Derived from the same column source as the xlsx template. ``friend=True``
    returns the curated friend (non-alumni contact) column set (#294)."""
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    if friend:
        writer.writerow(FRIEND_EXPECTED_HEADERS)
        writer.writerow(FRIEND_EXAMPLE_ROW)
    else:
        writer.writerow(EXPECTED_HEADERS)
        writer.writerow(EXAMPLE_ROW)
    return buffer.getvalue()
