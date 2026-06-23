"""Tests for the unified-notes routes (#39).

Two layers, no real database:

  * Authorization + validation gating — writes (POST/PATCH/DELETE) must reject
    view_only AND student before any query runs (write = full_access and up);
    reads (GET) allow any view-access role. A missing token is 401.
  * Happy paths — create / list / update / delete against a stamping fake
    session, asserting the unified (entity_type, entity_id) projection, author
    resolution, and that a FERPA audit row is written for every write (with the
    body snapshotted on edit/delete).
"""

import datetime
import uuid
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app.api.dependencies.auth import get_current_db_user
from app.core.database import get_session
from app.main import app
from app.models.alumni import Alumni
from app.models.audit import AuditLog
from app.models.event import Event
from app.models.note import Note
from app.models.user import User
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


class _FakeResult:
    def __init__(self, rows):
        self._rows = rows

    def scalars(self):
        return self

    def all(self):
        return list(self._rows)


class _FakeSession:
    """No-DB session that dispatches ``get`` by model, stamps a created ``Note``
    on ``refresh`` (mimicking the DB identity/timestamp defaults), and returns a
    queued row set from ``execute`` (the notes list query)."""

    def __init__(self, *, alumni=None, event=None, user=None, note=None, rows=()):
        self._alumni = alumni
        self._event = event
        self._user = user
        self._note = note
        self._rows = rows
        self.added = []
        self.deleted = []
        self.committed = False
        self._next_id = 100

    async def get(self, model, pk):
        if model is Alumni:
            return self._alumni
        if model is Event:
            return self._event
        if model is User:
            return self._user
        if model is Note:
            return self._note
        return None

    async def execute(self, stmt):
        return _FakeResult(self._rows)

    def add(self, obj):
        if isinstance(obj, Note) and obj.note_id is None:
            obj.note_id = self._next_id
        self.added.append(obj)

    async def delete(self, obj):
        self.deleted.append(obj)

    async def commit(self):
        self.committed = True

    async def refresh(self, obj):
        if isinstance(obj, Note):
            stamp = datetime.datetime(2026, 6, 22, 12, 0, tzinfo=datetime.UTC)
            if obj.note_id is None:
                obj.note_id = self._next_id
            obj.created_at = stamp
            obj.updated_at = stamp

    @property
    def audits(self):
        return [a for a in self.added if isinstance(a, AuditLog)]


def _with_session(session):
    async def _override():
        yield session

    return _override


def _actor():
    return SimpleNamespace(user_id=1, first_name="Tanya", last_name="Harmon", email="th@byu.edu")


def _note(**kw):
    base = dict(
        note_id=7,
        alumni_id=1,
        interaction_id=None,
        event_id=None,
        body="Met at conference.",
        created_by_user_id=1,
        updated_by_user_id=1,
        created_at=datetime.datetime(2026, 6, 1, 9, 0, tzinfo=datetime.UTC),
        updated_at=datetime.datetime(2026, 6, 1, 9, 0, tzinfo=datetime.UTC),
    )
    base.update(kw)
    return SimpleNamespace(**base)


# --- auth + validation gating (no DB) ----------------------------------------


def test_create_note_requires_auth(client):
    response = client.post("/notes", json={"entity_type": "alumni", "entity_id": 1, "body": "x"})
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "unauthorized"


def test_create_note_forbidden_for_view_only(client):
    app.dependency_overrides[get_current_db_user] = lambda: _ctx("view_only")
    response = client.post("/notes", json={"entity_type": "alumni", "entity_id": 1, "body": "x"})
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "forbidden"


def test_create_note_forbidden_for_student(client):
    # student may edit existing alumni but is excluded from note writes by spec.
    app.dependency_overrides[get_current_db_user] = lambda: _ctx("student")
    response = client.post("/notes", json={"entity_type": "alumni", "entity_id": 1, "body": "x"})
    assert response.status_code == 403


def test_update_note_forbidden_for_view_only(client):
    app.dependency_overrides[get_current_db_user] = lambda: _ctx("view_only")
    response = client.patch("/notes/7", json={"body": "x"})
    assert response.status_code == 403


def test_delete_note_forbidden_for_view_only(client):
    app.dependency_overrides[get_current_db_user] = lambda: _ctx("view_only")
    response = client.delete("/notes/7")
    assert response.status_code == 403


def test_list_notes_requires_auth(client):
    response = client.get("/notes", params={"entity_type": "alumni", "entity_id": 1})
    assert response.status_code == 401


def test_create_note_rejects_empty_body(client):
    app.dependency_overrides[get_current_db_user] = lambda: _ctx("full_access")
    response = client.post("/notes", json={"entity_type": "alumni", "entity_id": 1, "body": "   "})
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"


def test_create_note_rejects_unknown_entity_type(client):
    app.dependency_overrides[get_current_db_user] = lambda: _ctx("full_access")
    response = client.post("/notes", json={"entity_type": "donor", "entity_id": 1, "body": "x"})
    assert response.status_code == 422


