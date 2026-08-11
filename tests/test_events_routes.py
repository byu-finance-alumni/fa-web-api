"""Tests for the events read routes.

Auth-gating (a missing token is 401 before any query) plus happy-path coverage
of the detail and attendees endpoints using a stubbed session — no real
DATABASE_URL is required (CI has none). View access is granted to all three
roles, so reads have no 403 case.
"""

import datetime
import uuid
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app.api.dependencies.auth import get_current_db_user
from app.core.database import get_session
from app.main import app
from app.schemas.auth import UserContext


def _ctx(*roles: str) -> UserContext:
    return UserContext(
        user_id=1,
        auth_user_id=uuid.UUID("11111111-1111-1111-1111-111111111111"),
        roles=list(roles),
    )


async def _no_db_session():
    yield None


@pytest.fixture
def client():
    app.dependency_overrides[get_session] = _no_db_session
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


# --- auth gating --------------------------------------------------------------


@pytest.mark.parametrize(
    "path",
    ["/events", "/events/1", "/events/1/attendees"],
)
def test_events_requires_auth(client, path):
    response = client.get(path)
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "unauthorized"


# --- happy path (stubbed session) ---------------------------------------------


class _Result:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return self._rows


class _FakeSession:
    """Minimal AsyncSession stand-in for the events read routes."""

    def __init__(self, *, event, attendee_rows):
        self._event = event
        self._attendee_rows = attendee_rows

    async def get(self, _model, _pk):
        return self._event

    async def execute(self, _stmt):
        return _Result(self._attendee_rows)


def _event():
    return SimpleNamespace(
        event_id=7,
        event_name="Spring Networking Night",
        event_type="Networking",
        event_date=datetime.date(2026, 4, 10),
        event_location="Tanner Building",
        event_notes="Annual mixer.",
    )


def _attendee(**over):
    base = dict(
        alumni_id=42,
        first_name="Jane",
        preferred_first_name=None,
        last_name="Doe",
        graduation_year=2018,
        archived=False,
    )
    base.update(over)
    return SimpleNamespace(**base)


def _with_session(session):
    async def _override():
        yield session

    return _override


def test_event_attendees_returns_alumni(client):
    app.dependency_overrides[get_current_db_user] = lambda: _ctx("view_only")
    app.dependency_overrides[get_session] = _with_session(
        _FakeSession(
            event=_event(),
            # Roster rows are (alumni, attendance_status, attendance_notes).
            attendee_rows=[(_attendee(), "attended", "VIP - front table")],
        )
    )
    response = client.get("/events/7/attendees")
    assert response.status_code == 200
    body = response.json()
    assert body == [
        {
            "alumni_id": 42,
            "name": "Jane Doe",
            "graduation_year": 2018,
            "attendance_status": "attended",
            "notes": "VIP - front table",
        }
    ]


def test_event_attendees_notes_null_when_absent(client):
    # A row with no attendance_notes surfaces notes: null (not omitted / crash).
    app.dependency_overrides[get_current_db_user] = lambda: _ctx("view_only")
    app.dependency_overrides[get_session] = _with_session(
        _FakeSession(
            event=_event(),
            attendee_rows=[(_attendee(), "attended", None)],
        )
    )
    response = client.get("/events/7/attendees")
    assert response.status_code == 200
    assert response.json()[0]["notes"] is None


def test_event_attendees_unknown_event_is_404(client):
    app.dependency_overrides[get_current_db_user] = lambda: _ctx("view_only")
    app.dependency_overrides[get_session] = _with_session(
        _FakeSession(event=None, attendee_rows=[])
    )
    response = client.get("/events/999/attendees")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"


# --- attendee CSV export (GET /events/{id}/attendees/export, #219) ------------


class _ExportSession:
    """Stand-in for the attendee export: ``get`` returns the event, ``execute``
    returns (alumni, personal_email, work_email) rows, and add/commit record the
    disclosure audit."""

    def __init__(self, *, event, rows):
        self._event = event
        self._rows = rows
        self.added: list = []
        self.committed = False

    async def get(self, _model, _pk):
        return self._event

    async def execute(self, _stmt):
        return _Result(self._rows)

    def add(self, obj):
        self.added.append(obj)

    async def commit(self):
        self.committed = True


