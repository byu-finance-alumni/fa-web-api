"""Event attendee SPREADSHEET import (api#693, 2026-11-18 meeting).

A sign-up spreadsheet for ONE event, with the columns Jake's lists carry:

    Fname, Lname, Company, Role, Email, Focus, UG, UG YR, LinkedIn, Grad,
    GradYR, Net ID

* **Net ID is optional** — the column may be missing, or the cell blank.
* **Extra columns are ignored**, never an error (reported back so it is
  visible, the way the conference matcher reports them).

Per row:

* **Matched** — an existing, non-archived record whose first name (or preferred
  first name) + last name + a grad year agree with the row. "Grad year" is the
  row's ``UG YR`` OR ``GradYR``, compared against the record's
  ``graduation_year`` OR ``graduate_graduation_year``. An existing FRIEND
  (``is_alumni = false``) with the same name also matches when the friend has no
  year on file (or a year that agrees), so re-uploading the same sheet reuses the
  friend instead of creating a second one.
* **Ambiguous** — more than one record qualifies. Every candidate is listed and
  none is picked; the reviewer chooses on the confirm screen.
* **New alum** — no match, and ``Focus`` or ``Grad`` says "Fin" / "Finance".
* **New friend** — no match otherwise.

THE INVARIANT (from the issue): :func:`preview` never writes alumni or
attendance. The confirm screen sends back every row with the reviewer's
(possibly edited) values and choice, and only :func:`commit` writes — and it
re-validates every id and every field server-side, never trusting the preview.

Upload size and row limits are the conference matcher's
(``attendee_match.MAX_UPLOAD_BYTES`` / ``MAX_ATTENDEE_ROWS``), and parsing
reuses the shared CSV decoder, so Excel's ANSI saves and an over-long cell
behave exactly as they do on every other importer.
"""

from __future__ import annotations

import csv
import datetime
import io
import logging
import re
from collections.abc import Iterable

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.friend_id import friend_id_for
from app.models.alumni import Alumni
from app.models.employment import CurrentEmployment
from app.models.event import EventAttendance
from app.services.attendee_match import (
    MAX_ATTENDEE_ROWS,
    MAX_CANDIDATES_PER_ROW,
    MAX_UPLOAD_BYTES,
    _header_key,
    _norm_col,
    _norm_name,
    net_id_is_well_formed,
)
from app.services.import_csv import _decode_upload, unreadable_csv_message

log = logging.getLogger(__name__)

__all__ = [
    "MAX_UPLOAD_BYTES",
    "MAX_SHEET_ROWS",
    "MAX_NEW_RECORDS_PER_COMMIT",
    "parse_sheet",
    "preview",
    "is_finance",
    "new_record_payload",
]

MAX_SHEET_ROWS = MAX_ATTENDEE_ROWS
# New alumni / friends created by ONE confirm. Same cap as the conference
# matcher's friends leg (attendee_match.MAX_FRIENDS_PER_REQUEST): every create
# runs the full duplicate-detection path, and a serverless request has a clock.
MAX_NEW_RECORDS_PER_COMMIT = 500
# Bound on the candidate pool loaded for one file (a 2,000-row sheet of
# "Smith"s must not pull the whole directory into memory). Hitting it is
# reported as a warning, never silently truncated away.
_MAX_CANDIDATE_ROWS = 3000
# Longest value kept from any one cell. Wider than every target column it feeds
# (company/role 255, email 255, LinkedIn 500) is pointless.
CELL_MAX = 500

# --- Columns -----------------------------------------------------------------

FIELDS = (
    "first_name",
    "last_name",
    "company",
    "role",
    "email",
    "focus",
    "ug",
    "ug_year",
    "linkedin",
    "grad",
    "grad_year",
    "net_id",
)

# Header spellings (lower-cased, punctuation collapsed by ``_header_key``) for
# each field. The meeting's exact headers come first; the rest are the obvious
# variants a hand-made sheet uses.
_ALIASES: dict[str, str] = {
    "fname": "first_name",
    "first name": "first_name",
    "firstname": "first_name",
    "first": "first_name",
    "lname": "last_name",
    "last name": "last_name",
    "lastname": "last_name",
    "last": "last_name",
    "surname": "last_name",
    "company": "company",
    "employer": "company",
    "organization": "company",
    "firm": "company",
    "role": "role",
    "title": "role",
    "job title": "role",
    "position": "role",
    "email": "email",
    "e mail": "email",
    "email address": "email",
    "focus": "focus",
    "ug": "ug",
    "undergrad": "ug",
    "undergraduate": "ug",
    "ug yr": "ug_year",
    "ugyr": "ug_year",
    "ug year": "ug_year",
    "undergrad year": "ug_year",
    "linkedin": "linkedin",
    "linkedin url": "linkedin",
    "linkedin profile": "linkedin",
    "grad": "grad",
    "graduate": "grad",
    "grad program": "grad",
    "gradyr": "grad_year",
    "grad yr": "grad_year",
    "grad year": "grad_year",
    "graduate year": "grad_year",
    "net id": "net_id",
    "netid": "net_id",
}

