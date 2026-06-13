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
from types import SimpleNamespace

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
        "last_name": "Last name",
        "graduation_year": "Graduation year",
        "birth_date": "Birthday (YYYY-MM-DD)",
        "personal_email": "Personal email",
        "current_employer": "Current employer",
        "current_industry": "Current industry (see Reference sheet)",
        "deceased": "Deceased? (Yes/No)",
        "mentor_willing": "Willing to mentor (Yes/No)",
        "spouse_byu_id": "Spouse BYU ID (if also an alumnus)",
    }
    for field, value in overrides.items():
        values[by_field[field]] = value
    return [values[h] for h in HEADERS]


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
    assert import_csv.MAX_UPLOAD_BYTES == 5 * 1024 * 1024


def test_non_utf8_file_reports_friendly_error():
    # 0x80 is invalid as a leading UTF-8 byte -> decode fails gracefully.
    bad = b"First name,Last name\n\x80\x80,Doe\n"
    rows, errors = import_csv.parse_and_map(bad)
    assert rows == []
    assert errors
    assert "UTF-8" in errors[0]


def test_existing_index_normalizes_formatted_byu_id():
    # A stored formatted id "123-456-789" must collide with incoming "123456789".
    index = [(7, "123-456-789", None, "Jane", "Doe", 2018)]
    idx = _run(import_csv._load_existing_index(FakeSession(index_rows=index)))
    assert idx["byu_ids"] == {"123456789": 7}


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


def test_bad_date_rejected():
    csv = _csv_bytes(
        _row_values(first_name="Jane", last_name="Doe", birth_date="03/15/1990")
    )
    rows, _ = import_csv.parse_and_map(csv)
    report = _run(import_csv.evaluate(FakeSession(), rows))
    row = report["rows"][0]
    assert row["status"] == "rejected"
    assert "date" in row["error"].lower()


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
    # Existing index has byu 123456789 -> alumni_id 7. The hygiene byu scalar
    # lookup also returns that alum, producing the blocker.
    existing = SimpleNamespace(
        alumni_id=7, first_name="Jane", last_name="Doe", byu_id="123456789"
    )
    index = [(7, "123456789", None, "Jane", "Doe", 2018)]
    session = FakeSession(index_rows=index, scalars=[existing])
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


# --- Spouse link: unresolved -> warning, still importable --------------------


def test_unresolved_spouse_warns_but_imports():
    # spouse scalar resolve returns None (not found).
    session = FakeSession(scalars=[None])
    csv = _csv_bytes(
        _row_values(
            first_name="Jane", last_name="Doe", spouse_byu_id="999999999"
        )
    )
    rows, _ = import_csv.parse_and_map(csv)
    report = _run(import_csv.evaluate(session, rows))
    row = report["rows"][0]
    assert row["status"] == "importable"
    codes = {w["code"] for w in row["warnings"]}
    assert "spouse_not_found" in codes


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
    assert first_line.startswith("BYU ID (9 digits)")
