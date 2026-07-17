"""Offline tests for the bulk CSV alumni importer (no database).

A small fake session (mirroring ``tests/test_alumni_service.py`` /
``tests/test_alumni_routes.py``) feeds queued query results so the importer's
parse/map/evaluate/commit stages are exercised without a real DATABASE_URL.

The DB identity index is loaded ONCE via ``session.execute`` returning an
``_index_result`` row set; per-row duplicate scalar/execute lookups are then
served from queued results.
"""

import asyncio
import uuid

import pytest
from fastapi.testclient import TestClient

from app.api.dependencies.auth import get_current_db_user
from app.core.database import get_session
from app.main import app
from app.schemas.auth import UserContext
from app.services import import_csv

# --- CSV building helpers ----------------------------------------------------

HEADERS = import_csv.EXPECTED_HEADERS


def _row_values(**overrides) -> list[str]:
    """A blank row keyed by payload-ish names mapped back to header positions."""
    # Start all-blank, then set named cells by header.
    values = {h: "" for h in HEADERS}
    by_field = {
        "byu_id": "BYU ID (9 digits)",
        "net_id": "Net ID",
        "first_name": "First name",
        "last_name": "Last Name",
        "graduation_year": "Graduation Year",
        "birth_date": "Birthday (YYYY-MM-DD)",
        "personal_email": "Personal Email",
        "current_employer": "Current employer",
        "current_industry": "Current industry (see Reference sheet)",
        "current_industry_secondary": "Secondary industry (see Reference sheet)",
        "linkedin_url": "LinkedIn URL",
        "address_line_1": "Address line 1",
        "address_line_2": "Address line 2",
        # The sheet's location block is the EMPLOYER's address (#287): these
        # cells bind to career.current_*, so they are keyed by that name here.
        "current_city": "Current city",
        "current_state": "Current state",
        "current_country": "Current country",
        "current_zip": "Current ZIP",
        "region": (
            "Region (Northeast, Southeast, Midwest, Southwest, West, and Mountain West)"
        ),
        "deceased": "Deceased? (Yes/No)",
        "mentor_willing": "Willing to mentor (Yes/No)",
        "cfp_designation": "CFP designation (Yes/No)",
        "cfa_designation": "CFA designation (Yes/No)",
    }
    for field, value in overrides.items():
        values[by_field[field]] = value
    return [values[h] for h in HEADERS]


FRIEND_HEADERS = import_csv.FRIEND_EXPECTED_HEADERS


def _friend_row_values(**overrides) -> list[str]:
    """A blank FRIEND row keyed by field name -> friend-template header."""
    values = {h: "" for h in FRIEND_HEADERS}
    by_field = {
        "first_name": "First name",
        "last_name": "Last Name",
        "gender": "Gender",
        "personal_email": "Personal Email",
        "current_employer": "Current employer",
        "mentor_willing": "Willing to mentor (Yes/No)",
    }
    for field, value in overrides.items():
        values[by_field[field]] = value
    return [values[h] for h in FRIEND_HEADERS]


def _csv_bytes(*rows: list[str], headers: list[str] | None = None) -> bytes:
    import csv
    import io

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(headers if headers is not None else HEADERS)
    for row in rows:
        writer.writerow(row)
    # utf-8-sig: include the BOM Excel writes, to prove decoding handles it.
    return buf.getvalue().encode("utf-8-sig")


# --- Fake session ------------------------------------------------------------


class _Scalars:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return list(self._rows)


class _ExecResult:
    def __init__(self, rows):
        self._rows = rows

    def scalars(self):
        return _Scalars(self._rows)

    def all(self):
        return list(self._rows)


class _Savepoint:
    """Minimal async-context savepoint mirroring ``session.begin_nested``.

    Records how many objects were ``add``-ed when it opened so a ``rollback``
    can discard exactly the rows staged inside this nested block — enough to
    exercise the per-row SAVEPOINT logic in ``commit_import`` offline.
    """

    def __init__(self, session):
        self._session = session
        self._mark = len(session.added)
        self.rolled_back = False

    async def __aenter__(self):
        self._mark = len(self._session.added)
        return self

    async def __aexit__(self, exc_type, exc, tb):
        # commit_import catches its own exceptions and calls rollback itself, so
        # nothing should propagate; just don't swallow anything unexpectedly.
        return False

    async def rollback(self):
        self.rolled_back = True
        self._session.savepoint_rollbacks += 1
        del self._session.added[self._mark :]


class FakeSession:
    """Serves the one-time identity index plus queued per-row results.

    ``index_rows`` is the row tuple list returned by the FIRST ``execute`` (the
    batch identity load). Subsequent ``execute`` calls (fuzzy dup scan) return
    ``execute_rows`` entries; ``scalar`` calls (exact byu/net dup, spouse
    resolve) return ``scalars`` entries.

    ``fail_on_add`` optionally raises from ``add`` when a staged Alumni's
    ``first_name`` matches, to simulate a row blowing up mid-batch so the
    savepoint/rollback path can be exercised.
    """

    def __init__(
        self, index_rows=(), scalars=(), execute_rows=(), fail_on_add=None
    ):
        self._index_rows = list(index_rows)
        self._index_served = False
        self._scalars = list(scalars)
        self._execute_rows = list(execute_rows)
        self.added = []
        self.committed = 0
        self.savepoint_rollbacks = 0
        self._fail_on_add = fail_on_add

    def add(self, obj):
        if (
            self._fail_on_add is not None
            and getattr(obj, "first_name", None) == self._fail_on_add
        ):
            raise RuntimeError("simulated DB error: relation does not exist")
        self.added.append(obj)

    def begin_nested(self):
        return _Savepoint(self)

    async def scalar(self, stmt):
        return self._scalars.pop(0) if self._scalars else None

    async def execute(self, stmt):
        if not self._index_served:
            self._index_served = True
            return _ExecResult(self._index_rows)
        return _ExecResult(self._execute_rows.pop(0) if self._execute_rows else [])

    async def flush(self):
        for obj in self.added:
            if getattr(obj, "alumni_id", None) is None:
                obj.alumni_id = 100 + self.added.index(obj)

    async def commit(self):
        self.committed += 1

    async def refresh(self, obj):
        pass