TEMPLATE_HEADERS = (
    "Fname",
    "Lname",
    "Company",
    "Role",
    "Email",
    "Focus",
    "UG",
    "UG YR",
    "LinkedIn",
    "Grad",
    "GradYR",
    "Net ID",
)

_YEAR_RE = re.compile(r"^(\d{4})(?:\.0+)?$")
_FIN_RE = re.compile(r"\bfin(?:ance)?\b", re.IGNORECASE)


def is_finance(*values: object) -> bool:
    """True when any value says "Fin" or "Finance" as a word (case-insensitive).

    "Fin", "FIN", "Finance", "MBA - Finance" qualify; "Financial Planning" and
    "Final" do not (the issue names exactly "Fin" and "Finance")."""
    return any(isinstance(v, str) and _FIN_RE.search(v) for v in values)


def _parse_year(value: str) -> int | None:
    match = _YEAR_RE.match(value.strip())
    if match is None:
        return None
    year = int(match.group(1))
    if year < 1940 or year > datetime.date.today().year + 10:
        return None
    return year


def _normalize_linkedin(value: str) -> str:
    """Add the scheme a hand-typed ``linkedin.com/in/x`` lacks, so it passes the
    alumni schema's http(s)-only LinkedIn check instead of being dropped."""
    lowered = value.lower()
    if lowered.startswith(("linkedin.com", "www.linkedin.com")):
        return f"https://{value}"
    return value


# --- Stage 1: parse ----------------------------------------------------------


def parse_sheet(
    file_bytes: bytes, max_rows: int = MAX_SHEET_ROWS
) -> tuple[list[dict], list[str], list[str]]:
    """Parse the sheet into row dicts.

    Returns ``(rows, header_errors, ignored_columns)``. A row dict carries every
    key in :data:`FIELDS` (``None`` when absent), plus ``row`` (1-based
    spreadsheet line) and ``warnings``. ``header_errors`` is non-empty only when
    the file is unusable (unreadable, empty, no name columns, a duplicated
    column, too many rows)."""
    text = _decode_upload(file_bytes)
    if text is None:  # pragma: no cover - latin-1 never raises
        return [], ["The file could not be read. Re-save it as CSV UTF-8."], []
    reader = csv.reader(io.StringIO(text))
    try:
        raw_headers = next(reader)
    except StopIteration:
        return [], ["The file is empty."], []
    except csv.Error:
        return [], [unreadable_csv_message(reader.line_num)], []

    if len(raw_headers) == 1 and (
        raw_headers[0].count(";") >= 2 or raw_headers[0].count("\t") >= 2
    ):
        return (
            [],
            [
                "This looks like a semicolon- or tab-delimited file. Re-save it "
                "as a comma-delimited CSV and try again."
            ],
            [],
        )

    index: dict[str, int] = {}
    ignored: list[str] = []
    duplicates: set[str] = set()
    for i, raw in enumerate(raw_headers):
        key = _header_key(raw.lstrip("﻿"))
        field = _ALIASES.get(key)
        if field is None:
            if raw.strip():
                ignored.append(raw.strip())
            continue
        if field in index:
            duplicates.add(raw.strip())
            continue
        index[field] = i

    header_errors = [f"Duplicate column: {d!r}." for d in sorted(duplicates)]
    missing = [
        label
        for field, label in (("first_name", "Fname"), ("last_name", "Lname"))
        if field not in index
    ]
    if missing:
        header_errors.append(
            "Missing required column(s): " + ", ".join(missing) + "."
        )
    if header_errors:
        return [], header_errors, ignored

    rows: list[dict] = []
    try:
        for line, raw_row in enumerate(reader, start=2):
            if not any((cell or "").strip() for cell in raw_row):
                continue
            if len(rows) >= max_rows:
                return (
                    [],
                    [
                        f"File exceeds the {max_rows:,}-row limit. Split it into "
                        "smaller batches."
                    ],
                    ignored,
                )
            rows.append(_build_row(line, index, raw_row))
    except csv.Error:
        return [], [unreadable_csv_message(reader.line_num)], ignored
    return rows, [], ignored


