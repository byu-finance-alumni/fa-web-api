"""Generate the alumni + events data-intake template (Excel workbook).

Produces an .xlsx the department can fill in, with three sheets:

  * "Alumni"           — one row per alumnus (identity, birthday, spouse,
                         contact, current career, education, engagement).
  * "Events"           — one row per event (the event-level fields to record).
  * "Event attendance" — the attendance form/survey layout: one row per
                         (alumnus, event) so staff can record who attended what.

Events don't belong to a single alumnus, so they live on their own sheet;
attendance is the bridge that links an alumnus to an event.

This is a TEMPLATE generator — it does NOT read the database, so it runs with no
DB connection and always reflects the current field set. Keep the column lists
in sync with database/schema.sql when fields change.

The .xlsx is written with the standard library only (it's a zip of XML parts),
so there's no third-party dependency to install.

Run from the repo root:

    .venv/Scripts/python -m scripts.export_intake_template
    .venv/Scripts/python -m scripts.export_intake_template --out "C:/path/file.xlsx"
    .venv/Scripts/python -m scripts.export_intake_template --blank-rows 50
"""

import argparse
import datetime
import zipfile
from pathlib import Path
from xml.sax.saxutils import escape

# The one controlled vocabulary that the import path validates. Imported from the
# backend so the template's allowed-values reference can never drift from what the
# API actually accepts. (Pure constants — importing does not touch the DB.)
from app.core.dropdowns import INDUSTRIES

# Attendance status has no backend constant yet; this mirrors the frontend's
# ATTENDANCE_STATUS_OPTIONS (fa-web-app/src/constants/dropdowns.ts).
_ATTENDANCE_STATUSES = ("Registered", "Attended", "No Show", "Cancelled")

# Default output: workspace root (one level above the fa-web-api repo).
_DEFAULT_OUT = (
    Path(__file__).resolve().parents[2] / "Alumni_Data_Intake_Template.xlsx"
)

# Each sheet: (name, [(header, example_value), ...]). The example row mirrors the
# fully-populated mock records so staff can see the expected format.

# The finalized 64-column alumni intake set. Order + EXACT header text are the
# contract: import_csv.EXPECTED_HEADERS is derived from these headers and header
# validation is exact-match both ways, so any edit here must match the importer's
# _MAPPING keys verbatim (capitalization, punctuation, the parenthetical on
# Region, the trailing colon on "Other Designations:", the "#" on "Phone #", and
# the em-dash in the Women-in-Finance header).
_ALUMNI_COLUMNS: list[tuple[str, str]] = [
    ("Filled out Survey", "2026-01-15"),
    ("MSTID (from OneAccord)", "MST-000123"),
    ("BYU ID (9 digits)", "001000001"),
    ("Net ID", "jdoe1"),
    ("Preferred first name", ""),
    ("First name", "James"),
    ("Middle name", ""),
    ("Last Name", "Doe"),
    ("Gender", "M"),
    ("Personal Email", "james.doe@example.com"),
    ("Birthday (YYYY-MM-DD)", "1990-03-15"),
    ("Graduation Semester", "Winter"),
    ("Graduation Year", "2012"),
    ("Class of", "2012"),
    ("LinkedIn URL", "https://linkedin.com/in/mock-jdoe"),
    ("Finance program admitted year", "2011"),
    ("Employment Status", "Employed"),
    ("Profile Updated By", "Amy Adams"),
    ("Profile Updated Date", "2026-02-01"),
    ("Finance Leadership Position", "Finance Society President"),
    ("Graduate degree", ""),
    ("Graduate university", ""),
    ("Graduate graduation year", ""),
    ("Deceased? (Yes/No)", "No"),
    ("Notes", "Investment banking track."),
    ("Citizenship", "USA"),
    ("Marital Status", "Married"),
    ("Languages", "English; Spanish"),
    ("Spouse Name", "Ava Lee"),
    ("Phone #", "+1 (212) 555-0142"),
    ("Current employer", "Goldman Sachs"),
    ("Current title", "Vice President"),
    ("Current industry (see Reference sheet)", "Investment Banking"),
    ("Secondary industry (see Reference sheet)", "Private Equity"),
    ("Work Email", "jdoe@goldmansachs.com"),
    ("Address line 1", "200 West St"),
    ("Address line 2", ""),
    ("Residence city", "Brooklyn"),
    ("Residence state", "NY"),
    ("Current city", "New York"),
    ("Current state", "NY"),
    (
        "Region (Northeast, Southeast, Midwest, Southwest, West, and Mountain West)",
        "Northeast",
    ),
    ("Current country", "USA"),
    ("Current ZIP", "10282"),
    ("Home country", "USA"),
    ("Degree", "BS"),
    ("Major", "Finance"),
    ("Degree status", "Completed"),
    ("Degree year", "2012"),
    ("Former Company", "Morgan Stanley"),
    ("Former Title", "Analyst"),
    ("Former Industry", "Investment Banking"),
    ("Willing to host NetTrek (Yes/No)", "Yes"),
    ("Willing to attend finance conference (Yes/No)", "Yes"),
    ("Willing to mentor (Yes/No)", "Yes"),
    ("Willing to sponsor company event (Yes/No)", "No"),
    ("Willing to guest speak (Yes/No)", "Yes"),
    ("Willing to help at events (Yes/No)", "No"),
    ("Willing to host case competition (yes/no)", "No"),
    ("Willing to mentor — Women in Finance (Yes/No)", "No"),
    ("Hired a finance intern (Yes/No)", "Yes"),
    ("Hired finance full-time (Yes/No)", "No"),
    ("Willing to be a PIFF donor (Yes/No)", "Yes"),
    ("CFP designation (Yes/No)", "CFP Level 1"),
    ("CFA designation (Yes/No)", "CFA all 3 levels"),
    ("Other Designations:", "Series 7, Series 63"),
    ("Engagement notes", "Hosts NetTrek in NYC; active IB-track mentor."),
    ("Best Contact", "james.doe@example.com"),
]