def _export_alumnus(**over):
    base = dict(
        alumni_id=42,
        first_name="Jane",
        preferred_first_name=None,
        last_name="Doe",
        net_id="jdoe",
    )
    base.update(over)
    return SimpleNamespace(**base)


def test_attendee_export_requires_auth(client):
    response = client.get("/events/7/attendees/export")
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "unauthorized"


def test_attendee_export_forbidden_for_view_only(client):
    # Bulk PII leaves the system here, so it is gated above the view-only roster.
    app.dependency_overrides[get_current_db_user] = lambda: _ctx("view_only")
    response = client.get("/events/7/attendees/export")
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "forbidden"


def test_attendee_export_unknown_event_is_404(client):
    app.dependency_overrides[get_current_db_user] = lambda: _ctx("full_access")
    app.dependency_overrides[get_session] = _with_session(
        _ExportSession(event=None, rows=[])
    )
    response = client.get("/events/999/attendees/export")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"


def test_attendee_export_returns_csv_and_audits(client):
    app.dependency_overrides[get_current_db_user] = lambda: _ctx("full_access")
    session = _ExportSession(
        event=_event(),
        rows=[
            (_export_alumnus(), "jane@personal.com", "jane@work.com"),
            # No personal email -> falls back to the work email.
            (
                _export_alumnus(
                    alumni_id=43, first_name="Mark", last_name="Ash", net_id="mash"
                ),
                None,
                "mark@work.com",
            ),
            # No email at all -> empty cell, never a crash.
            (
                _export_alumnus(
                    alumni_id=44, first_name="Amy", last_name="Bee", net_id=None
                ),
                None,
                None,
            ),
        ],
    )
    app.dependency_overrides[get_session] = _with_session(session)

    response = client.get("/events/7/attendees/export")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/csv")
    assert (
        'filename="event_7_attendees.csv"'
        in response.headers["content-disposition"]
    )
    lines = response.text.splitlines()
    assert lines[0] == "Name,Email,Net ID"
    assert lines[1] == "Jane Doe,jane@personal.com,jdoe"
    assert lines[2] == "Mark Ash,mark@work.com,mash"  # work-email fallback
    assert lines[3] == "Amy Bee,,"  # no email, no net_id

    assert session.committed is True
    audits = [o for o in session.added if hasattr(o, "action_type")]
    assert len(audits) == 1
    assert audits[0].action_type == "export_event_attendees"
    assert audits[0].entity_type == "event"
    assert audits[0].entity_id == 7
    assert audits[0].user_id == 1
    # Disclosure record is self-contained: row count + fixed column set + event.
    assert audits[0].new_value == (
        "rows=3; columns=name,email,net_id; event='Spring Networking Night'"
    )


def test_attendee_export_neutralizes_csv_formula_injection(client):
    # Every free-text cell here (name, email, net_id) is attacker-reachable —
    # first/last name and net_id come from staff-editable alumni fields, and a
    # stored value starting with =/+/-/@ would otherwise execute as a live
    # formula the moment a staff member opens this export in Excel (#169).
    # Uses the exact HYPERLINK payload style a real phishing attempt would send.
    import csv as _csv
    import io as _io

    app.dependency_overrides[get_current_db_user] = lambda: _ctx("full_access")
    session = _ExportSession(
        event=_event(),
        rows=[
            (
                _export_alumnus(
                    first_name='=HYPERLINK("http://evil.com","Click")',
                    last_name="",
                    net_id="+cmd|'/C calc'!A1",
                ),
                "-2+3+cmd|' /C calc'!A1@evil.com",
                None,
            ),
        ],
    )
    app.dependency_overrides[get_session] = _with_session(session)

    response = client.get("/events/7/attendees/export")
    assert response.status_code == 200
    rows = list(_csv.reader(_io.StringIO(response.text)))
    assert rows[0] == ["Name", "Email", "Net ID"]
    name_cell, email_cell, net_id_cell = rows[1]
    # Every cell that starts with a formula-lead char is tab-prefixed; the
    # underlying value is preserved byte-for-byte after the tab.
    assert name_cell == '\t=HYPERLINK("http://evil.com","Click")'
    assert email_cell == "\t-2+3+cmd|' /C calc'!A1@evil.com"
    assert net_id_cell == "\t+cmd|'/C calc'!A1"