def _build_row(line: int, index: dict[str, int], raw_row: list[str]) -> dict:
    row: dict = {"row": line, "warnings": []}
    for field in FIELDS:
        pos = index.get(field)
        value = (raw_row[pos] if pos is not None and pos < len(raw_row) else "") or ""
        value = " ".join(value.split())
        if len(value) > CELL_MAX:
            row["warnings"].append(f"{_LABELS[field]} is too long and was left blank.")
            value = ""
        row[field] = value or None

    for field in ("ug_year", "grad_year"):
        raw = row[field]
        if raw is None:
            continue
        year = _parse_year(raw)
        if year is None:
            row["warnings"].append(
                f"{_LABELS[field]} '{raw[:12]}' is not a 4-digit year and was ignored."
            )
        row[field] = year

    if row["net_id"] is not None:
        net_id = row["net_id"].strip().lower()
        if not net_id_is_well_formed(net_id):
            row["warnings"].append("Net ID is not a valid Net ID and was ignored.")
            net_id = None
        row["net_id"] = net_id
    if row["email"] is not None:
        row["email"] = row["email"].lower()
        if "@" not in row["email"]:
            row["warnings"].append("Email does not look like an email address.")
    if row["linkedin"] is not None:
        row["linkedin"] = _normalize_linkedin(row["linkedin"])
    if not row["first_name"] or not row["last_name"]:
        row["warnings"].append("A first and last name are both needed.")
    return row


_LABELS = {
    "first_name": "Fname",
    "last_name": "Lname",
    "company": "Company",
    "role": "Role",
    "email": "Email",
    "focus": "Focus",
    "ug": "UG",
    "ug_year": "UG YR",
    "linkedin": "LinkedIn",
    "grad": "Grad",
    "grad_year": "GradYR",
    "net_id": "Net ID",
}


# --- Stage 2: preview (NO writes) --------------------------------------------


def _years(*values: int | None) -> set[int]:
    return {v for v in values if v is not None}


def _candidate_dict(c) -> dict:
    first = c.preferred_first_name or c.first_name
    name = " ".join(p for p in (first, c.last_name) if p).strip()
    return {
        "alumni_id": c.alumni_id,
        "name": name or f"Record #{c.alumni_id}",
        "graduation_year": c.graduation_year,
        "graduate_graduation_year": c.graduate_graduation_year,
        "is_alumni": bool(c.is_alumni),
        "friend_id": friend_id_for(c.alumni_id, c.is_alumni),
        "current_employer": c.current_employer,
    }


def row_qualifies(row: dict, candidate) -> bool:
    """Name + year rule for one candidate (see the module docstring)."""
    first = _norm_name(row.get("first_name"))
    last = _norm_name(row.get("last_name"))
    if not first or not last:
        return False
    if _norm_name(candidate.last_name) != last:
        return False
    if first not in {
        _norm_name(candidate.first_name),
        _norm_name(candidate.preferred_first_name),
    }:
        return False
    row_years = _years(row.get("ug_year"), row.get("grad_year"))
    record_years = _years(
        candidate.graduation_year, candidate.graduate_graduation_year
    )
    if row_years & record_years:
        return True
    # A friend usually has no year on file: the name alone reuses them.
    return not candidate.is_alumni and not record_years


def _candidate_query(last_names: Iterable[str]):
    return (
        select(
            Alumni.alumni_id,
            Alumni.first_name,
            Alumni.preferred_first_name,
            Alumni.last_name,
            Alumni.graduation_year,
            Alumni.graduate_graduation_year,
            Alumni.is_alumni,
            CurrentEmployment.current_employer,
        )
        .outerjoin(CurrentEmployment, CurrentEmployment.alumni_id == Alumni.alumni_id)
        .where(Alumni.archived.is_(False))
        .where(_norm_col(Alumni.last_name).in_(sorted(last_names)))
        .limit(_MAX_CANDIDATE_ROWS)
    )


def default_kind(row: dict) -> str:
    """``new_alumni`` when Focus/Grad says Fin/Finance, else ``new_friend``."""
    return "new_alumni" if is_finance(row.get("focus"), row.get("grad")) else "new_friend"