def _run(coro):
    return asyncio.run(coro)


# --- Header validation -------------------------------------------------------


def test_header_validation_missing_and_unknown():
    bad_headers = ["First name", "Last name", "Bogus column"]
    rows, errors = import_csv.parse_and_map(
        _csv_bytes(["Jane", "Doe", "x"], headers=bad_headers)
    )
    assert rows == []
    assert any("Missing required column" in e for e in errors)
    assert any("Unexpected column" in e for e in errors)


def test_header_validation_passes_for_template():
    rows, errors = import_csv.parse_and_map(_csv_bytes())  # header only
    assert errors == []
    assert rows == []  # no data rows


def test_duplicate_header_rejected():
    # A repeated column would silently last-wins-overwrite data (#168).
    headers = import_csv.EXPECTED_HEADERS + ["First name"]
    rows, errors = import_csv.parse_and_map(
        _csv_bytes(["x"] * len(headers), headers=headers)
    )
    assert rows == []
    assert any("Duplicate column" in e for e in errors)


# --- Legacy header aliases ---------------------------------------------------
#
# Renaming a template column must not invalidate sheets staff already filled in
# under the old name. The Region header enumerates the regions, so adding
# "Mountain West" (2026-07-16) renamed it — both spellings must import.

_OLD_REGION_HEADER = "Region (Northeast, Southeast, Midwest, Southwest, and West)"
_NEW_REGION_HEADER = (
    "Region (Northeast, Southeast, Midwest, Southwest, West, and Mountain West)"
)


def _headers_with_old_region() -> list[str]:
    return [
        _OLD_REGION_HEADER if h == _NEW_REGION_HEADER else h
        for h in import_csv.EXPECTED_HEADERS
    ]


def test_current_region_header_imports():
    row = _row_values(first_name="Jane", last_name="Doe")
    row[HEADERS.index(_NEW_REGION_HEADER)] = "Mountain West"
    rows, errors = import_csv.parse_and_map(_csv_bytes(row))
    assert errors == []
    assert rows[0]["payload"]["contact"]["region"] == "Mountain West"


def test_legacy_region_header_still_imports():
    # A sheet downloaded before the rename: same column, old caption. It must
    # import — not fail as "Missing required column" + "Unexpected column".
    headers = _headers_with_old_region()
    row = _row_values(first_name="Jane", last_name="Doe")
    row[headers.index(_OLD_REGION_HEADER)] = "West"
    rows, errors = import_csv.parse_and_map(_csv_bytes(row, headers=headers))
    assert errors == []
    assert rows[0]["payload"]["contact"]["region"] == "West"


def test_legacy_region_header_maps_to_the_same_field_as_the_current_one():
    row = _row_values(first_name="Jane", last_name="Doe")
    old_headers = _headers_with_old_region()
    old_row = list(row)
    old_row[old_headers.index(_OLD_REGION_HEADER)] = "Southeast"
    new_row = list(row)
    new_row[HEADERS.index(_NEW_REGION_HEADER)] = "Southeast"

    from_old, _ = import_csv.parse_and_map(_csv_bytes(old_row, headers=old_headers))
    from_new, _ = import_csv.parse_and_map(_csv_bytes(new_row))
    assert from_old[0]["payload"] == from_new[0]["payload"]


def test_both_region_headers_in_one_sheet_is_a_duplicate_column():
    # They are the same column under two captions, so accepting both would
    # silently last-wins one of them.
    headers = import_csv.EXPECTED_HEADERS + [_OLD_REGION_HEADER]
    rows, errors = import_csv.parse_and_map(
        _csv_bytes(["x"] * len(headers), headers=headers)
    )
    assert rows == []
    assert any("Duplicate column" in e for e in errors)


def test_aliases_are_renames_only_and_never_widen_the_template():
    # The alias table maps retired spellings ONTO current headers; a typo'd
    # target would make a real column unreachable, and the template itself must
    # keep exactly one caption per column.
    assert set(import_csv._LEGACY_HEADER_ALIASES.values()) <= set(
        import_csv.EXPECTED_HEADERS
    )
    assert not set(import_csv._LEGACY_HEADER_ALIASES) & set(
        import_csv.EXPECTED_HEADERS
    )
    # Retired captions are NOT mapping keys — the importer stays keyed solely by
    # the current template headers (see test_expected_headers_are_the_finalized_set).
    assert not set(import_csv._LEGACY_HEADER_ALIASES) & set(import_csv._MAPPING)


def test_template_download_serves_only_the_current_region_header():
    csv_text = import_csv.build_template_csv()
    assert _NEW_REGION_HEADER in csv_text
    assert _OLD_REGION_HEADER not in csv_text


