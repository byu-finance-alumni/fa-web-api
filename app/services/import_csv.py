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
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.dropdowns import (
    holds_designation,
    normalize_designation,
    validate_industry,
)
from app.core.errors import ConflictError, NotFoundError
from app.models.alumni import Alumni
from app.models.audit import AuditLog
from app.models.contact import AlumniContactInfo
from app.models.employment import CurrentEmployment, EducationHistory
from app.models.engagement import AlumniProgramEngagement
from app.repositories.alumni import build_alumni_query
from app.schemas.alumni import (
    AlumniCreateFull,
    AlumniUpdateFull,
    CareerCreate,
    ContactCreate,
    EducationCreate,
    EngagementCreate,
)
from app.services import alumni as alumni_service
from app.services import alumni_export, hygiene
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
#   "designation" -> CFA/CFP marker string; a NEGATIVE cell ("No", "N/A", ...)
#                    imports as blank instead of being stored verbatim
#
# Section targets: core -> alumni; contact -> alumni_contact_info; career ->
# current_employment (employer/title/industry AND the work location — see below);
# education -> education_history; former -> employment_history (is_current=false);
# leadership -> finance_society_leadership; engagement ->
# alumni_program_engagement.
#
# The sheet's location block is the EMPLOYER's address (#287). The sheet's own
# column order says so — it sits immediately after Current employer / title /
# industry / Work Email, and Tanya fills it in with where the alum WORKS. So
# "Current city/state/ZIP/country" bind to career.current_* (current_employment),
# NOT to contact.* (the residence row). Nothing in this system populates a
# residence: the concept exists in the schema, but no sheet column feeds it.
# ("Home country" is NOT part of this block — it's the country of ORIGIN, about
# the alum, and stays on core.home_country.)
#
# Keys are the EXACT header text from the finalized 66-column intake template, in
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
    "Graduate university": ("core", "graduate_school", "str"),
    "Graduate graduation year": ("core", "graduate_graduation_year", "int"),
    "Deceased? (Yes/No)": ("core", "deceased", "bool"),
    "Notes": ("core", "notes", "str"),
    "Citizenship": ("core", "citizenship", "str"),
    "Marital Status": ("core", "marital_status", "str"),
    "Languages": ("core", "languages", "str"),
    # One combined column on the sheet (99% of intake data has the full name in
    # one field). Split into spouse_first_name/spouse_last_name in _map_row; the
    # data stays separate everywhere else (Option A). kind "spouse_name" drives
    # both the split on import and the join on the cohort round-trip export.
    "Spouse Name": ("core", "spouse_first_name", "spouse_name"),
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
    # UNRESOLVED (#287): these two are the employer's street, but they are the one
    # column pair whose destination is still being decided, so they stay bound to
    # contact.address_line_1/_2 exactly as before. Do not move or drop them until
    # that call is made — the rest of the block moved to career.* below.
    "Address line 1": ("contact", "address_line_1", "str"),
    "Address line 2": ("contact", "address_line_2", "str"),
    # Residence city/state -> the actual contact address columns (distinct from
    # the employer "Current city/state" below, which are career.current_*).
    "Residence city": ("contact", "city", "str"),
    "Residence state": ("contact", "state", "str"),
    # The location block is the EMPLOYER's (#287) -> career.current_*, not contact.
    "Current city": ("career", "current_city", "str"),
    "Current state": ("career", "current_state", "str"),
    # Region is NOT an address — it's a US bucket DERIVED from the work state
    # (#283, see hygiene.derive_region). It physically lives on the contact row
    # and stays there; only the address columns above moved.
    "Region (Northeast, Southeast, Midwest, Southwest, West, and Mountain West)": (
        "contact",
        "region",
        "str",
    ),
    "Current country": ("career", "current_country", "str"),
    "Current ZIP": ("career", "current_zip", "str"),
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
    # These two columns are literally headed "(Yes/No)" but the underlying
    # columns are marker-string-or-NULL, not booleans — hence the dedicated
    # "designation" kind rather than "str" (see _map_row / _coerce). The sheet has
    # NO column for cpa_designation today; that is a separate open decision.
    "CFP designation (Yes/No)": ("engagement", "cfp_designation", "designation"),
    "CFA designation (Yes/No)": ("engagement", "cfa_designation", "designation"),
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


# --- Legacy header aliases ---------------------------------------------------
#
# Header validation is exact-match both ways, so RENAMING a template column would
# hard-reject every sheet already filled in under the old name (the old header
# reads as "Unexpected column", the new one as "Missing required column"). Staff
# work off copies of the template that were downloaded months ago, so a rename
# must not invalidate work already in progress.
#
# Each retired header therefore maps to its current name and is canonicalized on
# read, BEFORE validation and mapping. This is deliberately a rename-only table:
# both spellings mean the identical field, so there is no parallel mapping to
# drift and _MAPPING stays keyed solely by the current template headers.
#
#   "Region (…, and West)" -> "Region (…, West, and Mountain West)"
#       The Region header enumerates the valid regions, so adding the 6th region
#       (Mountain West, 2026-07-16) forced the header text to change.
_LEGACY_HEADER_ALIASES: dict[str, str] = {
    "Region (Northeast, Southeast, Midwest, Southwest, and West)": (
        "Region (Northeast, Southeast, Midwest, Southwest, West, and Mountain West)"
    ),
}