async def preview(session: AsyncSession, event_id: int, rows: list[dict]) -> dict:
    """The confirm-screen report. Reads only; never adds alumni or attendance.

    Two queries for the whole file: the candidate pool (by last name, exact
    after lower+trim, the indexed expression) and this event's roster."""
    warnings: list[str] = []
    last_names = {
        r["last_name"].strip().lower() for r in rows if r.get("last_name")
    }
    candidates = []
    if last_names:
        candidates = list((await session.execute(_candidate_query(last_names))).all())
        if len(candidates) >= _MAX_CANDIDATE_ROWS:
            warnings.append(
                "This file matched too many existing records to check them all. "
                "Split it into smaller files."
            )
    roster: set[int] = set(
        (
            await session.execute(
                select(EventAttendance.alumni_id).where(
                    EventAttendance.event_id == event_id
                )
            )
        )
        .scalars()
        .all()
    )

    by_last: dict[str, list] = {}
    for c in candidates:
        by_last.setdefault(_norm_name(c.last_name), []).append(c)

    summary = {
        "total_rows": len(rows),
        "matched": 0,
        "ambiguous": 0,
        "new_alumni": 0,
        "new_friend": 0,
        "already_attending": 0,
        "invalid": 0,
    }
    disclosed: set[int] = set()
    seen_people: dict[tuple, int] = {}
    out: list[dict] = []
    for row in rows:
        row_warnings = list(row.get("warnings", []))
        hits = [
            c
            for c in by_last.get(_norm_name(row.get("last_name")), [])
            if row_qualifies(row, c)
        ]
        # De-dupe (the employment outer join can repeat a record).
        unique: dict[int, object] = {}
        for c in hits:
            unique.setdefault(c.alumni_id, c)
        hits = list(unique.values())[:MAX_CANDIDATES_PER_ROW]
        cand = [_candidate_dict(c) for c in hits]
        disclosed.update(c["alumni_id"] for c in cand)

        alumni_id: int | None = None
        if not row.get("first_name") or not row.get("last_name"):
            status, action = "invalid", "skip"
        elif len(hits) == 1:
            alumni_id = hits[0].alumni_id
            if alumni_id in roster:
                status, action = "already_attending", "skip"
            else:
                status, action = "matched", "match"
        elif len(hits) > 1:
            status, action = "ambiguous", "match"
            row_warnings.append(
                f"{len(hits)} existing records match. Pick the right one."
            )
        else:
            status = action = default_kind(row)

        key = (
            _norm_name(row.get("first_name")),
            _norm_name(row.get("last_name")),
            row.get("ug_year"),
            row.get("grad_year"),
        )
        if status != "invalid":
            if key in seen_people:
                row_warnings.append(
                    f"Same person as row {seen_people[key]}. Only one will be saved."
                )
            else:
                seen_people[key] = row["row"]
        summary[status] += 1
        out.append(
            {
                **{f: row.get(f) for f in FIELDS},
                "row": row["row"],
                "status": status,
                "action": action,
                "alumni_id": alumni_id,
                "candidates": cand,
                "warnings": row_warnings,
            }
        )

    return {
        "columns_ok": True,
        "header_errors": [],
        "ignored_columns": [],
        "summary": summary,
        "rows": out,
        "warnings": warnings,
        "_disclosed_alumni_ids": sorted(disclosed),
    }


# --- Stage 3 helpers (the route performs the writes) -------------------------


def new_record_payload(row, *, is_alumni: bool) -> dict:
    """An ``AlumniCreateFull`` payload from a confirmed row: name, Net ID,
    LinkedIn, the two years, email, company and role (the issue's list). The
    Focus / UG / Grad text decides alum-vs-friend and is not stored."""
    payload: dict = {
        "first_name": row.first_name,
        "last_name": row.last_name,
        "is_alumni": is_alumni,
        "net_id": row.net_id,
        "linkedin_url": row.linkedin,
        "graduation_year": row.ug_year,
        "graduate_graduation_year": row.grad_year,
    }
    if row.email:
        payload["contact"] = {"personal_email": row.email}
    if row.company or row.role:
        payload["career"] = {
            "current_employer": row.company,
            "current_title": row.role,
        }
    return {k: v for k, v in payload.items() if v is not None}


def build_template_csv() -> str:
    """Header row plus two example rows (one Finance alum, one friend)."""
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(TEMPLATE_HEADERS)
    writer.writerow(
        [
            "Jane",
            "Doe",
            "Goldman Sachs",
            "Analyst",
            "jane.doe@example.org",
            "Fin",
            "BYU",
            "2019",
            "https://www.linkedin.com/in/janedoe",
            "",
            "",
            "jdoe19",
        ]
    )
    writer.writerow(
        [
            "Sam",
            "Lee",
            "Acme Corp",
            "Recruiter",
            "sam.lee@example.org",
            "Marketing",
            "",
            "",
            "",
            "",
            "",
            "",
        ]
    )
    return buffer.getvalue()