# --- Row-cap + decoding guards -----------------------------------------------


def test_too_many_rows_rejected():
    many = [
        _row_values(first_name=f"P{i}", last_name="X") for i in range(5)
    ]
    csv = _csv_bytes(*many)
    rows, errors = import_csv.parse_and_map(csv, max_rows=3)
    assert rows == []
    assert errors
    assert "3-row import limit" in errors[0]


def test_row_cap_default_constant():
    assert import_csv.MAX_IMPORT_ROWS == 2000
    # 4 MiB, kept under Vercel's ~4.5 MB Function body ceiling (#170).
    assert import_csv.MAX_UPLOAD_BYTES == 4 * 1024 * 1024


def test_non_utf8_file_reports_friendly_error():
    # 0x80 is invalid as a leading UTF-8 byte -> decode fails gracefully.
    bad = b"First name,Last name\n\x80\x80,Doe\n"
    rows, errors = import_csv.parse_and_map(bad)
    assert rows == []
    assert errors
    assert "UTF-8" in errors[0]


def test_existing_index_normalizes_formatted_byu_id():
    # A stored formatted id "123-456-789" must collide with incoming "123456789".
    # Rows are (id, byu_id, net_id, first, last, grad_year, archived).
    index = [(7, "123-456-789", None, "Jane", "Doe", 2018, False)]
    idx = _run(import_csv._load_existing_index(FakeSession(index_rows=index)))
    assert idx["active_byu"] == {"123456789": (7, "Jane", "Doe")}


# --- Clean row imports -------------------------------------------------------


def test_clean_rows_are_importable():
    csv = _csv_bytes(
        _row_values(
            byu_id="123456789",
            first_name="Jane",
            last_name="Doe",
            graduation_year="2018",
            personal_email="jane@example.com",
            current_employer="Goldman Sachs",
        )
    )
    rows, errors = import_csv.parse_and_map(csv)
    assert errors == []
    report = _run(import_csv.evaluate(FakeSession(), rows))
    assert report["columns_ok"] is True
    assert report["summary"]["total"] == 1
    assert report["summary"]["importable"] == 1
    assert report["summary"]["rejected"] == 0
    assert report["rows"][0]["status"] == "importable"
    assert report["rows"][0]["error"] is None


# --- Dirty row gets cleaned (diffs reported) ---------------------------------


def test_dirty_row_is_cleaned_with_changes():
    csv = _csv_bytes(
        _row_values(
            byu_id="123456789",
            first_name="JANE",
            last_name="doe",
            graduation_year="2018",
            personal_email="JANE@EXAMPLE.COM",
            current_employer="Goldman Sachs",
        )
    )
    rows, _ = import_csv.parse_and_map(csv)
    report = _run(import_csv.evaluate(FakeSession(), rows))
    row = report["rows"][0]
    assert row["status"] == "importable"
    changed = {(c["section"], c["field"]) for c in row["changes"]}
    assert ("core", "first_name") in changed
    assert ("contact", "personal_email") in changed
    assert report["summary"]["cleaned"] == 1


# --- Free-text CFP/CFA/CPA designations (formerly boolean) -------------------


def test_free_text_designation_imports_and_persists_as_string():
    # CFP/CFA/CPA are free-text varchar(100), not Yes/No booleans: a value like
    # "CFP Level 1" must survive mapping verbatim into the engagement payload
    # (under the old bool coercion this cell would have been rejected).
    csv = _csv_bytes(
        _row_values(
            byu_id="123456789",
            first_name="Jane",
            last_name="Doe",
            graduation_year="2018",
            personal_email="jane@example.com",
            cfp_designation="CFP Level 1",
            cfa_designation="CFA all 3 levels",
        )
    )
    rows, errors = import_csv.parse_and_map(csv)
    assert errors == []
    assert rows[0]["error"] is None
    engagement = rows[0]["payload"]["engagement"]
    assert engagement["cfp_designation"] == "CFP Level 1"
    assert engagement["cfa_designation"] == "CFA all 3 levels"


def test_designation_schema_round_trip_returns_string():
    # EngagementCreate accepts free-text designations and ProgramEngagementRead
    # serializes them back as the stored string (true -> label, never bool).
    from app.schemas.alumni import EngagementCreate
    from app.schemas.profile import ProgramEngagementRead

    created = EngagementCreate(
        cfp_designation="CFP Level 1",
        cfa_designation="CFA all 3 levels",
        cpa_designation="CPA (Utah)",
    )
    assert created.cfp_designation == "CFP Level 1"
    assert created.cfa_designation == "CFA all 3 levels"
    assert created.cpa_designation == "CPA (Utah)"
    # Unset designation defaults to None (no cert), not False.
    assert EngagementCreate().cfp_designation is None

    read = ProgramEngagementRead.model_validate(
        {
            "engagement_profile_id": 1,
            "nettrek_host_willing": False,
            "finance_conference_willing": False,
            "mentor_willing": False,
            "company_event_sponsor_willing": False,
            "guest_speaker_willing": False,
            "help_at_event_willing": False,
            "case_competition_host_willing": False,
            "women_in_finance_mentor_willing": False,
            "hired_finance_intern": False,
            "hired_finance_full_time": False,
            "piff_donor": False,
            "cfp_designation": "CFP Level 1",
            "cfa_designation": "CFA all 3 levels",
            "cpa_designation": None,
        }
    )
    assert read.cfp_designation == "CFP Level 1"
    assert read.cfa_designation == "CFA all 3 levels"
    assert read.cpa_designation is None