# "Friends of the finance program" (#294) are non-alumni contacts
# (``is_alumni = false``) imported through the SAME pipeline as alumni. Their
# intake template is a CURATED SUBSET of the alumni columns: a friend's identity
# is satisfied by name alone, so the alumni-only academic fields (BYU ID / Net
# ID, graduation year/month, finance program year, graduate degree, and the whole
# education block) plus the spouse-link fields are dropped. The contact, current
# employment, and program-engagement columns are kept — a "friend" (e.g. a
# recruiter or employer partner) still has an employer, contact info, and can be
# willing to host events / hire / be a PIFF donor.
_FRIEND_EXCLUDED_HEADERS: frozenset[str] = frozenset(
    {
        # Alumni/OneAccord identity + academic + finance-society leadership are
        # alumni-only, so they are dropped from the friend intake set too. Header
        # text matches the finalized 64-column set exactly.
        "MSTID (from OneAccord)",
        "BYU ID (9 digits)",
        "Net ID",
        "Birthday (YYYY-MM-DD)",
        "Graduation Semester",
        "Graduation Year",
        "Class of",
        "Finance program admitted year",
        "Finance Leadership Position",
        "Graduate degree",
        "Graduate university",
        "Graduate graduation year",
        "Deceased? (Yes/No)",
        "Spouse Name",
        "Degree",
        "Major",
        "Degree status",
        "Degree year",
    }
)

_FRIEND_COLUMNS: list[tuple[str, str]] = [
    col for col in _ALUMNI_COLUMNS if col[0] not in _FRIEND_EXCLUDED_HEADERS
]


def friend_columns() -> list[tuple[str, str]]:
    """The curated friend intake columns (header, example), in template order."""
    return list(_FRIEND_COLUMNS)


_EVENT_COLUMNS: list[tuple[str, str]] = [
    ("Event name", "Spring NetTrek"),
    ("Event type", "Net Trek"),
    ("Event date (YYYY-MM-DD)", "2026-03-12"),
    ("Location", "New York, NY"),
    ("Notes", "Annual NYC finance trek."),
]

# Attendance form / survey layout: who attended which event. Identify the
# alumnus by BYU ID (preferred) or name, and the event by its name on the Events
# sheet. Status options: Registered, Attended, No Show, Cancelled.
_ATTENDANCE_COLUMNS: list[tuple[str, str]] = [
    ("Event name", "Spring NetTrek"),
    ("Event date (YYYY-MM-DD)", "2026-03-12"),
    ("Attendee BYU ID", "001000001"),
    ("Attendee first name", "James"),
    ("Attendee last name", "Doe"),
    ("Attendance status (Registered/Attended/No Show/Cancelled)", "Attended"),
    ("Notes", ""),
]

# Reference sheet: documents the only constrained values the import path checks,
# plus the formats staff should enter so the data is import-ready. Two columns
# (Field, Allowed values / format); built partly from the live INDUSTRIES list.
_MAX_YEAR = datetime.date.today().year + 10
_REFERENCE_ROWS: list[list[str]] = [
    ["Field / column", "Allowed values & format"],
    [
        "Current industry, Secondary industry",
        "Must be EXACTLY one of: " + ", ".join(INDUSTRIES),
    ],
    [
        "Attendance status (Event attendance sheet)",
        ", ".join(_ATTENDANCE_STATUSES),
    ],
    ["All Yes/No columns", "Enter Yes or No"],
    ["All date columns (birthday, event date)", "Format YYYY-MM-DD, e.g. 1990-03-15"],
    ["Birthday (YYYY-MM-DD)", "A real date in the past (1900 or later)"],
    [
        "Graduation Year, Finance program admitted year, Degree year",
        f"4-digit year between 1950 and {_MAX_YEAR}",
    ],
    ["BYU ID, Attendee BYU ID", "Exactly 9 digits, e.g. 001000001"],
    ["Net ID", "2-12 lowercase letters/numbers, e.g. jdoe1"],
    [
        "LinkedIn URL",
        "Full https://www.linkedin.com/... URL (must be a linkedin.com address)",
    ],
    [
        "Event name (Event attendance sheet)",
        "Must match an Event name on the Events sheet exactly.",
    ],
    [
        "Names, employer, title, address, notes, etc.",
        "Free text. Names may not contain ; = < > | characters.",
    ],
]

