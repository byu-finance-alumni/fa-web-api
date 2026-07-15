"""Offline tests for the bulk UPDATE ("round-trip") CSV path (no database).

Staff export a cohort to CSV, edit cells, and upload it back to mass-UPDATE the
existing profiles. This exercises ``import_csv.evaluate_update`` (preview diff)
and ``import_csv.commit_update`` (apply through the single-record edit path),
matching the fake-session style of ``tests/test_alumni_import.py`` /
``tests/test_alumni_service.py``.

A fake session serves the one-time identity index (first ``execute``), resolves
``session.get`` from seeded alumni, and routes ``session.scalar`` to the seeded
related-section row by table name (so the real ``update_alumni`` write path can
run without Postgres). Row shapes reuse the create-import row helper.
"""

import asyncio

from sqlalchemy.dialects import postgresql

from app.models.alumni import Alumni
from app.models.employment import CurrentEmployment
from app.services import import_csv
from tests.test_alumni_import import _csv_bytes, _row_values

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
    def __init__(self, session):
        self._session = session
        self._mark = len(session.added)

    async def __aenter__(self):
        self._mark = len(self._session.added)
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def rollback(self):
        self._session.savepoint_rollbacks += 1
        del self._session.added[self._mark :]


class FakeUpdateSession:
    """Serves the identity index, ``get`` by id, and section-row ``scalar``.

    ``index_rows`` feeds the FIRST ``execute`` (the batch identity load); later
    ``execute`` calls (the fuzzy duplicate scan inside ``update_alumni``) return
    empty. ``alumni`` maps alumni_id -> seeded ``Alumni``. ``sections`` maps a
    table-name fragment (e.g. ``current_employment``) -> the seeded related row;
    any ``scalar`` whose SQL targets that table returns it, so both the preview
    diff read and the write-path upsert see the SAME object. Duplicate/spouse
    lookups (which hit the ``alumni`` table only) fall through to None.
    """

    def __init__(self, index_rows=(), alumni=None, sections=None):
        self._index_rows = list(index_rows)
        self._alumni = dict(alumni or {})
        self._sections = dict(sections or {})
        self.added = []
        self.committed = 0
        self.savepoint_rollbacks = 0

    def add(self, obj):
        self.added.append(obj)

    async def get(self, entity, ident):
        return self._alumni.get(ident)

    async def scalar(self, stmt):
        sql = str(stmt.compile(dialect=postgresql.dialect()))
        for fragment, row in self._sections.items():
            if fragment in sql:
                return row
        return None

    async def execute(self, stmt):
        # The batch identity load (_load_existing_index) is the only WHERE-less
        # SELECT; serve it the index rows every call (each request loads it
        # fresh). The fuzzy-duplicate scan inside update_alumni has a WHERE and
        # returns nothing here.
        sql = str(stmt.compile(dialect=postgresql.dialect()))
        if "WHERE" not in sql:
            return _ExecResult(self._index_rows)
        return _ExecResult([])

    async def flush(self):
        for obj in self.added:
            if getattr(obj, "alumni_id", None) is None:
                obj.alumni_id = 900 + self.added.index(obj)

    async def commit(self):
        self.committed += 1

    async def refresh(self, obj):
        pass

    def begin_nested(self):
        return _Savepoint(self)


def _run(coro):
    return asyncio.run(coro)


# Identity index rows are (id, byu_id, net_id, first, last, grad_year, archived).


# --- (a) preview diff: one changed cell, blanks excluded ---------------------


def test_update_preview_diffs_only_changed_cell():
    index = [(1, "123456789", None, "Jane", "Doe", 2018, False)]
    alumni = {
        1: Alumni(
            alumni_id=1,
            byu_id="123456789",
            first_name="Jane",
            last_name="Doe",
            graduation_year=2018,
            archived=False,
        )
    }
    session = FakeUpdateSession(index_rows=index, alumni=alumni)
    # Only the BYU ID (the match key, unchanged) + a new last name are filled in.
    csv = _csv_bytes(_row_values(byu_id="123456789", last_name="Smith"))
    rows, errors = import_csv.parse_and_map(csv)
    assert errors == []
    report = _run(import_csv.evaluate_update(session, rows))

    assert report["columns_ok"] is True
    assert report["summary"]["matched"] == 1
    assert report["summary"]["with_changes"] == 1
    row = report["rows"][0]
    assert row["status"] == "update"
    assert row["alumni_id"] == 1
    # Exactly one change: last_name Doe -> Smith. The unchanged match key (byu_id)
    # and every blank cell are NOT reported.
    assert row["changes"] == [
        {"field": "last_name", "section": "core", "old": "Doe", "new": "Smith"}
    ]