# --- Invalid industry / bad date -> rejected ---------------------------------


def test_invalid_industry_rejected():
    csv = _csv_bytes(
        _row_values(
            first_name="Jane",
            last_name="Doe",
            current_industry="Underwater Basket Weaving",
        )
    )
    rows, _ = import_csv.parse_and_map(csv)
    report = _run(import_csv.evaluate(FakeSession(), rows))
    row = report["rows"][0]
    assert row["status"] == "rejected"
    assert row["error"] is not None
    assert "industry" in row["error"].lower()


@pytest.mark.parametrize("placeholder", ["unknown", "Unknown", "UNKNOWN", "n/a", "N/A", "na"])
def test_industry_placeholder_maps_to_other(placeholder):
    # Real intake sheets use these tokens for "no known industry" — map to Other
    # instead of rejecting the row (import-only leniency).
    assert import_csv._coerce_industry("Current industry", placeholder) == "Other"


def test_industry_placeholder_row_imports():
    csv = _csv_bytes(
        _row_values(first_name="Jane", last_name="Doe", current_industry="unknown")
    )
    rows, _ = import_csv.parse_and_map(csv)
    report = _run(import_csv.evaluate(FakeSession(), rows))
    row = report["rows"][0]
    assert row["status"] != "rejected"
    assert row["error"] is None


def test_secondary_industry_is_free_text():
    # Secondary industry is open response — a value outside the controlled vocab
    # is accepted and kept as-is (unlike the primary industry dropdown).
    csv = _csv_bytes(
        _row_values(
            first_name="Jane", last_name="Doe", current_industry_secondary="Insurance"
        )
    )
    rows, _ = import_csv.parse_and_map(csv)
    report = _run(import_csv.evaluate(FakeSession(), rows))
    assert report["rows"][0]["status"] != "rejected"
    assert rows[0]["payload"]["career"]["current_industry_secondary"] == "Insurance"


@pytest.mark.parametrize("token", ["unknown", "Unknown", "n/a", "N/A", "na"])
def test_secondary_industry_placeholder_blanked(token):
    csv = _csv_bytes(
        _row_values(
            first_name="Jane", last_name="Doe", current_industry_secondary=token
        )
    )
    rows, _ = import_csv.parse_and_map(csv)
    assert rows[0]["error"] is None
    assert "current_industry_secondary" not in rows[0]["payload"].get("career", {})


@pytest.mark.parametrize("token", ["unknown", "Unknown", "n/a", "N/A", "na"])
def test_location_placeholder_left_blank(token):
    # A placeholder in an address/location cell is stored blank, not literally.
    # The work-location cells land on career (#287), so the placeholder-blanking
    # must follow them there — it is keyed by the payload field name.
    csv = _csv_bytes(
        _row_values(
            first_name="Jane",
            last_name="Doe",
            current_city=token,
            current_state=token,
            current_country=token,
            current_zip=token,
            address_line_1=token,
        )
    )
    rows, _ = import_csv.parse_and_map(csv)
    assert rows[0]["error"] is None
    career = rows[0]["payload"].get("career", {})
    assert "current_city" not in career
    assert "current_state" not in career
    assert "current_country" not in career
    assert "current_zip" not in career
    assert "address_line_1" not in rows[0]["payload"].get("contact", {})


@pytest.mark.parametrize("token", ["unknown", "Unknown", "n/a", "N/A", "na"])
def test_linkedin_placeholder_left_blank(token):
    csv = _csv_bytes(
        _row_values(first_name="Jane", last_name="Doe", linkedin_url=token)
    )
    rows, _ = import_csv.parse_and_map(csv)
    assert rows[0]["error"] is None
    assert "linkedin_url" not in rows[0]["payload"]


def test_real_linkedin_still_kept():
    csv = _csv_bytes(
        _row_values(
            first_name="Jane",
            last_name="Doe",
            linkedin_url="https://linkedin.com/in/jane",
        )
    )
    rows, _ = import_csv.parse_and_map(csv)
    assert rows[0]["payload"]["linkedin_url"] == "https://linkedin.com/in/jane"


def test_real_city_still_kept():
    csv = _csv_bytes(
        _row_values(first_name="Jane", last_name="Doe", current_city="Provo",
                    current_state="UT")
    )
    rows, _ = import_csv.parse_and_map(csv)
    assert rows[0]["payload"]["career"]["current_city"] == "Provo"
    # parse_and_map only maps raw cells; hygiene cleaning (which expands "UT" ->
    # "Utah") runs in the later preview/write stage, so the raw value is kept.
    assert rows[0]["payload"]["career"]["current_state"] == "UT"


# --- The location block is the EMPLOYER's address (#287) ----------------------


def test_work_location_binds_to_the_employment_record():
    """The sheet's "Current city/state/ZIP/country" block is the EMPLOYER's
    address (it sits immediately after the employment columns and Tanya fills in
    where the alum WORKS), so it binds to current_employment — not to the
    residence row, which nothing in this system populates."""
    csv = _csv_bytes(
        _row_values(
            first_name="Jane",
            last_name="Doe",
            current_city="Provo",
            current_state="UT",
            current_country="USA",
            current_zip="84604",
        )
    )
    rows, _ = import_csv.parse_and_map(csv)
    payload = rows[0]["payload"]

    assert payload["career"]["current_city"] == "Provo"
    assert payload["career"]["current_state"] == "UT"
    assert payload["career"]["current_country"] == "USA"
    assert payload["career"]["current_zip"] == "84604"

    # And NOT onto the residence record.
    contact = payload.get("contact", {})
    for residence_field in ("city", "state", "country", "zip"):
        assert residence_field not in contact


