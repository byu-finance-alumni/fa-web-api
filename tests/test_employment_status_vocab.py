""""Unknown" is the eighth employment status (#377) — everywhere but the survey.

Jake's 2026-08-04 prod cleanup consolidated the misspelled ``unkown`` / ``UNKOWN``
rows onto the literal ``Unknown``, so ~65 live alumni now hold a value the app did
not offer. These tests pin the consequences of making it a first-class option:

* the canonical tuple, the doc (``database/dropdowns.md``) and the API's own
  filter description all agree on the same eight values, in the same order;
* a write path (create / edit / CSV import) accepts ``Unknown`` without a 422 —
  the whole reason this is urgent;
* the SURVEY list is the canonical list MINUS ``Unknown``, because "unknown" is
  meaningless as a self-description.

Offline: schemas and markdown are exercised as plain objects/text, no database.
"""

from __future__ import annotations

import csv
import io
import re
from pathlib import Path

import pytest

from app.core.dropdowns import (
    EMPLOYMENT_STATUS_PLACEHOLDERS,
    EMPLOYMENT_STATUSES,
    SURVEY_EMPLOYMENT_STATUSES,
)
from app.schemas.alumni import AlumniCreate, AlumniUpdate
from app.services import import_csv

DROPDOWNS_MD = Path(__file__).resolve().parents[1] / "database" / "dropdowns.md"

# The seven that shipped in #568, frozen verbatim: #377 only APPENDS to them.
_SEVEN_BEFORE_377 = (
    "Full-time",
    "Part-time",
    "Self-Employed",
    "Graduate Student",
    "Military",
    "Not in the Labor Force",
    "Unemployed",
)


# --- the canonical list ------------------------------------------------------


def test_employment_statuses_are_the_seven_plus_unknown() -> None:
    assert EMPLOYMENT_STATUSES == (*_SEVEN_BEFORE_377, "Unknown")


def test_unknown_is_pinned_last() -> None:
    """Order is Tanya's (#568), not alphabetical; "Unknown" is appended rather
    than sorted in, mirroring how it is pinned in ``INDUSTRIES``."""
    assert EMPLOYMENT_STATUSES[-1] == "Unknown"


def test_the_original_seven_kept_their_order() -> None:
    """Guard against a "helpful" alphabetization: the dashboard and the survey
    both read this order."""
    assert EMPLOYMENT_STATUSES[:-1] == _SEVEN_BEFORE_377


def test_no_duplicates() -> None:
    assert len(set(EMPLOYMENT_STATUSES)) == len(EMPLOYMENT_STATUSES)


@pytest.mark.parametrize("value", EMPLOYMENT_STATUSES)
def test_every_status_fits_the_varchar_50_column(value: str) -> None:
    assert len(value) <= 50


# --- the survey excludes it --------------------------------------------------


def test_survey_list_omits_unknown() -> None:
    """"Unknown" is meaningless as a SELF-description — offering it back to an
    alum re-collects the non-answer the survey exists to clear."""
    assert "Unknown" not in SURVEY_EMPLOYMENT_STATUSES


def test_survey_list_is_the_canonical_list_minus_the_placeholders() -> None:
    """Derived, never hand-typed: a ninth status must reach the survey without
    anyone remembering to update a second list."""
    assert SURVEY_EMPLOYMENT_STATUSES == tuple(
        v for v in EMPLOYMENT_STATUSES if v not in EMPLOYMENT_STATUS_PLACEHOLDERS
    )
    assert SURVEY_EMPLOYMENT_STATUSES == _SEVEN_BEFORE_377


def test_survey_list_preserves_the_canonical_order() -> None:
    assert list(SURVEY_EMPLOYMENT_STATUSES) == [
        v for v in EMPLOYMENT_STATUSES if v in set(SURVEY_EMPLOYMENT_STATUSES)
    ]


def test_placeholders_are_real_options_not_a_parallel_vocabulary() -> None:
    """The placeholder set narrows the survey; it does NOT put "Unknown" outside
    the canonical list. Storable/filterable everywhere, just not self-reportable."""
    for value in EMPLOYMENT_STATUS_PLACEHOLDERS:
        assert value in EMPLOYMENT_STATUSES


# --- nothing rejects it on write ---------------------------------------------