# --- list filters (compiled SQL) ----------------------------------------------


class _CapturingSession:
    """Records the compiled SQL of executed statements so we can assert that
    the list filters made it into the WHERE clause. Returns no rows."""

    def __init__(self):
        self.compiled: list[str] = []

    async def execute(self, stmt):
        self.compiled.append(str(stmt.compile()))
        return _Result([])


def test_list_events_filters_present_in_sql(client):
    app.dependency_overrides[get_current_db_user] = lambda: _ctx("view_only")
    session = _CapturingSession()
    app.dependency_overrides[get_session] = _with_session(session)
    response = client.get(
        "/events",
        params={
            "q": "mixer",
            "event_type": "Networking",
            "date_from": "2026-01-01",
            "date_to": "2026-12-31",
        },
    )
    assert response.status_code == 200
    assert response.json() == []
    sql = session.compiled[0].lower()
    # q searches name OR location (ILIKE compiles to a lower(...) like ... pair
    # on the default dialect); type is lowered for case-insensitive exact match;
    # the date range bounds both appear.
    assert "like" in sql
    assert "events.event_name" in sql
    assert "events.event_location" in sql
    assert "lower(events.event_type)" in sql
    assert "events.event_date >=" in sql
    assert "events.event_date <=" in sql


def test_list_events_no_filters_has_no_where(client):
    app.dependency_overrides[get_current_db_user] = lambda: _ctx("view_only")
    session = _CapturingSession()
    app.dependency_overrides[get_session] = _with_session(session)
    response = client.get("/events")
    assert response.status_code == 200
    sql = session.compiled[0].lower()
    assert "ilike" not in sql
    assert "event_date >=" not in sql


# --- options endpoint ---------------------------------------------------------


def test_event_options_requires_auth(client):
    response = client.get("/events/options")
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "unauthorized"


def test_event_options_returns_distinct_types(client):
    app.dependency_overrides[get_current_db_user] = lambda: _ctx("view_only")
    app.dependency_overrides[get_session] = _with_session(
        _FakeSession(event=None, attendee_rows=[("Networking",), ("Workshop",)])
    )
    response = client.get("/events/options")
    assert response.status_code == 200
    assert response.json() == {"types": ["Networking", "Workshop"]}


# --- create (POST /events) ----------------------------------------------------


def test_create_event_requires_auth(client):
    response = client.post("/events", json={"event_name": "Mixer"})
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "unauthorized"


def test_create_event_forbidden_for_view_only(client):
    app.dependency_overrides[get_current_db_user] = lambda: _ctx("view_only")
    response = client.post("/events", json={"event_name": "Mixer"})
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "forbidden"


def test_create_event_rejects_missing_name(client):
    # full_access passes the guard; a missing name fails validation (422, not 403).
    app.dependency_overrides[get_current_db_user] = lambda: _ctx("full_access")
    response = client.post("/events", json={"event_type": "Networking"})
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"


def test_create_event_rejects_blank_name(client):
    app.dependency_overrides[get_current_db_user] = lambda: _ctx("full_access")
    response = client.post("/events", json={"event_name": "   "})
    assert response.status_code == 422


def test_create_event_rejects_missing_date(client):
    # M4: event_date is required — a dateless event is a 422, not a defaulted row.
    app.dependency_overrides[get_current_db_user] = lambda: _ctx("full_access")
    response = client.post("/events", json={"event_name": "Mixer"})
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"


def test_update_event_rejects_clearing_date(client):
    # M4: an explicit null must not wipe the (now required) event date.
    app.dependency_overrides[get_current_db_user] = lambda: _ctx("full_access")
    response = client.patch("/events/1", json={"event_date": None})
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"