def test_no_contact_to_career_mirror_runs():
    """The old contact->career location mirror is gone (#287). With the columns
    bound straight to career.*, a blank work location must stay honestly blank
    rather than being back-filled from the residence row."""
    csv = _csv_bytes(_row_values(first_name="Jane", last_name="Doe"))
    rows, _ = import_csv.parse_and_map(csv)
    payload = rows[0]["payload"]
    # No location cells filled -> no career location keys invented.
    career = payload.get("career", {})
    for field in ("current_city", "current_state", "current_country", "current_zip"):
        assert field not in career


def test_address_lines_still_bind_to_contact():
    """Address line 1/2 are the one column pair whose destination is still being
    decided (#287), so they stay on the contact record exactly as before. This
    test is the tripwire: it must be a deliberate decision to move them."""
    csv = _csv_bytes(
        _row_values(
            first_name="Jane",
            last_name="Doe",
            address_line_1="200 West St",
            address_line_2="Floor 4",
        )
    )
    rows, _ = import_csv.parse_and_map(csv)
    contact = rows[0]["payload"]["contact"]
    assert contact["address_line_1"] == "200 West St"
    assert contact["address_line_2"] == "Floor 4"
    assert "address_line_1" not in rows[0]["payload"].get("career", {})


def test_region_still_binds_to_contact():
    """Region is not an address — it's a US bucket derived from the work state
    (#283) that physically lives on the contact row. The rebind must not move
    it."""
    csv = _csv_bytes(
        _row_values(first_name="Jane", last_name="Doe", region="West")
    )
    rows, _ = import_csv.parse_and_map(csv)
    assert rows[0]["payload"]["contact"]["region"] == "West"
    assert "region" not in rows[0]["payload"].get("career", {})


def test_home_country_untouched_by_the_rebind():
    """"Home country" is the country of ORIGIN (about the alum), not part of the
    employer address block — it stays on the core record."""
    values = {h: "" for h in HEADERS}
    values["First name"] = "Jane"
    values["Last Name"] = "Doe"
    values["Home country"] = "Brazil"
    values["Current country"] = "USA"
    rows, _ = import_csv.parse_and_map(_csv_bytes([values[h] for h in HEADERS]))
    payload = rows[0]["payload"]
    assert payload["home_country"] == "Brazil"
    # The work country is a separate, unrelated value on the employment record.
    assert payload["career"]["current_country"] == "USA"


def test_old_region_caption_still_imports_after_rebind():
    """A sheet Tanya downloaded before the Mountain West rename must keep working
    — the rebind changed bindings, not captions, so the legacy alias still
    canonicalizes and the moved columns still land on career."""
    legacy_headers = [
        "Region (Northeast, Southeast, Midwest, Southwest, and West)"
        if h.startswith("Region (")
        else h
        for h in HEADERS
    ]
    values = {h: "" for h in HEADERS}
    values["First name"] = "Jane"
    values["Last Name"] = "Doe"
    values["Current city"] = "Provo"
    values["Region (Northeast, Southeast, Midwest, Southwest, West, and Mountain West)"] = "West"
    row = [values[h] for h in HEADERS]

    rows, errors = import_csv.parse_and_map(
        _csv_bytes(row, headers=legacy_headers)
    )

    assert errors == []
    payload = rows[0]["payload"]
    assert payload["contact"]["region"] == "West"
    assert payload["career"]["current_city"] == "Provo"


def test_bad_date_rejected():
    csv = _csv_bytes(
        _row_values(first_name="Jane", last_name="Doe", birth_date="not a real date")
    )
    rows, _ = import_csv.parse_and_map(csv)
    report = _run(import_csv.evaluate(FakeSession(), rows))
    row = report["rows"][0]
    assert row["status"] == "rejected"
    assert "date" in row["error"].lower()


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("2002-03-03", "2002-03-03"),  # ISO (unchanged)
        ("2002-3-3", "2002-03-03"),    # unpadded ISO
        ("3-Mar-02", "2002-03-03"),    # the reported real-world case
        ("3-Mar-2002", "2002-03-03"),
        ("3 Mar 2002", "2002-03-03"),
        ("March 3, 2002", "2002-03-03"),
        ("03/15/1990", "1990-03-15"),  # US m/d/Y (was previously rejected)
        ("03/15/90", "1990-03-15"),    # 2-digit year pivots to 1990
    ],
)
def test_flexible_date_formats_normalized_to_iso(raw, expected):
    assert import_csv._coerce_date("Birthday (YYYY-MM-DD)", raw) == expected


def test_unparseable_date_still_raises():
    with pytest.raises(import_csv._CellError):
        import_csv._coerce_date("Birthday (YYYY-MM-DD)", "13/45/1990")


def test_bad_int_rejected():
    csv = _csv_bytes(
        _row_values(
            first_name="Jane", last_name="Doe", graduation_year="two thousand"
        )
    )
    rows, _ = import_csv.parse_and_map(csv)
    report = _run(import_csv.evaluate(FakeSession(), rows))
    assert report["rows"][0]["status"] == "rejected"