# Each sheet: (display name, kind, payload).
#   kind "form" -> payload is [(header, example), ...]: header row + 1 example
#                  row + blank rows to fill in.
#   kind "rows" -> payload is [[cell, ...], ...]: written verbatim, no blanks.
SHEETS: list[tuple[str, str, object]] = [
    ("Alumni", "form", _ALUMNI_COLUMNS),
    ("Events", "form", _EVENT_COLUMNS),
    ("Event attendance", "form", _ATTENDANCE_COLUMNS),
    ("Reference (allowed values)", "rows", _REFERENCE_ROWS),
]


# --- Minimal .xlsx writer (stdlib only) --------------------------------------


def _col_letter(n: int) -> str:
    """1-indexed column number -> spreadsheet column letters (1->A, 27->AA)."""
    letters = ""
    while n > 0:
        n, rem = divmod(n - 1, 26)
        letters = chr(65 + rem) + letters
    return letters


def _cell(ref: str, value: str) -> str:
    # Inline string cell — avoids a shared-strings table. xml:space=preserve so
    # leading/trailing spaces survive.
    return (
        f'<c r="{ref}" t="inlineStr"><is>'
        f'<t xml:space="preserve">{escape(value)}</t></is></c>'
    )


def _sheet_xml(rows: list[list[str]]) -> str:
    """Worksheet XML for arbitrary rows (list of cell-value lists)."""
    rows_xml: list[str] = []
    for r, values in enumerate(rows, start=1):
        cells = "".join(
            _cell(f"{_col_letter(c + 1)}{r}", values[c]) for c in range(len(values))
        )
        rows_xml.append(f'<row r="{r}">{cells}</row>')
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        f"<sheetData>{''.join(rows_xml)}</sheetData>"
        "</worksheet>"
    )


def _form_rows(columns: list[tuple[str, str]], blank_rows: int) -> list[list[str]]:
    """Header row + one example row + N blank rows to fill in."""
    headers = [h for h, _ in columns]
    example = [v for _, v in columns]
    return [headers, example] + [[""] * len(headers) for _ in range(blank_rows)]


def _content_types() -> str:
    overrides = "".join(
        f'<Override PartName="/xl/worksheets/sheet{i + 1}.xml" '
        'ContentType="application/vnd.openxmlformats-officedocument.'
        'spreadsheetml.worksheet+xml"/>'
        for i in range(len(SHEETS))
    )
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'  # noqa: E501
        '<Default Extension="xml" ContentType="application/xml"/>'
        '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'  # noqa: E501
        f"{overrides}</Types>"
    )


def _root_rels() -> str:
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'  # noqa: E501
        '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>'  # noqa: E501
        "</Relationships>"
    )


def _workbook_xml() -> str:
    sheets = "".join(
        f'<sheet name="{escape(sheet[0])}" sheetId="{i + 1}" r:id="rId{i + 1}"/>'
        for i, sheet in enumerate(SHEETS)
    )
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
        'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
        f"<sheets>{sheets}</sheets></workbook>"
    )


def _workbook_rels() -> str:
    rels = "".join(
        f'<Relationship Id="rId{i + 1}" '
        'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" '
        f'Target="worksheets/sheet{i + 1}.xml"/>'
        for i in range(len(SHEETS))
    )
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        f"{rels}</Relationships>"
    )


def write_workbook(out_path: Path, blank_rows: int) -> None:
    with zipfile.ZipFile(out_path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", _content_types())
        z.writestr("_rels/.rels", _root_rels())
        z.writestr("xl/workbook.xml", _workbook_xml())
        z.writestr("xl/_rels/workbook.xml.rels", _workbook_rels())
        for i, (_, kind, payload) in enumerate(SHEETS):
            rows = (
                _form_rows(payload, blank_rows) if kind == "form" else list(payload)
            )
            z.writestr(f"xl/worksheets/sheet{i + 1}.xml", _sheet_xml(rows))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--out", type=Path, default=_DEFAULT_OUT, help="Output .xlsx path."
    )
    parser.add_argument(
        "--blank-rows",
        type=int,
        default=20,
        help="Empty rows to append after each example row (default 20).",
    )
    args = parser.parse_args()
    write_workbook(args.out, args.blank_rows)
    summary = ", ".join(
        f"{name} ({len(payload)} {'cols' if kind == 'form' else 'rows'})"
        for name, kind, payload in SHEETS
    )
    print(f"Wrote intake workbook: {args.out}\nSheets: {summary}")


if __name__ == "__main__":
    main()
