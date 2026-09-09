"""``work-log.csv`` — the time record, and the one file that leaves this folder.

Jake opens this in Excel. That makes it the highest-risk artifact in the whole
system, for two unrelated reasons.

**Formula injection.** ``request_title`` is an email subject line: text a
stranger chose, landing in a spreadsheet cell. The same guard the alumni
exports use applies here (see ``tests/test_csv_formula_injection_sweep.py`` for
the house precedent), with one deliberate difference — this module prefixes an
apostrophe rather than a tab, because these rows are meant to be read by a
person in Excel and a leading tab is invisible noise in a narrow column.

**What must never be in it.** No email body. No email address. No attachment
content. The ``requester`` column is a DISPLAY NAME only; the address stays in
the request Markdown, which never leaves the folder. A CSV is the artifact most
likely to be mailed to somebody, and it must stay boring.

The arithmetic rule that matters:

    ⚠️ ``total_jake_minutes`` stays BLANK when no ``jake_*`` field is filled.

Writing ``0`` would say "he spent no time on this". Blank says "not recorded".
Those are different claims and only one of them is true.
"""

from __future__ import annotations

import csv
import datetime as dt
import pathlib

HEADER = [
    "request_id",
    "request_title",
    "requester",
    "date_received",
    "claude_started",
    "claude_finished",
    "claude_runtime_minutes",
    "jake_request_review_minutes",
    "jake_testing_minutes",
    "jake_correction_minutes",
    "jake_deployment_minutes",
    "total_jake_minutes",
    "status",
    "branch",
    "notes",
]

#: The four columns that add up to ``total_jake_minutes``.
JAKE_COLUMNS = (
    "jake_request_review_minutes",
    "jake_testing_minutes",
    "jake_correction_minutes",
    "jake_deployment_minutes",
)

#: Leading characters Excel and LibreOffice treat as the start of a formula.
_FORMULA_LEADS = ("=", "+", "-", "@", "\t", "\r")


def csv_safe(value: object) -> str:
    """Neutralise a cell so a spreadsheet renders it as text, not as code.

    Line breaks are flattened first, so a subject line cannot forge a second
    CSV row. The formula check then looks at the value with LEADING WHITESPACE
    IGNORED — ``" =HYPERLINK(...)"`` is the documented gap in the alumni-export
    guard (``tests/test_csv_formula_injection_sweep.py`` explains why it is
    unreachable there: every producer trims first). Nothing trims an email
    subject before it reaches this function, so the check is made here instead
    of assumed upstream.
    """
    text = "" if value is None else str(value)
    text = text.replace("\r\n", " ").replace("\n", " ").replace("\r", " ")
    if text.startswith(_FORMULA_LEADS) or text.lstrip().startswith(_FORMULA_LEADS):
        return "'" + text
    return text


def ensure(path: pathlib.Path) -> bool:
    """Create the log with its header if absent. Returns True if created."""
    if path.exists():
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        csv.writer(handle).writerow(HEADER)
    return True


def read(path: pathlib.Path) -> list[dict[str, str]]:
    if not path.is_file():
        return []
    with path.open("r", encoding="utf-8", newline="") as handle:
        return [dict(row) for row in csv.DictReader(handle)]


def write(path: pathlib.Path, rows: list[dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=HEADER, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({key: csv_safe(row.get(key, "")) for key in HEADER})


def blank_row() -> dict[str, str]:
    return dict.fromkeys(HEADER, "")


def has_row(path: pathlib.Path, request_id: str) -> bool:
    return any(row.get("request_id") == request_id for row in read(path))


def append(path: pathlib.Path, row: dict[str, str]) -> None:
    """Append one row. Refuses to duplicate a request id."""
    ensure(path)
    rows = read(path)
    if any(existing.get("request_id") == row.get("request_id") for existing in rows):
        return
    full = blank_row() | {key: value for key, value in row.items() if key in HEADER}
    with path.open("a", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=HEADER, extrasaction="ignore")
        writer.writerow({key: csv_safe(full.get(key, "")) for key in HEADER})


def update(path: pathlib.Path, request_id: str, fields: dict[str, str]) -> bool:
    """Patch one row in place. Returns False if the request id is not present."""
    rows = read(path)
    found = False
    for row in rows:
        if row.get("request_id") == request_id:
            row.update({key: value for key, value in fields.items() if key in HEADER})
            row["total_jake_minutes"] = total_jake_minutes(row)
            found = True
    if found:
        write(path, rows)
    return found


def _as_minutes(value: str) -> int | None:
    """Parse a cell that may carry the apostrophe guard, or be blank."""
    text = (value or "").strip().lstrip("'").strip()
    if not text:
        return None
    try:
        return int(round(float(text)))
    except ValueError:
        return None


def total_jake_minutes(row: dict[str, str]) -> str:
    """Sum of the recorded ``jake_*`` columns, or BLANK if none is recorded."""
    values = [_as_minutes(row.get(column, "")) for column in JAKE_COLUMNS]
    recorded = [value for value in values if value is not None]
    if not recorded:
        return ""
    return str(sum(recorded))


def runtime_minutes(started: str, finished: str) -> str:
    """Whole minutes between two ``YYYY-MM-DD HH:MM`` stamps, floored at 0."""
    fmt = "%Y-%m-%d %H:%M"
    try:
        start = dt.datetime.strptime((started or "").strip().lstrip("'"), fmt)
        end = dt.datetime.strptime((finished or "").strip().lstrip("'"), fmt)
    except ValueError:
        return ""
    return str(max(0, int(round((end - start).total_seconds() / 60))))