# --- Exact duplicate vs DB -> rejected blocker -------------------------------


def test_exact_duplicate_vs_db_rejected():
    # Existing index has byu 123456789 -> alumni_id 7. Duplicate detection now
    # runs in-memory off the preloaded index (no per-row scalar), so the index
    # row alone produces the blocker. Rows are
    # (id, byu_id, net_id, first, last, grad_year, archived).
    index = [(7, "123456789", None, "Jane", "Doe", 2018, False)]
    session = FakeSession(index_rows=index)
    csv = _csv_bytes(
        _row_values(byu_id="123456789", first_name="Jane", last_name="Doe")
    )
    rows, _ = import_csv.parse_and_map(csv)
    report = _run(import_csv.evaluate(session, rows))
    row = report["rows"][0]
    assert row["status"] == "rejected"
    codes = {b["code"] for b in row["blockers"]}
    assert "duplicate_byu_id" in codes


# --- Exact duplicate WITHIN the file -> second rejected ----------------------


def test_exact_duplicate_within_file_second_rejected():
    csv = _csv_bytes(
        _row_values(byu_id="123456789", first_name="Jane", last_name="Doe"),
        _row_values(byu_id="123456789", first_name="Janet", last_name="Roe"),
    )
    rows, _ = import_csv.parse_and_map(csv)
    report = _run(import_csv.evaluate(FakeSession(), rows))
    assert report["rows"][0]["status"] == "importable"
    second = report["rows"][1]
    assert second["status"] == "rejected"
    codes = {b["code"] for b in second["blockers"]}
    assert "duplicate_byu_id_in_file" in codes


# --- Fuzzy name+year within file -> importable with warning ------------------


def test_fuzzy_duplicate_within_file_warns():
    csv = _csv_bytes(
        _row_values(first_name="Jane", last_name="Doe", graduation_year="2018"),
        _row_values(first_name="Jane", last_name="Doe", graduation_year="2018"),
    )
    rows, _ = import_csv.parse_and_map(csv)
    report = _run(import_csv.evaluate(FakeSession(), rows))
    second = report["rows"][1]
    assert second["status"] == "importable"
    codes = {w["code"] for w in second["warnings"]}
    assert "possible_duplicate_in_file" in codes


# --- commit_import: imports importable, skips rejects ------------------------


def test_commit_import_inserts_and_reports_rejects():
    csv = _csv_bytes(
        _row_values(
            byu_id="123456789",
            first_name="Jane",
            last_name="Doe",
            graduation_year="2018",
        ),
        # duplicate byu in-file -> rejected
        _row_values(byu_id="123456789", first_name="Janet", last_name="Roe"),
        # bad date -> rejected at mapping
        _row_values(
            first_name="Bad", last_name="Date", birth_date="not-a-date"
        ),
    )
    rows, _ = import_csv.parse_and_map(csv)
    session = FakeSession()
    result = _run(import_csv.commit_import(session, rows))
    assert result["imported"] == 1
    assert result["skipped"] == 2
    assert len(result["created_ids"]) == 1
    reject_rows = {r["row"] for r in result["rejects"]}
    assert reject_rows == {3, 4}  # rows 3 + 4 (1-based, header is row 1)
    # Exactly one real commit for the whole batch.
    assert session.committed == 1


def test_commit_import_midbatch_failure_keeps_earlier_rows():
    # Row 2 (Jane) imports; row 3 (Boom) raises inside add() -> its savepoint
    # rolls back, row 4 (Late) still imports. imported/created_ids reflect ONLY
    # the two that flushed cleanly; the reject reason is the SAFE generic text
    # (no raw DB string), since RuntimeError isn't a domain error.
    csv = _csv_bytes(
        _row_values(first_name="Jane", last_name="Doe", graduation_year="2018"),
        _row_values(first_name="Boom", last_name="Row", graduation_year="2019"),
        _row_values(first_name="Late", last_name="Row", graduation_year="2020"),
    )
    rows, _ = import_csv.parse_and_map(csv)
    session = FakeSession(fail_on_add="Boom")
    result = _run(import_csv.commit_import(session, rows))

    assert result["imported"] == 2
    assert len(result["created_ids"]) == 2
    assert result["skipped"] == 1
    # Exactly the failing row was savepoint-rolled-back.
    assert session.savepoint_rollbacks == 1
    # One real commit for the surviving batch.
    assert session.committed == 1
    boom = next(r for r in result["rejects"] if r["name"] == "Boom Row")
    # SAFE classification: generic "Unexpected error (...)", not the raw message.
    assert boom["reason"] == "Unexpected error (RuntimeError)"
    assert "relation does not exist" not in boom["reason"]


def test_commit_import_conflict_error_message_surfaced():
    # A ConflictError (domain error) keeps its client-safe message verbatim.
    from app.core.errors import ConflictError

    reason = import_csv._classify_reject(
        ConflictError("BYU ID 123456789 already belongs to Jane Doe."), 5
    )
    assert reason == "BYU ID 123456789 already belongs to Jane Doe."


def test_commit_import_no_importable_does_not_commit():
    csv = _csv_bytes(
        _row_values(
            first_name="Bad", last_name="Date", birth_date="not-a-date"
        )
    )
    rows, _ = import_csv.parse_and_map(csv)
    session = FakeSession()
    result = _run(import_csv.commit_import(session, rows))
    assert result["imported"] == 0
    assert result["skipped"] == 1
    assert session.committed == 0