@pytest.mark.parametrize("value", EMPLOYMENT_STATUSES)
def test_create_schema_accepts_every_canonical_status(value: str) -> None:
    payload = AlumniCreate(first_name="Jane", last_name="Doe", employment_status=value)
    assert payload.employment_status == value


@pytest.mark.parametrize("value", EMPLOYMENT_STATUSES)
def test_update_schema_accepts_every_canonical_status(value: str) -> None:
    """The urgent case: ~65 prod alumni already hold "Unknown", and editing any
    unrelated field on them round-trips the stored status through this schema.
    An allow-list here would 422 every one of those profiles."""
    payload = AlumniUpdate(employment_status=value)
    assert payload.employment_status == value


@pytest.mark.parametrize(
    "value", ["Employed", "Stay at home parent", "Self-Employed Business Owner"]
)
def test_update_schema_still_accepts_off_list_legacy_values(value: str) -> None:
    """Unchanged by #377 and deliberately so — the column is free text, and
    anything already on file must stay editable."""
    assert AlumniUpdate(employment_status=value).employment_status == value


# --- CSV import --------------------------------------------------------------


def _csv_with_status(value: str) -> bytes:
    """One minimal importable row carrying *value* in "Employment Status"."""
    cells = {header: "" for header in import_csv.EXPECTED_HEADERS}
    cells["First name"] = "Jane"
    cells["Last Name"] = "Doe"
    cells["Employment Status"] = value
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(import_csv.EXPECTED_HEADERS)
    writer.writerow([cells[header] for header in import_csv.EXPECTED_HEADERS])
    return buf.getvalue().encode("utf-8-sig")


@pytest.mark.parametrize("value", EMPLOYMENT_STATUSES)
def test_import_accepts_every_canonical_status(value: str) -> None:
    rows, errors = import_csv.parse_and_map(_csv_with_status(value))
    assert errors == []
    assert rows[0]["payload"]["employment_status"] == value


def test_import_keeps_unknown_verbatim_rather_than_blanking_it() -> None:
    """"Unknown" is a RECORDED non-answer, not a missing value — the importer
    must not fold it into a blank the way it does for placeholder location /
    marital-status cells."""
    rows, _ = import_csv.parse_and_map(_csv_with_status("Unknown"))
    assert rows[0]["payload"].get("employment_status") == "Unknown"


# --- the doc is the source of truth and must agree ---------------------------

_BULLET_RE = re.compile(r"^-\s+(.+?)(\s*\*\([^)]+\)\*)?\s*$")


def _documented_statuses() -> list[str]:
    """Parse the ``## Employment Status`` option bullets from ``dropdowns.md``.

    Reads only the single unbroken run of bullets after that section's
    "Options:" line, mirroring ``tests/test_industry_vocab.py``.
    """
    lines = DROPDOWNS_MD.read_text(encoding="utf-8").splitlines()
    start = lines.index("## Employment Status")
    end = next(i for i in range(start + 1, len(lines)) if lines[i].strip() == "---")
    options_at = next(i for i in range(start, end) if lines[i].strip() == "Options:")

    found: list[str] = []
    for line in lines[options_at + 1 : end]:
        if not line.startswith("-"):
            break
        match = _BULLET_RE.match(line.strip())
        assert match is not None, f"unparseable option bullet: {line!r}"
        found.append(match.group(1).strip())
    return found


def test_doc_actually_lists_statuses() -> None:
    """Guard the parser: a silent miss would make the assertion below pass
    vacuously."""
    assert len(_documented_statuses()) == len(EMPLOYMENT_STATUSES)


def test_doc_matches_the_tuple_in_order() -> None:
    assert _documented_statuses() == list(EMPLOYMENT_STATUSES)


# --- the API's own description -----------------------------------------------


def test_filter_description_is_built_from_the_tuple() -> None:
    """``GET /alumni``'s ``employment_status`` help text hand-listed the seven
    and went stale the moment an eighth arrived. It is now derived — this asserts
    it, so the OpenAPI docs can't drift again."""
    from app.main import app

    schema = app.openapi()
    params = next(
        (
            operations["get"]["parameters"]
            for path, operations in schema["paths"].items()
            if path.rstrip("/").endswith("/alumni") and "get" in operations
        ),
        None,
    )
    assert params is not None, "GET /alumni not found in the OpenAPI schema"
    description = next(
        p["description"] for p in params if p["name"] == "employment_status"
    )
    for value in EMPLOYMENT_STATUSES:
        assert value in description