def test_update_preview_no_changes_when_values_match():
    index = [(1, "123456789", None, "Jane", "Doe", 2018, False)]
    alumni = {
        1: Alumni(
            alumni_id=1,
            byu_id="123456789",
            first_name="Jane",
            last_name="Doe",
            archived=False,
        )
    }
    session = FakeUpdateSession(index_rows=index, alumni=alumni)
    # Re-upload the same last name -> nothing differs.
    csv = _csv_bytes(_row_values(byu_id="123456789", last_name="Doe"))
    rows, _ = import_csv.parse_and_map(csv)
    report = _run(import_csv.evaluate_update(session, rows))
    row = report["rows"][0]
    assert row["status"] == "no_changes"
    assert row["changes"] == []
    assert report["summary"]["matched"] == 1
    assert report["summary"]["with_changes"] == 0


# --- (b) unmatched row is reported, never created ----------------------------


def test_update_unmatched_row_reported_not_created():
    index = [(1, "123456789", None, "Jane", "Doe", 2018, False)]
    alumni = {1: Alumni(alumni_id=1, byu_id="123456789", archived=False)}
    session = FakeUpdateSession(index_rows=index, alumni=alumni)
    # A BYU ID nobody has.
    csv = _csv_bytes(
        _row_values(byu_id="999999999", first_name="Nobody", last_name="Here")
    )
    rows, _ = import_csv.parse_and_map(csv)

    preview = _run(import_csv.evaluate_update(session, rows))
    assert preview["rows"][0]["status"] == "unmatched"
    assert preview["summary"]["unmatched"] == 1

    result = _run(import_csv.commit_update(session, rows))
    assert result["updated"] == 0
    assert result["unmatched"] == 1
    assert result["updated_ids"] == []
    # Nothing was inserted (update mode never creates) and no commit ran.
    assert session.added == []
    assert session.committed == 0
    assert result["results"][0]["status"] == "unmatched"


def test_update_archived_only_match_reported_unmatched_archived():
    # The row matches ONLY an archived record -> reported, never updated.
    index = [(9, "123456789", None, "Jane", "Doe", 2018, True)]
    session = FakeUpdateSession(index_rows=index)
    csv = _csv_bytes(_row_values(byu_id="123456789", last_name="Smith"))
    rows, _ = import_csv.parse_and_map(csv)
    report = _run(import_csv.evaluate_update(session, rows))
    row = report["rows"][0]
    assert row["status"] == "unmatched_archived"
    assert row["alumni_id"] == 9
    assert "archived" in row["message"].lower()


# --- (c) commit updates only the changed core field, leaves others intact ----


def test_commit_update_changes_only_supplied_core_field():
    index = [(1, "123456789", None, "Jane", "Doe", 2018, False)]
    existing = Alumni(
        alumni_id=1,
        byu_id="123456789",
        first_name="Jane",
        last_name="Doe",
        archived=False,
    )
    session = FakeUpdateSession(index_rows=index, alumni={1: existing})
    # Only last_name is filled in; first_name is left blank.
    csv = _csv_bytes(_row_values(byu_id="123456789", last_name="Smith"))
    rows, _ = import_csv.parse_and_map(csv)
    result = _run(import_csv.commit_update(session, rows))

    assert result["updated"] == 1
    assert result["updated_ids"] == [1]
    assert session.committed == 1
    # The changed field was applied; the blank cell left first_name intact.
    assert existing.last_name == "Smith"
    assert existing.first_name == "Jane"
    assert existing.manually_edited_at is not None


# --- (d) a blank section cell does NOT clear an existing value ----------------


def test_commit_update_blank_cell_does_not_clear_existing_section_value():
    index = [(1, "123456789", None, "Jane", "Doe", 2018, False)]
    existing = Alumni(alumni_id=1, byu_id="123456789", first_name="Jane", archived=False)
    # Current employment already has an employer AND a title.
    employment = CurrentEmployment(
        current_employment_id=5,
        alumni_id=1,
        current_employer="Old Co",
        current_title="Analyst",
    )
    session = FakeUpdateSession(
        index_rows=index,
        alumni={1: existing},
        sections={"current_employment": employment},
    )
    # Change the employer only; leave the title cell blank.
    csv = _csv_bytes(
        _row_values(byu_id="123456789", current_employer="New Co")
    )
    rows, _ = import_csv.parse_and_map(csv)

    preview = _run(import_csv.evaluate_update(session, rows))
    changed = {
        (c["section"], c["field"]): (c["old"], c["new"])
        for c in preview["rows"][0]["changes"]
    }
    assert changed[("career", "current_employer")] == ("Old Co", "New Co")
    # The blank title is not a change.
    assert ("career", "current_title") not in changed

    result = _run(import_csv.commit_update(session, rows))
    assert result["updated"] == 1
    # Employer updated in place; the blank title cell did NOT null the stored one.
    assert employment.current_employer == "New Co"
    assert employment.current_title == "Analyst"


# --- (e) match falls back from BYU ID to Net ID ------------------------------