def test_create_note_rejects_unknown_field(client):
    app.dependency_overrides[get_current_db_user] = lambda: _ctx("full_access")
    response = client.post(
        "/notes",
        json={"entity_type": "alumni", "entity_id": 1, "body": "x", "nope": 1},
    )
    assert response.status_code == 422


# --- happy paths (stamping fake session) -------------------------------------


def test_create_alumni_note_happy_path(client):
    session = _FakeSession(alumni=SimpleNamespace(alumni_id=1), user=_actor())
    app.dependency_overrides[get_current_db_user] = lambda: _ctx("full_access")
    app.dependency_overrides[get_session] = _with_session(session)

    response = client.post(
        "/notes",
        json={"entity_type": "alumni", "entity_id": 1, "body": "  Met at conf.  "},
    )
    assert response.status_code == 201
    body = response.json()
    assert body["entity_type"] == "alumni"
    assert body["entity_id"] == 1
    assert body["body"] == "Met at conf."  # trimmed
    assert body["author"] == "Tanya Harmon"
    assert session.committed
    # FERPA: one audit row, against the owning alumni, carrying the new text.
    assert [a.action_type for a in session.audits] == ["add_note"]
    audit = session.audits[0]
    assert (audit.entity_type, audit.entity_id) == ("alumni", 1)
    assert audit.new_value == "Met at conf."


def test_create_note_404_when_target_missing(client):
    session = _FakeSession(alumni=None)  # alumni id doesn't resolve
    app.dependency_overrides[get_current_db_user] = lambda: _ctx("full_access")
    app.dependency_overrides[get_session] = _with_session(session)

    response = client.post("/notes", json={"entity_type": "alumni", "entity_id": 999, "body": "x"})
    assert response.status_code == 404
    assert session.audits == []


def test_create_event_note_audits_against_event(client):
    session = _FakeSession(event=SimpleNamespace(event_id=5), user=_actor())
    app.dependency_overrides[get_current_db_user] = lambda: _ctx("full_access")
    app.dependency_overrides[get_session] = _with_session(session)

    response = client.post(
        "/notes", json={"entity_type": "event", "entity_id": 5, "body": "Great turnout"}
    )
    assert response.status_code == 201
    assert response.json()["entity_type"] == "event"
    audit = session.audits[0]
    assert (audit.entity_type, audit.entity_id) == ("event", 5)


def test_list_notes_happy_path(client):
    rows = [_note(note_id=7, body="Newest"), _note(note_id=6, body="Older")]
    session = _FakeSession(alumni=SimpleNamespace(alumni_id=1), user=_actor(), rows=rows)
    app.dependency_overrides[get_current_db_user] = lambda: _ctx("view_only")
    app.dependency_overrides[get_session] = _with_session(session)

    response = client.get("/notes", params={"entity_type": "alumni", "entity_id": 1})
    assert response.status_code == 200
    body = response.json()
    assert [n["note_id"] for n in body] == [7, 6]
    assert all(n["entity_type"] == "alumni" for n in body)
    assert body[0]["author"] == "Tanya Harmon"


def test_update_note_happy_path(client):
    note = Note(
        alumni_id=1,
        interaction_id=None,
        event_id=None,
        body="Old text",
        created_by_user_id=1,
    )
    note.note_id = 7
    session = _FakeSession(alumni=SimpleNamespace(alumni_id=1), user=_actor(), note=note)
    app.dependency_overrides[get_current_db_user] = lambda: _ctx("full_access")
    app.dependency_overrides[get_session] = _with_session(session)

    response = client.patch("/notes/7", json={"body": "New text"})
    assert response.status_code == 200
    assert response.json()["body"] == "New text"
    assert [a.action_type for a in session.audits] == ["update_note"]
    audit = session.audits[0]
    assert (audit.old_value, audit.new_value) == ("Old text", "New text")
    assert (audit.entity_type, audit.entity_id) == ("alumni", 1)


def test_update_note_404_when_missing(client):
    session = _FakeSession(note=None)
    app.dependency_overrides[get_current_db_user] = lambda: _ctx("full_access")
    app.dependency_overrides[get_session] = _with_session(session)

    response = client.patch("/notes/7", json={"body": "x"})
    assert response.status_code == 404
    assert session.audits == []


def test_delete_note_snapshots_body(client):
    note = Note(
        alumni_id=1,
        interaction_id=None,
        event_id=None,
        body="Sensitive note text",
        created_by_user_id=1,
    )
    note.note_id = 7
    session = _FakeSession(alumni=SimpleNamespace(alumni_id=1), note=note)
    app.dependency_overrides[get_current_db_user] = lambda: _ctx("full_access")
    app.dependency_overrides[get_session] = _with_session(session)

    response = client.delete("/notes/7")
    assert response.status_code == 204
    assert session.deleted == [note]
    assert [a.action_type for a in session.audits] == ["delete_note"]
    # Body snapshotted into the audit row before the hard delete (FERPA).
    assert session.audits[0].old_value == "Sensitive note text"