# --- Template endpoint content ----------------------------------------------


FINALIZED_ALUMNI_HEADERS = [
    "Filled out Survey",
    "MSTID (from OneAccord)",
    "BYU ID (9 digits)",
    "Net ID",
    "Preferred first name",
    "First name",
    "Middle name",
    "Last Name",
    "Gender",
    "Personal Email",
    "Birthday (YYYY-MM-DD)",
    "Graduation Semester",
    "Graduation Year",
    "Class of",
    "LinkedIn URL",
    "Finance program admitted year",
    "Employment Status",
    "Profile Updated By",
    "Profile Updated Date",
    "Finance Leadership Position",
    "Graduate degree",
    "Graduate university",
    "Graduate graduation year",
    "Deceased? (Yes/No)",
    "Notes",
    "Citizenship",
    "Marital Status",
    "Spouse First Name",
    "Spouse Last Name",
    "Phone #",
    "Current employer",
    "Current title",
    "Current industry (see Reference sheet)",
    "Secondary industry (see Reference sheet)",
    "Work Email",
    "Address line 1",
    "Address line 2",
    "Current city",
    "Current state",
    "Region (Northeast, Southeast, Midwest, Southwest, West, and Mountain West)",
    "Current country",
    "Current ZIP",
    "Home country",
    "Degree",
    "Major",
    "Degree status",
    "Degree year",
    "Former Company",
    "Former Title",
    "Former Industry",
    "Willing to host NetTrek (Yes/No)",
    "Willing to attend finance conference (Yes/No)",
    "Willing to mentor (Yes/No)",
    "Willing to sponsor company event (Yes/No)",
    "Willing to guest speak (Yes/No)",
    "Willing to help at events (Yes/No)",
    "Willing to host case competition (yes/no)",
    "Willing to mentor — Women in Finance (Yes/No)",
    "Hired a finance intern (Yes/No)",
    "Hired finance full-time (Yes/No)",
    "Willing to be a PIFF donor (Yes/No)",
    "CFP designation (Yes/No)",
    "CFA designation (Yes/No)",
    "Other Designations:",
    "Engagement notes",
    "Best Contact",
]


def test_expected_headers_are_the_finalized_set_in_order():
    # The intake template's EXPECTED_HEADERS must equal the finalized column set
    # VERBATIM and in order (header validation is exact-match both ways). The set
    # grew from 64 to 66 when the graduate university + graduation-year columns
    # were added (#269 follow-up) so the graduate program round-trips. Adding the
    # "Mountain West" region RENAMED the Region column's caption without adding a
    # column, so the count is unchanged — retired captions live in
    # import_csv._LEGACY_HEADER_ALIASES, never here.
    assert import_csv.EXPECTED_HEADERS == FINALIZED_ALUMNI_HEADERS
    assert len(import_csv.EXPECTED_HEADERS) == 66
    # Every header is a mapping key (and vice-versa).
    assert set(import_csv._MAPPING) == set(FINALIZED_ALUMNI_HEADERS)


def test_template_csv_has_expected_headers():
    csv_text = import_csv.build_template_csv()
    lines = csv_text.splitlines()
    import csv as _csv

    header_line = next(_csv.reader([lines[0]]))
    assert header_line == HEADERS
    # An example row is present.
    assert len(lines) >= 2


# --- Route-level (multipart) coverage ---------------------------------------
#
# Mirrors the fake-session route pattern in tests/test_alumni_routes.py: the
# DB-user + session dependencies are overridden so the import endpoints run
# without a real DATABASE_URL.


def _ctx(*roles: str) -> UserContext:
    return UserContext(
        user_id=1,
        auth_user_id=uuid.UUID("11111111-1111-1111-1111-111111111111"),
        roles=list(roles),
    )


def _full_access_client(session):
    async def _override():
        yield session

    app.dependency_overrides[get_session] = _override
    app.dependency_overrides[get_current_db_user] = lambda: _ctx("full_access")
    return TestClient(app, raise_server_exceptions=False)


def test_route_import_preview_forbidden_for_view_only():
    app.dependency_overrides[get_current_db_user] = lambda: _ctx("view_only")
    with TestClient(app, raise_server_exceptions=False) as c:
        resp = c.post(
            "/alumni/import/preview",
            files={"file": ("a.csv", b"x", "text/csv")},
        )
    app.dependency_overrides.clear()
    assert resp.status_code == 403


def test_route_import_preview_returns_report():
    csv = _csv_bytes(
        _row_values(
            byu_id="123456789",
            first_name="Jane",
            last_name="Doe",
            graduation_year="2018",
            current_employer="Goldman",
        )
    )
    session = FakeSession()
    with _full_access_client(session) as c:
        resp = c.post(
            "/alumni/import/preview",
            files={"file": ("import.csv", csv, "text/csv")},
        )
    app.dependency_overrides.clear()
    assert resp.status_code == 200
    body = resp.json()
    assert body["columns_ok"] is True
    assert body["summary"]["importable"] == 1


def test_route_import_preview_bad_headers():
    csv = _csv_bytes(["Jane"], headers=["Only column"])
    with _full_access_client(FakeSession()) as c:
        resp = c.post(
            "/alumni/import/preview",
            files={"file": ("bad.csv", csv, "text/csv")},
        )
    app.dependency_overrides.clear()
    assert resp.status_code == 200
    body = resp.json()
    assert body["columns_ok"] is False
    assert body["header_errors"]