def _canonicalize_header(header: str) -> str:
    """Map a retired header spelling to its current one (others pass through)."""
    if header in _LEGACY_HEADER_ALIASES:
        return _LEGACY_HEADER_ALIASES[header]
    # The Women-in-Finance header contains an em-dash, which Excel/encoding
    # routinely mangles into a hyphen, en-dash, or mojibake ("â€"") — the #1
    # cause of a spurious "columns don't match" on this template. Normalize ANY
    # dash variant back to the canonical spelling.
    if header.startswith("Willing to mentor") and "Women in Finance" in header:
        return "Willing to mentor — Women in Finance (Yes/No)"
    return header


def _decode_upload(data: bytes) -> str | None:
    """Decode uploaded CSV bytes to text, tolerant of how Excel actually saves.

    Tries UTF-8 (with/without BOM) first — the recommended format — then falls
    back to Windows-1252 (Excel's plain "CSV" / ANSI default on Windows) and
    finally Latin-1 (which decodes any byte). So an ANSI export containing an
    em-dash or an accented name imports without the user re-saving as UTF-8.
    """
    for encoding in ("utf-8-sig", "cp1252", "latin-1"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return None  # unreachable — latin-1 never raises — kept for safety


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

# Marital status is free-text (not a validated vocab), and intake sheets often
# carry "Undeclared"/"N/A"/"None" for students who didn't answer. Those all mean
# "not provided", so they import as blank rather than a literal value. Kept
# separate from _PLACEHOLDER_TOKENS so "Undeclared"/"None" only blank marital
# status — they must NOT reclassify an industry cell (see _coerce_industry).
_MARITAL_BLANK_TOKENS = frozenset({"undeclared", "n/a", "na", "none", "unknown"})

# Free-text fields where a placeholder token means "leave blank": the address/
# location columns, the open-response secondary industry, and the LinkedIn URL.
#
# Matched on the payload FIELD name (the section is not part of the key), so the
# work-location entries are the career.current_* names the sheet now binds to
# (#287) — "city"/"state"/"zip"/"country" would no longer match anything.
_PLACEHOLDER_BLANK_FIELDS = frozenset(
    {
        "address_line_1",
        "address_line_2",
        "current_city",
        "current_state",
        "region",
        "current_country",
        "current_zip",
        "current_industry_secondary",
        "linkedin_url",
    }
)


# --- "Best Contact" reconciliation (#284) ------------------------------------
#
# The intake sheet's "Best Contact" column is FREE TEXT holding a literal VALUE
# (an email or a phone number), while ``preferred_contact_method`` names a
# validated METHOD (personal_email / work_email / phone / linkedin). Left alone
# the two drift apart and contradict each other.
#
# On import we resolve the free text against the row's other contact fields:
#   * matches personal_email -> preferred_contact_method = "personal_email"
#   * matches work_email     -> preferred_contact_method = "work_email"
#   * matches phone          -> preferred_contact_method = "phone"
#   * matches nothing        -> the free text is KEPT in best_contact (it's an
#     address/number we don't otherwise have) and surfaced as a review warning.
#
# Where it resolves, best_contact is CLEARED — the method now carries the intent
# and the value already lives in the field it points at, so the two cannot drift.
# An explicit preferred_contact_method (in update mode: one already stored on the
# record) always wins; reconciliation never overwrites it, and in that case the
# free text is left in place rather than silently dropped.

_PREFERRED_METHOD_ORDER = ("personal_email", "work_email", "phone")


def _phone_key(value: str) -> str:
    """Comparable form of a phone number: digits only.

    Strips punctuation/spacing so ``555-123-4567``, ``(555) 123 4567`` and
    ``5551234567`` all compare equal. A leading NANP country code is dropped so
    ``+1 555-123-4567`` matches the same number stored without it. Anything with
    no digits at all (i.e. not a phone) returns "" and never matches.
    """
    digits = re.sub(r"\D", "", value)
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    return digits


def _match_best_contact(contact: dict) -> str | None:
    """Which contact METHOD the free-text ``best_contact`` value names, if any.

    Emails compare case-insensitively after trimming; phones compare on their
    digits (see :func:`_phone_key`). Returns the ``preferred_contact_method``
    token, or None when the value matches none of the row's contact fields.
    """
    raw = contact.get("best_contact")
    value = str(raw).strip() if raw else ""
    if not value:
        return None

    for field in ("personal_email", "work_email"):
        stored = contact.get(field)
        if stored and str(stored).strip().lower() == value.lower():
            return field

    key = _phone_key(value)
    stored_phone = contact.get("phone")
    if key and stored_phone and _phone_key(str(stored_phone)) == key:
        return "phone"
    return None


def _reconcile_best_contact(contact: dict) -> str | None:
    """Resolve ``best_contact`` into ``preferred_contact_method`` in place.

    Mutates *contact*: on a clean match it sets ``preferred_contact_method`` and
    REMOVES ``best_contact`` (so the two fields can't contradict each other).
    Returns the method it resolved to, or None if it left the row untouched
    (no ``best_contact``, no match, or an explicit method already set — the
    explicit value always wins).
    """
    if contact.get("preferred_contact_method"):
        return None  # explicit wins; leave the free text alone for review
    method = _match_best_contact(contact)
    if method is None:
        return None
    contact["preferred_contact_method"] = method
    contact.pop("best_contact", None)
    return method


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
               "spouse_byu_id": str|None, "best_contact_raw": str|None,
               "error": str|None}``
        ``row`` is the 1-based spreadsheet row number (header = row 1, so the
        first data row is row 2). ``payload`` is shaped for AlumniCreateFull
        (core fields at top level, sections nested) using only non-empty cells.
        ``spouse_byu_id`` is the raw spouse BYU ID to resolve later (or None).
        ``best_contact_raw`` is the "Best Contact" cell BEFORE reconciliation
        (#284), which update mode needs to re-resolve against stored values.
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
    text = _decode_upload(file_bytes)
    if text is None:
        return [], [
            "The file could not be read. Re-save it as CSV UTF-8 from Excel "
            "(Save As → 'CSV UTF-8 (Comma delimited)') and re-upload."
        ]
    reader = csv.reader(io.StringIO(text))
    try:
        header_row = next(reader)
    except StopIteration:
        return [], ["The file is empty."]

    # Retired header spellings are folded to their current names here, so every
    # downstream step (validation, duplicate detection, _map_row) sees one
    # canonical header set and a sheet on the old template still imports.
    headers = [_canonicalize_header(h.strip()) for h in header_row]
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

        if kind == "spouse_name":
            # One combined "Spouse Name" cell -> first token is the first name,
            # the remainder is the last name (blank last if a single word).
            parts = raw.strip().split(None, 1)
            if parts:
                core["spouse_first_name"] = parts[0]
                if len(parts) > 1:
                    core["spouse_last_name"] = parts[1]
            continue

        # "Undeclared"/"N/A"/"None" marital status means "not provided" -> blank.
        if field == "marital_status" and raw.strip().lower() in _MARITAL_BLANK_TOKENS:
            continue

        # A free-text location / secondary-industry cell filled with a
        # placeholder ("unknown", "n/a") isn't known — store blank, not the
        # literal token.
        if (
            field in _PLACEHOLDER_BLANK_FIELDS
            and raw.strip().lower() in _PLACEHOLDER_TOKENS
        ):
            continue

        # Finance designations (#529). The sheet's headers say "(Yes/No)" but the
        # column is a marker string ("CFA") or NULL — a stored "No" is non-NULL
        # and would make every presence test count this alumnus as HOLDING the
        # designation. Jake, 2026-08-01: "auto make the nos into blank if entered
        # in". So a negative cell is treated exactly like an EMPTY cell: dropped
        # from the payload. In create mode that stores NULL; in update mode it
        # means "unchanged", matching this importer's blank-cell contract (an
        # explicit None can't clear it there anyway — EngagementCreate.has_values
        # treats an all-None section as nothing to write).
        if kind == "designation" and not holds_designation(raw):
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

    # NOTE (#287): a contact->career location mirror used to run here, copying
    # contact.city/state/country/zip onto career.current_*. The location columns
    # now bind straight to career.current_* above, so it had nothing left to copy
    # — and removing it is what makes a blank work location honestly blank
    # instead of a value laundered in from the residence row.

    # Resolve the free-text "Best Contact" cell against this row's own contact
    # fields (#284). Kept raw below for update mode, which must re-reconcile
    # against the STORED values (a blank email/phone cell there means
    # "unchanged", not "absent").
    contact_section = sections.get("contact")
    best_contact_raw = (
        contact_section.get("best_contact") if contact_section else None
    )
    if contact_section:
        _reconcile_best_contact(contact_section)

    payload = dict(core)
    payload.update(sections)
    return {
        "row": row_num,
        "name": _display_name(core),
        "payload": payload,
        "spouse_byu_id": spouse_byu_id,
        "best_contact_raw": best_contact_raw,
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
    if kind == "designation":
        # Negatives were already dropped by _map_row, so what reaches here is the
        # marker text the alumnus actually holds. normalize_designation is still
        # the coercion of record (one predicate, one storage form) — it returns
        # None for anything negative, which would blank the cell rather than
        # storing it.
        return normalize_designation(raw)
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

    # A "Best Contact" value still sitting in the free-text field after
    # reconciliation matched none of this row's contact fields (#284) — it's a
    # new address/number, so surface it for review rather than dropping it.
    unresolved = (cleaned.get("contact") or {}).get("best_contact")
    if unresolved:
        warnings.append(
            {
                "code": "best_contact_unresolved",
                "message": (
                    f"Best Contact {unresolved!r} doesn't match this row's "
                    "personal email, work email, or phone. It was kept as free "
                    "text — review whether it's a new contact detail."
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


# --- Update mode ("round-trip" bulk edit) ------------------------------------
#
# Staff export a graduation-year cohort to CSV (the same 64-column intake sheet),
# edit cells, and upload it back to mass-UPDATE the existing profiles. This is
# distinct from the CREATE-ONLY import above (which treats existing BYU/Net IDs
# as duplicate BLOCKERS). Rules:
#   * match each row to an existing alumnus by BYU ID first (digit-stripped),
#     then fall back to Net ID (lowercased); ACTIVE records only. A row matching
#     only an ARCHIVED record is reported as ``unmatched_archived`` (never
#     updated).
#   * blank cell = leave unchanged (``_map_row`` already omits empty cells, so a
#     mapped payload only carries the cells the user filled in — a PARTIAL update).
#   * unmatched rows are REPORTED, never created (no inserts in update mode).
#   * preview (dry-run diff) then commit, mirroring the create-mode split.
#
# Each matched row is applied through ``alumni_service.update_alumni`` so update
# semantics + provenance stamping + per-field audit match a single-record edit.
# Rows are validated against the all-optional ``AlumniUpdateFull`` (NOT the
# create schema), so a row carrying only a BYU ID + one changed cell never trips
# a required-field error.

# The intake sheet also carries "Former Company/Title/Industry" and "Finance
# Leadership Position" columns, which map to the ``former`` / ``leadership``
# sections. Those sections are NOT part of the single-record edit schema
# (``AlumniUpdateFull`` only exposes contact/career/education/engagement), so the
# update path cannot apply them; they are dropped from the update payload.
_UPDATE_UNSUPPORTED_SECTIONS = frozenset({"former", "leadership"})

# The nested sections the single-record edit path (and thus update mode) applies,
# mapped to their write-schema and ORM model. Order/model mirror
# ``alumni_service.update_alumni`` + ``_upsert_section`` so preview reads and the
# write path see the SAME related row.
_UPDATE_SECTION_SCHEMAS: dict[str, type] = {
    "contact": ContactCreate,
    "career": CareerCreate,
    "education": EducationCreate,
    "engagement": EngagementCreate,
}
_UPDATE_SECTION_MODELS: dict[str, type] = {
    "contact": AlumniContactInfo,
    "career": CurrentEmployment,
    "education": EducationHistory,
    "engagement": AlumniProgramEngagement,
}


def _to_jsonable(value: object) -> object:
    """Serialize a diff value (dates -> ISO) so old/new are JSON-safe."""
    if isinstance(value, (datetime.date, datetime.datetime)):
        return value.isoformat()
    return value


def _match_keys(payload: dict) -> tuple[str, str]:
    """Digit-stripped BYU key + lowercased Net key from a mapped row payload.

    Normalized the SAME way as ``_load_existing_index`` builds its keys, so a
    formatted "123-45-6789" or an upper-case " JDoe " still resolves."""
    byu_raw = payload.get("byu_id") or ""
    net_raw = payload.get("net_id") or ""
    byu_key = re.sub(r"\D", "", str(byu_raw).strip())
    net_key = str(net_raw).strip().lower()
    return byu_key, net_key


def _resolve_update_match(payload: dict, existing: dict) -> tuple[int | None, str]:
    """Resolve a mapped row to an existing alumnus for update.

    BYU ID first (digit-stripped), then Net ID (lowercased); ACTIVE records win.
    Returns ``(alumni_id, status)`` where status is one of ``"matched"`` (active),
    ``"unmatched_archived"`` (matches only an archived record — never updated), or
    ``"unmatched"`` (no match — never created)."""
    byu_key, net_key = _match_keys(payload)
    if byu_key and byu_key in existing["active_byu"]:
        return existing["active_byu"][byu_key][0], "matched"
    if net_key and net_key in existing["active_net"]:
        return existing["active_net"][net_key][0], "matched"
    if byu_key and byu_key in existing["archived_byu"]:
        return existing["archived_byu"][byu_key][0], "unmatched_archived"
    if net_key and net_key in existing["archived_net"]:
        return existing["archived_net"][net_key][0], "unmatched_archived"
    return None, "unmatched"


def _unmatched_message(payload: dict, status: str) -> str:
    """Human reason a row wasn't updated (unmatched / archived-only)."""
    byu_key, net_key = _match_keys(payload)
    if byu_key:
        ident = f"BYU ID {payload.get('byu_id')}"
    elif net_key:
        ident = f"Net ID {payload.get('net_id')}"
    else:
        ident = None
    if status == "unmatched_archived":
        base = "matches only an archived record, which update mode does not modify."
        return f"{ident} {base}" if ident else f"Row {base}"
    if ident is None:
        return "Row has no BYU ID or Net ID to match on; not created (update mode)."
    return f"No active alumnus matches {ident}; not created (update mode)."


def _update_payload_from_row(row: dict) -> dict:
    """The mapped payload minus sections/flags the update path cannot apply.

    Drops the ``former`` / ``leadership`` sections (not on ``AlumniUpdateFull``)
    and any ``is_alumni`` stamp; the remaining core + contact/career/education/
    engagement cells form the PARTIAL update payload."""
    return {
        key: value
        for key, value in row["payload"].items()
        if key not in _UPDATE_UNSUPPORTED_SECTIONS and key != "is_alumni"
    }


async def _current_section_row(session: AsyncSession, section: str, alumni_id: int):
    """Load the current related-section row for *alumni_id* (or None).

    Order-by mirrors ``alumni_service._upsert_section`` so the row we diff against
    (preview) is the SAME row the write path upserts."""
    model = _UPDATE_SECTION_MODELS[section]
    stmt = select(model).where(model.alumni_id == alumni_id)
    if section == "contact":
        stmt = stmt.order_by(model.contact_info_id)
    elif section == "career":
        stmt = stmt.order_by(model.current_employment_id.desc())
    elif section == "education":
        stmt = stmt.order_by(model.degree_year.desc().nullslast())
    return await session.scalar(stmt.limit(1))


async def _reconcile_update_contact(
    session: AsyncSession, alumni_id: int, row: dict, payload: dict
) -> None:
    """Re-run "Best Contact" reconciliation for UPDATE mode (#284), in place.

    Update mode reconciles against the MERGED view, not just the cells present in
    this row: a blank Personal Email / Work Email / Phone cell means "unchanged",
    so the free text must still resolve against the value already STORED on the
    record. A ``preferred_contact_method`` already on the record is an explicit
    choice and always wins — reconciliation never overwrites it.

    ``payload["contact"]`` is mutated to the reconciled result: on a match the
    method is set and ``best_contact`` becomes None (CLEARING the stored free
    text so the two fields can't drift); with no match the free text is restored.
    """
    contact = payload.get("contact")
    if not contact:
        return
    current_row = await _current_section_row(session, "contact", alumni_id)

    def _current(field: str):
        return getattr(current_row, field, None) if current_row is not None else None

    # The merged view: this row's cell if the user filled it in, else the stored
    # value. Seeded with the STORED method so an explicit one wins below.
    view = {
        field: contact.get(field) or _current(field)
        for field in _PREFERRED_METHOD_ORDER
    }
    view["best_contact"] = row.get("best_contact_raw")
    view["preferred_contact_method"] = _current("preferred_contact_method")

    _reconcile_best_contact(view)

    # None here CLEARS the stored free text — that's the point of a clean resolve.
    contact["best_contact"] = view.get("best_contact")
    method = view.get("preferred_contact_method")
    if method is None:
        contact.pop("preferred_contact_method", None)
    else:
        contact["preferred_contact_method"] = method


async def _diff_against_current(
    session: AsyncSession, alumnus: Alumni, cleaned: dict
) -> list[dict]:
    """Per-field diff of the cleaned partial payload against stored values.

    Only fields the user actually filled in appear in ``cleaned`` (blank cells
    were dropped by ``_map_row`` and ``exclude_unset``), and only fields whose new
    value differs from the current stored value are reported. Each entry is
    ``{"field","section","old","new"}`` with old/new JSON-serialized."""
    changes: list[dict] = []
    for field, new in cleaned.items():
        if field in _UPDATE_SECTION_SCHEMAS:
            continue  # nested section handled below
        old = getattr(alumnus, field, None)
        if old != new:
            changes.append(
                {
                    "field": field,
                    "section": "core",
                    "old": _to_jsonable(old),
                    "new": _to_jsonable(new),
                }
            )
    for section in _UPDATE_SECTION_SCHEMAS:
        section_data = cleaned.get(section)
        if not section_data:
            continue
        current_row = await _current_section_row(session, section, alumnus.alumni_id)
        for field, new in section_data.items():
            old = getattr(current_row, field, None) if current_row is not None else None
            if old != new:
                changes.append(
                    {
                        "field": field,
                        "section": section,
                        "old": _to_jsonable(old),
                        "new": _to_jsonable(new),
                    }
                )
    return changes


async def _evaluate_update_row(
    session: AsyncSession, row: dict, existing: dict
) -> dict:
    """Evaluate one mapped row into an update-preview entry."""
    base = {
        "row": row["row"],
        "name": row["name"],
        "alumni_id": None,
        "status": "error",
        "changes": [],
        "error": None,
        "message": None,
    }

    # A mapping-stage coercion error (bad date/number/industry) rejects the row.
    if row["error"]:
        base["error"] = row["error"]
        return base

    alumni_id, match_status = _resolve_update_match(row["payload"], existing)
    if match_status != "matched":
        base["status"] = match_status
        base["alumni_id"] = alumni_id  # archived id for unmatched_archived, else None
        base["message"] = _unmatched_message(row["payload"], match_status)
        return base

    base["alumni_id"] = alumni_id
    # Validate the non-blank cells against the PARTIAL (all-optional) update schema
    # — never the create schema, so a required-field error can't fire on update.
    try:
        model = AlumniUpdateFull(**_update_payload_from_row(row))
    except ValidationError as exc:
        base["error"] = _format_validation_error(exc)
        return base

    alumnus = await alumni_service.get_alumni(session, alumni_id)
    # jsonable=False keeps dates as date objects so they compare equal to the ORM
    # values; _to_jsonable serializes them for the report.
    cleaned, _changes = hygiene.clean_alumni_payload(model, jsonable=False)
    # Re-reconcile Best Contact against the STORED values (#284) so the preview
    # diff shows exactly what commit_update will apply.
    if row.get("best_contact_raw"):
        await _reconcile_update_contact(session, alumni_id, row, cleaned)
    changes = await _diff_against_current(session, alumnus, cleaned)
    base["changes"] = changes
    base["status"] = "update" if changes else "no_changes"
    return base


async def evaluate_update(session: AsyncSession, rows: list[dict]) -> dict:
    """Dry-run report for a bulk UPDATE ("round-trip") of already-parsed *rows*.

    For each row: resolve the match (BYU -> Net, active only); for matched rows
    validate the non-blank cells against ``AlumniUpdateFull`` and compute a
    per-field diff against the CURRENT stored values. Reports summary counts plus
    per-row detail. NO writes."""
    existing = await _load_existing_index(session)

    out_rows: list[dict] = []
    matched = unmatched = with_changes = errors = 0
    for row in rows:
        report = await _evaluate_update_row(session, row, existing)
        out_rows.append(report)
        status = report["status"]
        if status in ("update", "no_changes"):
            matched += 1
        elif status in ("unmatched", "unmatched_archived"):
            unmatched += 1
        if status == "update":
            with_changes += 1
        elif status == "error":
            errors += 1

    return {
        "columns_ok": True,
        "header_errors": [],
        "summary": {
            "total": len(rows),
            "matched": matched,
            "unmatched": unmatched,
            "with_changes": with_changes,
            "errors": errors,
        },
        "rows": out_rows,
    }


async def _build_update_model(
    session: AsyncSession, alumni_id: int, row: dict
) -> AlumniUpdateFull:
    """Build the ``AlumniUpdateFull`` applied for a matched row.

    Core cells pass through as the partial payload (``update_alumni`` only touches
    the fields present). For each section the user filled in, the provided cells
    are MERGED on top of the record's CURRENT section values — because
    ``update_alumni`` overwrites the whole related row from the section, merging
    the current values back in is what keeps a blank cell from clearing an
    existing value."""
    partial = _update_payload_from_row(row)
    # Best Contact resolves against the STORED contact values here (#284), not
    # just this row's cells — same call the preview makes, so both agree. Copy
    # the section first: the parsed row is re-read on retry/re-evaluation.
    if row.get("best_contact_raw") and partial.get("contact"):
        partial["contact"] = dict(partial["contact"])
        await _reconcile_update_contact(session, alumni_id, row, partial)
    for section, schema in _UPDATE_SECTION_SCHEMAS.items():
        provided = partial.get(section)
        if not provided:
            continue
        current_row = await _current_section_row(session, section, alumni_id)
        merged = {
            field: (getattr(current_row, field, None) if current_row is not None else None)
            for field in schema.model_fields
        }
        merged.update(provided)
        partial[section] = merged
    return AlumniUpdateFull(**partial)


async def commit_update(
    session: AsyncSession,
    rows: list[dict],
    actor_user_id: int | None = None,
) -> dict:
    """Re-evaluate *rows* and apply every matched, changed row in ONE transaction.

    Mirrors ``commit_import``'s stance: re-evaluate rather than trust a client
    preview; apply each matched row via ``alumni_service.update_alumni`` (so
    cleaning + provenance stamping + per-field audit fire exactly as for a manual
    edit). ``update_alumni`` commits internally; as in ``commit_import`` we
    temporarily neutralize its commit/refresh (flush instead) and run one real
    commit after the loop. Each row runs inside its OWN SAVEPOINT so a mid-batch
    failure rolls back only that row. Unmatched rows are reported, never created;
    rows with no effective change are reported ``unchanged``. Returns:
        ``{"updated": int, "unchanged": int, "unmatched": int, "errors": int,
           "updated_ids": [int],
           "results": [{"row","name","alumni_id","status","message"}]}``
    """
    report = await evaluate_update(session, rows)
    parsed_by_row = {r["row"]: r for r in rows}

    updated_ids: list[int] = []
    results: list[dict] = []
    updated = unchanged = unmatched = errors = 0

    real_commit = session.commit
    real_refresh = getattr(session, "refresh", None)

    async def _noop_commit() -> None:
        await session.flush()

    async def _noop_refresh(_obj: object) -> None:
        return None

    for evaluated in report["rows"]:
        row_num = evaluated["row"]
        status = evaluated["status"]
        if status == "error":
            errors += 1
            results.append(
                {
                    "row": row_num,
                    "name": evaluated["name"],
                    "alumni_id": evaluated["alumni_id"],
                    "status": "error",
                    "message": evaluated["error"] or "Rejected.",
                }
            )
            continue
        if status in ("unmatched", "unmatched_archived"):
            unmatched += 1
            results.append(
                {
                    "row": row_num,
                    "name": evaluated["name"],
                    "alumni_id": evaluated["alumni_id"],
                    "status": status,
                    "message": evaluated["message"],
                }
            )
            continue
        if status == "no_changes":
            unchanged += 1
            results.append(
                {
                    "row": row_num,
                    "name": evaluated["name"],
                    "alumni_id": evaluated["alumni_id"],
                    "status": "unchanged",
                    "message": "No changes.",
                }
            )
            continue

        # status == "update": apply through the single-record edit path.
        parsed = parsed_by_row[row_num]
        alumni_id = evaluated["alumni_id"]
        async with session.begin_nested() as savepoint:
            try:
                model = await _build_update_model(session, alumni_id, parsed)
                session.commit = _noop_commit  # type: ignore[method-assign]
                if real_refresh is not None:
                    session.refresh = _noop_refresh  # type: ignore[method-assign]
                try:
                    await alumni_service.update_alumni(
                        session, alumni_id, model, actor_user_id=actor_user_id
                    )
                finally:
                    session.commit = real_commit  # type: ignore[method-assign]
                    if real_refresh is not None:
                        session.refresh = real_refresh  # type: ignore[method-assign]
                updated_ids.append(alumni_id)
                updated += 1
                results.append(
                    {
                        "row": row_num,
                        "name": evaluated["name"],
                        "alumni_id": alumni_id,
                        "status": "updated",
                        "message": "Updated.",
                    }
                )
            except Exception as exc:  # noqa: BLE001 - record + continue per row
                await savepoint.rollback()
                errors += 1
                results.append(
                    {
                        "row": row_num,
                        "name": evaluated["name"],
                        "alumni_id": alumni_id,
                        "status": "error",
                        "message": _classify_reject(exc, row_num),
                    }
                )

    # One real commit for the whole updated batch.
    if updated:
        await session.commit()

    return {
        "updated": updated,
        "unchanged": unchanged,
        "unmatched": unmatched,
        "errors": errors,
        "updated_ids": updated_ids,
        "results": results,
    }


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


# --- Cohort round-trip export (a FILLED update template) ---------------------
#
# The inverse of the empty template above: emit a FILLED intake-template CSV for
# one graduation-year cohort, so staff can download the cohort, edit cells
# offline, and re-upload through ``POST /alumni/import/update`` (the bulk-update
# path above). The header row is EXACTLY :data:`EXPECTED_HEADERS` and every cell
# is formatted to the template's TEXT form (dates -> YYYY-MM-DD, bools -> Yes/No,
# ints -> str), so the file round-trips: a value written here re-parses via
# ``_map_row`` back to the same value.
#
# Column reverse-map: for each header, ``_MAPPING[header]`` gives
# ``(section, field, kind)``. ``core`` fields read off the ``Alumni`` row;
# ``contact`` / ``career`` / ``education`` / ``engagement`` read off that
# alumnus's loaded 1:1 side row (blank if the alumnus has none). Headers with no
# ``_MAPPING`` entry, or whose section is a multi-row history table with no clean
# single-field source (``former`` -> employment_history, ``leadership`` ->
# finance_society_leadership), export EMPTY. BYU ID + Net ID are ``core`` entries
# in ``_MAPPING`` so they always populate — they are the re-import match keys.

# The 1:1 side tables we can reverse-map a single row from, same load machinery
# as the customizable export. ``former`` / ``leadership`` are intentionally
# absent, so their columns export blank.
_COHORT_SIDE_MODELS: dict[str, type] = {
    "contact": AlumniContactInfo,
    "career": CurrentEmployment,
    "engagement": AlumniProgramEngagement,
}


class CohortTooLargeError(Exception):
    """A cohort export exceeds the export row cap (route -> 413).

    Uses the SAME cap and "narrow it down" wording as the customizable export so
    an over-large cohort fails clearly instead of streaming the whole table."""

    def __init__(self, total: int) -> None:
        self.total = total
        super().__init__(
            f"This cohort matches {total:,} alumni, over the "
            f"{alumni_export.MAX_EXPORT_ROWS:,}-row export limit. "
            "Narrow it down and try again."
        )


def _cohort_cell(value: object, kind: str) -> str:
    """Format one reverse-mapped value to the template's text form.

    Reuses the export formatter (dates -> ISO, bools -> Yes/No, ints -> str, plus
    the spreadsheet formula-injection guard). ``industry`` and any other kind fall
    through as plain text — the stored industry is already a canonical vocab value
    that re-parses cleanly."""
    fmt_kind = kind if kind in ("bool", "date", "int") else "str"
    return alumni_export._fmt(value, fmt_kind)


async def build_cohort_update_csv(
    session: AsyncSession,
    *,
    graduation_year: int | None = None,
    graduation_class: int | None = None,
    actor_user_id: int | None = None,
) -> str:
    """Build a FILLED intake-template CSV for one active graduation-year cohort.

    Loads every ACTIVE alumnus with ``graduation_year`` (via the shared
    ``build_alumni_query`` — same archived / alumni-vs-friend gating the list and
    export use), plus their 1:1 contact / career / education / engagement rows
    (batch-loaded once each, no N+1). Emits a CSV whose header row is EXACTLY
    ``EXPECTED_HEADERS`` and whose cells are reverse-mapped through ``_MAPPING``
    and formatted for a clean re-import.

    Enforces the SAME hard row cap as the customizable export
    (:data:`alumni_export.MAX_EXPORT_ROWS`); over the cap it raises
    :class:`CohortTooLargeError` (the route maps that to a 413). Writes an
    ``export_alumni`` disclosure audit row (actor + cohort + row count, never the
    data) and commits, mirroring the export path."""
    if (graduation_year is None) == (graduation_class is None):
        raise ValueError("Pass exactly one of graduation_year or graduation_class.")
    base = build_alumni_query(
        graduation_year=graduation_year, graduation_class=graduation_class
    )
    total = await session.scalar(select(func.count()).select_from(base.subquery()))
    if total and total > alumni_export.MAX_EXPORT_ROWS:
        raise CohortTooLargeError(int(total))

    stmt = base.order_by(Alumni.last_name.asc(), Alumni.alumni_id.asc()).limit(
        alumni_export.MAX_EXPORT_ROWS
    )
    alumni = (await session.execute(stmt)).scalars().all()
    ids = [a.alumni_id for a in alumni]

    side: dict[str, dict[int, object]] = {
        section: await alumni_export._load_side(session, model, ids)
        for section, model in _COHORT_SIDE_MODELS.items()
    }
    # Education keeps the latest entry (greatest degree_year, newest on ties),
    # exactly as the customizable export does.
    side["education"] = await alumni_export._load_side(
        session, EducationHistory, ids, latest_by="degree_year", pk_attr="education_id"
    )

    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(EXPECTED_HEADERS)
    for alumnus in alumni:
        row_out: list[str] = []
        for header in EXPECTED_HEADERS:
            target = _MAPPING.get(header)
            if target is None:
                row_out.append("")  # no clean single-field source -> blank
                continue
            section, field, kind = target
            if kind == "spouse_name":
                # Inverse of the import split: join first + last back into the
                # single "Spouse Name" cell so the round-trip re-parses cleanly.
                first = getattr(alumnus, "spouse_first_name", None) or ""
                last = getattr(alumnus, "spouse_last_name", None) or ""
                row_out.append(_cohort_cell(f"{first} {last}".strip(), "str"))
                continue
            if section == "core":
                source_row = alumnus
            else:
                source_row = side.get(section, {}).get(alumnus.alumni_id)
            value = getattr(source_row, field, None) if source_row is not None else None
            row_out.append(_cohort_cell(value, kind))
        writer.writerow(row_out)

    cohort_label = (
        f"grad_year={graduation_year}"
        if graduation_year is not None
        else f"class_year={graduation_class}"
    )
    _audit_cohort_export(session, actor_user_id, cohort_label, len(alumni))
    await session.commit()
    return buffer.getvalue()


def _audit_cohort_export(
    session: AsyncSession,
    actor_user_id: int | None,
    cohort_label: str,
    row_count: int,
) -> None:
    """Disclosure audit for a cohort round-trip export — actor + WHAT left the
    system (the cohort year and row count), never the data itself. Mirrors
    ``alumni_export._audit_export``; a missing actor is logged, not silently
    dropped."""
    if actor_user_id is None:
        log.warning("Cohort export audit skipped: no actor (rows=%s)", row_count)
        return
    summary = f"cohort update template; {cohort_label}; rows={row_count}"
    session.add(
        AuditLog(
            user_id=actor_user_id,
            action_type="export_alumni",
            entity_type="alumni",
            entity_id=None,
            new_value=summary[:2000],
        )
    )