class _CreateSession:
    """Captures added rows and assigns a PK on flush, mirroring a real insert."""

    def __init__(self):
        self.added: list = []
        self.committed = False

    def add(self, obj):
        self.added.append(obj)

    async def flush(self):
        for obj in self.added:
            if getattr(obj, "event_id", None) is None and hasattr(obj, "event_name"):
                obj.event_id = 123

    async def commit(self):
        self.committed = True

    async def refresh(self, _obj):
        pass


def test_create_event_happy_path_creates_and_audits(client):
    app.dependency_overrides[get_current_db_user] = lambda: _ctx("full_access")
    session = _CreateSession()
    app.dependency_overrides[get_session] = _with_session(session)
    response = client.post(
        "/events",
        json={
            "event_name": "Spring Mixer",
            "event_type": "Networking",
            "event_date": "2026-04-10",
            "event_location": "Tanner Building",
        },
    )
    assert response.status_code == 201
    body = response.json()
    assert body["event_id"] == 123
    assert body["event_name"] == "Spring Mixer"
    assert body["attendance_count"] == 0
    assert session.committed is True
    # Both the event and an audit row were added.
    events = [o for o in session.added if hasattr(o, "event_name")]
    audits = [o for o in session.added if hasattr(o, "action_type")]
    assert len(events) == 1
    assert events[0].logged_by_user_id == 1
    assert len(audits) == 1
    assert audits[0].entity_type == "event"
    assert audits[0].action_type == "create"
    assert audits[0].entity_id == 123
    assert audits[0].user_id == 1


# --- update (PATCH /events/{id}) ----------------------------------------------


def test_update_event_requires_auth(client):
    response = client.patch("/events/7", json={"event_name": "Renamed"})
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "unauthorized"


def test_update_event_forbidden_for_view_only(client):
    app.dependency_overrides[get_current_db_user] = lambda: _ctx("view_only")
    response = client.patch("/events/7", json={"event_name": "Renamed"})
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "forbidden"


def test_update_event_unknown_is_404(client):
    app.dependency_overrides[get_current_db_user] = lambda: _ctx("full_access")
    app.dependency_overrides[get_session] = _with_session(
        _FakeSession(event=None, attendee_rows=[])
    )
    response = client.patch("/events/999", json={"event_name": "Renamed"})
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"


def test_update_event_rejects_blank_name(client):
    app.dependency_overrides[get_current_db_user] = lambda: _ctx("full_access")
    response = client.patch("/events/7", json={"event_name": "   "})
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"


class _UpdateSession:
    """Stand-in for the PATCH flow: returns a fixed event from get(), records
    added audit rows, and answers the attendance-count scalar()."""

    def __init__(self, event, attendance=0):
        self._event = event
        self._attendance = attendance
        self.added: list = []
        self.committed = False

    async def get(self, _model, _pk):
        return self._event

    def add(self, obj):
        self.added.append(obj)

    async def commit(self):
        self.committed = True

    async def refresh(self, _obj):
        pass

    async def scalar(self, _stmt):
        return self._attendance