def test_route_import_commits():
    csv = _csv_bytes(
        _row_values(
            byu_id="123456789",
            first_name="Jane",
            last_name="Doe",
            graduation_year="2018",
        )
    )
    session = FakeSession()
    with _full_access_client(session) as c:
        resp = c.post(
            "/alumni/import",
            files={"file": ("import.csv", csv, "text/csv")},
        )
    app.dependency_overrides.clear()
    assert resp.status_code == 200
    body = resp.json()
    assert body["imported"] == 1
    assert len(body["created_ids"]) == 1


def test_route_import_preview_oversized_413():
    big = b"x" * (import_csv.MAX_UPLOAD_BYTES + 10)
    with _full_access_client(FakeSession()) as c:
        resp = c.post(
            "/alumni/import/preview",
            files={"file": ("big.csv", big, "text/csv")},
        )
    app.dependency_overrides.clear()
    assert resp.status_code == 413
    assert resp.json()["error"]["code"] == "payload_too_large"


def test_route_import_oversized_413():
    big = b"x" * (import_csv.MAX_UPLOAD_BYTES + 10)
    with _full_access_client(FakeSession()) as c:
        resp = c.post(
            "/alumni/import",
            files={"file": ("big.csv", big, "text/csv")},
        )
    app.dependency_overrides.clear()
    assert resp.status_code == 413


def test_route_template_download():
    with _full_access_client(FakeSession()) as c:
        resp = c.get("/alumni/import/template")
    app.dependency_overrides.clear()
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/csv")
    assert "alumni_import_template.csv" in resp.headers["content-disposition"]
    first_line = resp.text.splitlines()[0]
    assert first_line.startswith("Filled out Survey")


# --- Friend (non-alumni contact) import (#294) -------------------------------


def test_friend_template_excludes_academic_and_identity_fields():
    csv_text = import_csv.build_template_csv(friend=True)
    header_line = csv_text.splitlines()[0]
    headers = header_line.split(",")
    # Includes the friend-relevant columns...
    assert "First name" in headers
    assert "Last Name" in headers
    assert "Current employer" in headers
    assert "Personal Email" in headers
    assert "Willing to mentor (Yes/No)" in headers
    assert "Willing to be a PIFF donor (Yes/No)" in headers
    # ...and excludes the alumni-only academic / identity fields.
    for banned in (
        "BYU ID (9 digits)",
        "Net ID",
        "Graduation Year",
        "Graduation Semester",
        "Class of",
        "Finance program admitted year",
        "Graduate degree",
        "Degree year",
        "Spouse First Name",
    ):
        assert banned not in headers


def test_friend_row_commits_with_is_alumni_false():
    csv = _csv_bytes(
        _friend_row_values(
            first_name="Rick",
            last_name="Recruiter",
            current_employer="Acme Capital",
            personal_email="rick@acme.example.com",
        ),
        headers=FRIEND_HEADERS,
    )
    rows, errors = import_csv.parse_and_map(csv, friend=True)
    assert errors == []
    # The mapped payload carries the is_alumni=False stamp.
    assert rows[0]["payload"]["is_alumni"] is False
    session = FakeSession()
    result = _run(import_csv.commit_import(session, rows))
    assert result["imported"] == 1
    # The persisted Alumni row is a friend.
    created = [o for o in session.added if hasattr(o, "is_alumni")]
    assert len(created) == 1
    assert created[0].is_alumni is False
    assert created[0].first_name == "Rick"


def test_alumni_import_row_defaults_is_alumni_unset():
    # A normal (non-friend) import must NOT stamp is_alumni, so the model default
    # (True) / DB server_default applies.
    csv = _csv_bytes(
        _row_values(first_name="Jane", last_name="Doe", graduation_year="2018")
    )
    rows, _ = import_csv.parse_and_map(csv)
    assert "is_alumni" not in rows[0]["payload"]


def test_friend_headers_reject_alumni_only_columns():
    # Feeding a friend import the FULL alumni template must fail header validation
    # (the academic columns are "unexpected" for the friend surface).
    csv = _csv_bytes(_row_values(first_name="Jane", last_name="Doe"))
    rows, errors = import_csv.parse_and_map(csv, friend=True)
    assert rows == []
    assert any("Unexpected column" in e for e in errors)


def test_route_friend_template_download():
    with _full_access_client(FakeSession()) as c:
        resp = c.get("/alumni/import/template", params={"kind": "friend"})
    app.dependency_overrides.clear()
    assert resp.status_code == 200
    assert "friend_import_template.csv" in resp.headers["content-disposition"]
    first_line = resp.text.splitlines()[0]
    assert first_line.startswith("Filled out Survey")
    assert "BYU ID (9 digits)" not in first_line


def test_route_friend_import_kind_routes_and_flags_friend():
    csv = _csv_bytes(
        _friend_row_values(first_name="Rick", last_name="Recruiter"),
        headers=FRIEND_HEADERS,
    )
    session = FakeSession()
    with _full_access_client(session) as c:
        resp = c.post(
            "/alumni/import",
            params={"kind": "friend"},
            files={"file": ("friends.csv", csv, "text/csv")},
        )
    app.dependency_overrides.clear()
    assert resp.status_code == 200
    assert resp.json()["imported"] == 1
    created = [o for o in session.added if hasattr(o, "is_alumni")]
    assert created and created[0].is_alumni is False
