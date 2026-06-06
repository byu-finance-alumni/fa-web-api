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


def _attendee():
    return SimpleNamespace(
        alumni_id=42,
        first_name="Jane",
        preferred_first_name=None,
        last_name="Doe",
        graduation_year=2018,
    )


def _with_session(session):
    async def _override():
        yield session

    return _override


def test_event_attendees_returns_alumni(client):
    app.dependency_overrides[get_current_db_user] = lambda: _ctx("view_only")
    app.dependency_overrides[get_session] = _with_session(
        _FakeSession(event=_event(), attendee_rows=[(_attendee(), "attended")])
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
        }
    ]


def test_event_attendees_unknown_event_is_404(client):
    app.dependency_overrides[get_current_db_user] = lambda: _ctx("view_only")
    app.dependency_overrides[get_session] = _with_session(
        _FakeSession(event=None, attendee_rows=[])
    )
    response = client.get("/events/999/attendees")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"


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