def test_update_match_falls_back_to_net_id():
    # A row with a BLANK BYU ID but a Net ID that matches an active record
    # resolves via the Net ID fallback to id 2 (mixed-case "JSmith" -> "jsmith").
    index = [
        (1, "123456789", None, "Jane", "Doe", 2018, False),
        (2, None, "jsmith", "John", "Smith", 2019, False),
    ]
    alumni = {
        2: Alumni(
            alumni_id=2,
            net_id="jsmith",
            first_name="John",
            last_name="Smith",
            archived=False,
        )
    }
    session = FakeUpdateSession(index_rows=index, alumni=alumni)
    csv = _csv_bytes(_row_values(net_id="JSmith", last_name="Smithers"))
    rows, _ = import_csv.parse_and_map(csv)
    report = _run(import_csv.evaluate_update(session, rows))
    row = report["rows"][0]
    assert row["status"] == "update"
    assert row["alumni_id"] == 2
    assert row["changes"] == [
        {"field": "last_name", "section": "core", "old": "Smith", "new": "Smithers"}
    ]


# --- Header + route coverage -------------------------------------------------


def test_update_preview_bad_headers_reported():
    csv = _csv_bytes(["Jane"], headers=["Only column"])
    rows, header_errors = import_csv.parse_and_map(csv)
    assert rows == []
    assert header_errors


def test_commit_update_reports_per_row_outcomes():
    index = [(1, "123456789", None, "Jane", "Doe", 2018, False)]
    existing = Alumni(
        alumni_id=1, byu_id="123456789", first_name="Jane", last_name="Doe", archived=False
    )
    session = FakeUpdateSession(index_rows=index, alumni={1: existing})
    csv = _csv_bytes(
        # matched + changed
        _row_values(byu_id="123456789", last_name="Smith"),
        # unmatched
        _row_values(byu_id="888888888", last_name="Ghost"),
    )
    rows, _ = import_csv.parse_and_map(csv)
    result = _run(import_csv.commit_update(session, rows))
    assert result["updated"] == 1
    assert result["unmatched"] == 1
    statuses = {r["row"]: r["status"] for r in result["results"]}
    assert statuses == {2: "updated", 3: "unmatched"}


# --- Route-level (multipart) coverage ---------------------------------------


def test_route_update_preview_forbidden_for_view_only():
    import uuid

    from fastapi.testclient import TestClient

    from app.api.dependencies.auth import get_current_db_user
    from app.main import app
    from app.schemas.auth import UserContext

    app.dependency_overrides[get_current_db_user] = lambda: UserContext(
        user_id=1,
        auth_user_id=uuid.UUID("11111111-1111-1111-1111-111111111111"),
        roles=["view_only"],
    )
    with TestClient(app, raise_server_exceptions=False) as c:
        resp = c.post(
            "/alumni/import/update/preview",
            files={"file": ("a.csv", b"x", "text/csv")},
        )
    app.dependency_overrides.clear()
    assert resp.status_code == 403


def test_route_update_preview_returns_report():
    import uuid

    from fastapi.testclient import TestClient

    from app.api.dependencies.auth import get_current_db_user
    from app.core.database import get_session
    from app.main import app
    from app.schemas.auth import UserContext

    index = [(1, "123456789", None, "Jane", "Doe", 2018, False)]
    session = FakeUpdateSession(
        index_rows=index,
        alumni={
            1: Alumni(
                alumni_id=1,
                byu_id="123456789",
                first_name="Jane",
                last_name="Doe",
                archived=False,
            )
        },
    )

    async def _override():
        yield session

    app.dependency_overrides[get_session] = _override
    app.dependency_overrides[get_current_db_user] = lambda: UserContext(
        user_id=1,
        auth_user_id=uuid.UUID("11111111-1111-1111-1111-111111111111"),
        roles=["full_access"],
    )
    csv = _csv_bytes(_row_values(byu_id="123456789", last_name="Smith"))
    with TestClient(app, raise_server_exceptions=False) as c:
        resp = c.post(
            "/alumni/import/update/preview",
            files={"file": ("cohort.csv", csv, "text/csv")},
        )
    app.dependency_overrides.clear()
    assert resp.status_code == 200
    body = resp.json()
    assert body["columns_ok"] is True
    assert body["summary"]["with_changes"] == 1
    assert body["rows"][0]["status"] == "update"


def test_route_update_bad_headers_returns_zeroed_result():
    import uuid

    from fastapi.testclient import TestClient

    from app.api.dependencies.auth import get_current_db_user
    from app.core.database import get_session
    from app.main import app
    from app.schemas.auth import UserContext

    session = FakeUpdateSession()

    async def _override():
        yield session

    app.dependency_overrides[get_session] = _override
    app.dependency_overrides[get_current_db_user] = lambda: UserContext(
        user_id=1,
        auth_user_id=uuid.UUID("11111111-1111-1111-1111-111111111111"),
        roles=["full_access"],
    )
    csv = _csv_bytes(["Jane"], headers=["Only column"])
    with TestClient(app, raise_server_exceptions=False) as c:
        resp = c.post(
            "/alumni/import/update",
            files={"file": ("bad.csv", csv, "text/csv")},
        )
    app.dependency_overrides.clear()
    assert resp.status_code == 200
    body = resp.json()
    assert body["updated"] == 0
    assert body["errors"] >= 1