def test_update_event_happy_path_updates_and_audits(client):
    app.dependency_overrides[get_current_db_user] = lambda: _ctx("full_access")
    event = _event()
    session = _UpdateSession(event, attendance=5)
    app.dependency_overrides[get_session] = _with_session(session)
    response = client.patch(
        "/events/7",
        json={"event_name": "Updated Night", "event_location": "Wilkinson Center"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["event_name"] == "Updated Night"
    assert body["event_location"] == "Wilkinson Center"
    assert body["attendance_count"] == 5
    # The event object was mutated in place.
    assert event.event_name == "Updated Night"
    assert session.committed is True
    # One audit row per changed field, each capturing old/new.
    audits = [o for o in session.added if hasattr(o, "action_type")]
    assert {a.field_name for a in audits} == {"event_name", "event_location"}
    assert all(a.action_type == "update" for a in audits)
    assert all(a.entity_type == "event" for a in audits)
    assert all(a.entity_id == 7 for a in audits)
    assert all(a.user_id == 1 for a in audits)
    by_field = {a.field_name: a for a in audits}
    assert by_field["event_name"].old_value == "Spring Networking Night"
    assert by_field["event_name"].new_value == "Updated Night"


def test_update_event_no_changes_skips_audit_and_commit(client):
    app.dependency_overrides[get_current_db_user] = lambda: _ctx("full_access")
    event = _event()
    session = _UpdateSession(event, attendance=2)
    app.dependency_overrides[get_session] = _with_session(session)
    # Send the same name the event already has → no diff → no audit, no commit.
    response = client.patch("/events/7", json={"event_name": "Spring Networking Night"})
    assert response.status_code == 200
    assert response.json()["attendance_count"] == 2
    assert session.committed is False
    assert [o for o in session.added if hasattr(o, "action_type")] == []


# --- add attendee (POST /events/{id}/attendees) -------------------------------


class _AddAttendeeSession:
    """Stand-in for the add-attendee flow. ``get`` returns the event then the
    alumni in call order; ``scalar`` answers the duplicate-existence probe;
    added rows (attendance + audit) are recorded."""

    def __init__(self, *, event, alumni, existing=None):
        self._gets = [event, alumni]
        self._existing = existing
        self.added: list = []
        self.committed = False

    async def get(self, _model, _pk):
        return self._gets.pop(0) if self._gets else None

    async def scalar(self, _stmt):
        return self._existing

    def add(self, obj):
        self.added.append(obj)

    async def commit(self):
        self.committed = True


def test_add_attendee_requires_auth(client):
    response = client.post("/events/7/attendees", json={"alumni_id": 42})
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "unauthorized"


def test_add_attendee_forbidden_for_view_only(client):
    app.dependency_overrides[get_current_db_user] = lambda: _ctx("view_only")
    response = client.post("/events/7/attendees", json={"alumni_id": 42})
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "forbidden"


def test_add_attendee_rejects_unknown_keys(client):
    app.dependency_overrides[get_current_db_user] = lambda: _ctx("full_access")
    response = client.post(
        "/events/7/attendees", json={"alumni_id": 42, "bogus": "x"}
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"


def test_add_attendee_unknown_event_is_404(client):
    app.dependency_overrides[get_current_db_user] = lambda: _ctx("full_access")
    app.dependency_overrides[get_session] = _with_session(
        _AddAttendeeSession(event=None, alumni=_attendee())
    )
    response = client.post("/events/999/attendees", json={"alumni_id": 42})
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"


def test_add_attendee_unknown_alumni_is_404(client):
    app.dependency_overrides[get_current_db_user] = lambda: _ctx("full_access")
    app.dependency_overrides[get_session] = _with_session(
        _AddAttendeeSession(event=_event(), alumni=None)
    )
    response = client.post("/events/7/attendees", json={"alumni_id": 999})
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"


def test_add_attendee_duplicate_is_409(client):
    app.dependency_overrides[get_current_db_user] = lambda: _ctx("full_access")
    app.dependency_overrides[get_session] = _with_session(
        _AddAttendeeSession(event=_event(), alumni=_attendee(), existing=55)
    )
    response = client.post("/events/7/attendees", json={"alumni_id": 42})
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "conflict"


def test_add_attendee_happy_path_creates_and_audits(client):
    app.dependency_overrides[get_current_db_user] = lambda: _ctx("full_access")
    session = _AddAttendeeSession(event=_event(), alumni=_attendee())
    app.dependency_overrides[get_session] = _with_session(session)
    response = client.post(
        "/events/7/attendees",
        json={"alumni_id": 42, "attendance_status": "attended"},
    )
    assert response.status_code == 201
    body = response.json()
    assert body == {
        "alumni_id": 42,
        "name": "Jane Doe",
        "graduation_year": 2018,
        "attendance_status": "attended",
        "notes": None,
    }
    assert session.committed is True
    attendance = [o for o in session.added if hasattr(o, "alumni_id")]
    audits = [o for o in session.added if hasattr(o, "action_type")]
    assert len(attendance) == 1
    assert attendance[0].event_id == 7
    assert attendance[0].alumni_id == 42
    assert attendance[0].attendance_status == "attended"
    assert len(audits) == 1
    assert audits[0].action_type == "add_attendee"
    assert audits[0].entity_type == "event"
    assert audits[0].entity_id == 7
    assert audits[0].user_id == 1
    assert "42" in audits[0].new_value
    assert "Jane Doe" in audits[0].new_value


def test_add_attendee_persists_and_echoes_notes(client):
    # notes (#181) is persisted to attendance_notes and echoed back in the body.
    app.dependency_overrides[get_current_db_user] = lambda: _ctx("full_access")
    session = _AddAttendeeSession(event=_event(), alumni=_attendee())
    app.dependency_overrides[get_session] = _with_session(session)
    response = client.post(
        "/events/7/attendees",
        json={
            "alumni_id": 42,
            "attendance_status": "attended",
            "notes": "Spoke on the fintech panel",
        },
    )
    assert response.status_code == 201
    assert response.json()["notes"] == "Spoke on the fintech panel"
    attendance = [o for o in session.added if hasattr(o, "alumni_id")]
    assert attendance[0].attendance_notes == "Spoke on the fintech panel"


def test_add_attendee_archived_alumni_is_404(client):
    # Archived alumni are not valid attendees (mirrors the import's active-only
    # match); manual add rejects them with a 404, same as an unknown alumnus.
    app.dependency_overrides[get_current_db_user] = lambda: _ctx("full_access")
    app.dependency_overrides[get_session] = _with_session(
        _AddAttendeeSession(event=_event(), alumni=_attendee(archived=True))
    )
    response = client.post(
        "/events/7/attendees", json={"alumni_id": 42, "notes": "should not persist"}
    )
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"


# --- remove attendee (DELETE /events/{id}/attendees/{alumni_id}) --------------


class _RemoveAttendeeSession:
    """Stand-in for the remove-attendee flow. ``scalar`` returns the attendance
    row to delete (or None); deletions and added audit rows are recorded."""

    def __init__(self, attendance):
        self._attendance = attendance
        self.added: list = []
        self.deleted: list = []
        self.committed = False

    async def scalar(self, _stmt):
        return self._attendance

    async def delete(self, obj):
        self.deleted.append(obj)

    def add(self, obj):
        self.added.append(obj)

    async def commit(self):
        self.committed = True


def test_remove_attendee_requires_auth(client):
    response = client.delete("/events/7/attendees/42")
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "unauthorized"


def test_remove_attendee_forbidden_for_view_only(client):
    app.dependency_overrides[get_current_db_user] = lambda: _ctx("view_only")
    response = client.delete("/events/7/attendees/42")
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "forbidden"


def test_remove_attendee_not_present_is_404(client):
    app.dependency_overrides[get_current_db_user] = lambda: _ctx("full_access")
    app.dependency_overrides[get_session] = _with_session(
        _RemoveAttendeeSession(attendance=None)
    )
    response = client.delete("/events/7/attendees/42")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"


def test_remove_attendee_happy_path_deletes_and_audits(client):
    app.dependency_overrides[get_current_db_user] = lambda: _ctx("full_access")
    attendance = SimpleNamespace(
        event_attendance_id=99, event_id=7, alumni_id=42
    )
    session = _RemoveAttendeeSession(attendance=attendance)
    app.dependency_overrides[get_session] = _with_session(session)
    response = client.delete("/events/7/attendees/42")
    assert response.status_code == 200
    body = response.json()
    assert body == {"event_id": 7, "alumni_id": 42, "removed": True}
    assert session.committed is True
    assert session.deleted == [attendance]
    audits = [o for o in session.added if hasattr(o, "action_type")]
    assert len(audits) == 1
    assert audits[0].action_type == "remove_attendee"
    assert audits[0].entity_type == "event"
    assert audits[0].entity_id == 7
    assert audits[0].user_id == 1
    assert audits[0].old_value == "42"
