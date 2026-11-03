"""The `notes.create` capability: students add profile notes (2026-11-03).

Jake's rule: Finance Society student leaders (role `student`) may ADD a note on
any alumni profile, and may EDIT or DELETE ONLY THE NOTES THEY WROTE. Roles that
hold `notes.manage` keep add / edit / delete-any. view_only stays read-only.

The tests prove the line is where it says it is:

  * the capability is registered, assignable, a declared post-#379 addition, and
    seeded to student + every notes.manage role, never view_only;
  * a student can add an alumni note, edit and delete their own, and is refused
    (403, nothing written) on anyone else's note and on interaction / event
    notes; a missing note is still 404 first;
  * notes.manage roles are unchanged; the rule follows the LIVE config;
  * `can_edit` on every read tells the UI exactly what the server would allow;
  * the seed migration is additive, idempotent, and cannot break an unseeded DB.

Offline: auth, permission config and the session are overridden.
"""

from __future__ import annotations

import datetime
import re
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app.api.dependencies.auth import get_current_db_user, get_permission_config
from app.core import rate_limit
from app.core.capabilities import (
    ALUMNI_FULL_REPLACEMENTS,
    CAPABILITIES_BY_CODE,
    DEFAULT_GRANTS,
    POST_SPLIT_CAPABILITIES,
    POST_SPLIT_ROLE_WIDENINGS,
    Capability,
    effective_capabilities,
)
from app.core.database import get_session
from app.main import app
from app.models.alumni import Alumni
from app.models.audit import AuditLog
from app.models.crm import Interaction
from app.models.event import Event
from app.models.note import Note
from app.models.user import User
from app.schemas.auth import UserContext

MIGRATION = (
    Path(__file__).resolve().parents[1]
    / "database"
    / "migrations"
    / "2026-11-03_notes_create_capability.sql"
)

ME = 1
SOMEONE_ELSE = 2
_STAMP = datetime.datetime(2026, 6, 1, 9, 0, tzinfo=datetime.UTC)


def _ctx(role: str) -> UserContext:
    return UserContext(
        user_id=ME,
        auth_user_id=uuid.UUID("11111111-1111-1111-1111-111111111111"),
        roles=[role],
    )


class _Result:
    def __init__(self, rows):
        self._rows = rows

    def scalars(self):
        return self

    def all(self):
        return list(self._rows)


class _Session:
    def __init__(self, *, note=None, rows=(), archived=False, alumni=True):
        self._note = note
        self._rows = rows
        self._alumni = SimpleNamespace(alumni_id=1, archived=archived) if alumni else None
        self.added: list = []
        self.deleted: list = []
        self.committed = False

    async def get(self, model, pk):
        if model is Alumni:
            return self._alumni
        if model is Interaction:
            return SimpleNamespace(interaction_id=pk, alumni_id=1)
        if model is Event:
            return SimpleNamespace(event_id=pk)
        if model is User:
            return SimpleNamespace(user_id=pk, first_name="Sam", last_name="Lee", email="s@x")
        if model is Note:
            return self._note
        return None

    async def execute(self, stmt):
        return _Result(self._rows)

    def add(self, obj):
        if isinstance(obj, Note) and obj.note_id is None:
            obj.note_id = 100
        self.added.append(obj)

    async def delete(self, obj):
        self.deleted.append(obj)

    async def commit(self):
        self.committed = True

    async def rollback(self):
        pass

    async def refresh(self, obj):
        if isinstance(obj, Note):
            obj.created_at = obj.created_at or _STAMP
            obj.updated_at = obj.updated_at or _STAMP

    @property
    def writes(self):
        return [
            a.action_type
            for a in self.added
            if isinstance(a, AuditLog) and a.action_type != "view_notes"
        ]


def _note(note_id=7, author=ME, **kw) -> Note:
    note = Note(
        alumni_id=kw.get("alumni_id", 1),
        interaction_id=kw.get("interaction_id"),
        event_id=None,
        body=kw.get("body", "Reached out at the fall mixer."),
        created_by_user_id=author,
        updated_by_user_id=author,
    )
    note.note_id = note_id
    note.created_at = _STAMP
    note.updated_at = _STAMP
    return note


@pytest.fixture
def client():
    rate_limit.reset()
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()
    rate_limit.reset()


def _as(role: str, session: _Session, **grants: set) -> None:
    config = dict(DEFAULT_GRANTS)
    for r, caps in grants.items():
        config[r] = frozenset(caps)
    app.dependency_overrides[get_current_db_user] = lambda: _ctx(role)
    app.dependency_overrides[get_permission_config] = lambda: config

    async def _override():
        yield session

    app.dependency_overrides[get_session] = _override


_ALUMNI_NOTE = {"entity_type": "alumni", "entity_id": 1, "body": "Finance Society called."}


# --- the capability itself ----------------------------------------------------


def test_the_capability_is_registered_and_assignable():
    spec = CAPABILITIES_BY_CODE[Capability.NOTES_CREATE]
    assert spec.code == "notes.create"
    assert spec.label == "Add notes"
    assert spec.assignable is True
    text = spec.description.lower()
    assert "you wrote yourself" in text
    assert "write notes" in text  # points at the broader toggle
    assert "/notes" not in spec.description


def test_it_is_a_declared_post_split_addition_not_a_widening():
    assert Capability.NOTES_CREATE in POST_SPLIT_CAPABILITIES
    assert Capability.NOTES_CREATE not in ALUMNI_FULL_REPLACEMENTS
    for codes in POST_SPLIT_ROLE_WIDENINGS.values():
        assert Capability.NOTES_CREATE not in codes


@pytest.mark.parametrize("role", ["student", "full_access", "super_admin", "engineer"])
def test_student_and_every_note_manager_hold_it(role):
    assert Capability.NOTES_CREATE in effective_capabilities(DEFAULT_GRANTS, [role])


def test_every_role_with_notes_manage_holds_it_by_default():
    for role, caps in DEFAULT_GRANTS.items():
        if Capability.NOTES_MANAGE in caps:
            assert Capability.NOTES_CREATE in caps, role


def test_view_only_holds_neither_note_write():
    caps = effective_capabilities(DEFAULT_GRANTS, ["view_only"])
    assert Capability.NOTES_CREATE not in caps
    assert Capability.NOTES_MANAGE not in caps


def test_student_still_does_not_manage_notes():
    assert Capability.NOTES_MANAGE not in effective_capabilities(DEFAULT_GRANTS, ["student"])


# --- student: add ----------------------------------------------------------------


def test_student_can_add_an_alumni_note(client):
    session = _Session()
    _as("student", session)
    response = client.post("/notes", json=_ALUMNI_NOTE)
    assert response.status_code == 201
    body = response.json()
    assert body["entity_type"] == "alumni"
    assert body["can_edit"] is True
    assert session.writes == ["add_note"]
    note = next(a for a in session.added if isinstance(a, Note))
    assert note.created_by_user_id == ME


@pytest.mark.parametrize("entity_type", ["interaction", "event"])
def test_student_cannot_add_interaction_or_event_notes(client, entity_type):
    session = _Session()
    _as("student", session)
    response = client.post("/notes", json={"entity_type": entity_type, "entity_id": 3, "body": "x"})
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "forbidden"
    assert session.added == []


def test_student_cannot_add_a_note_on_an_archived_alumnus(client):
    session = _Session(archived=True)
    _as("student", session)
    assert client.post("/notes", json=_ALUMNI_NOTE).status_code == 404
    assert session.added == []


def test_full_access_can_still_add_on_an_archived_alumnus(client):
    session = _Session(archived=True)
    _as("full_access", session)
    assert client.post("/notes", json=_ALUMNI_NOTE).status_code == 201


# --- student: edit / delete own, refused on others --------------------------------


def test_student_can_edit_own_note(client):
    session = _Session(note=_note(author=ME))
    _as("student", session)
    response = client.patch("/notes/7", json={"body": "Updated by me."})
    assert response.status_code == 200
    assert response.json()["body"] == "Updated by me."
    assert response.json()["can_edit"] is True
    assert session.writes == ["update_note"]


def test_student_can_delete_own_note(client):
    note = _note(author=ME)
    session = _Session(note=note)
    _as("student", session)
    assert client.delete("/notes/7").status_code == 204
    assert session.deleted == [note]
    assert session.writes == ["delete_note"]


@pytest.mark.parametrize("author", [SOMEONE_ELSE, None], ids=["other-user", "no-author"])
def test_student_is_refused_editing_someone_elses_note(client, author):
    note = _note(author=author, body="Original")
    session = _Session(note=note)
    _as("student", session)
    response = client.patch("/notes/7", json={"body": "Hijacked"})
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "forbidden"
    assert note.body == "Original"
    assert session.added == []
    assert not session.committed


@pytest.mark.parametrize("author", [SOMEONE_ELSE, None], ids=["other-user", "no-author"])
def test_student_is_refused_deleting_someone_elses_note(client, author):
    session = _Session(note=_note(author=author))
    _as("student", session)
    response = client.delete("/notes/7")
    assert response.status_code == 403
    assert session.deleted == []
    assert session.added == []


def test_a_missing_note_is_404_not_403_for_a_student(client):
    session = _Session(note=None)
    _as("student", session)
    assert client.patch("/notes/7", json={"body": "x"}).status_code == 404
    assert client.delete("/notes/7").status_code == 404


def test_student_cannot_touch_own_note_on_an_archived_alumnus(client):
    session = _Session(note=_note(author=ME), archived=True)
    _as("student", session)
    assert client.patch("/notes/7", json={"body": "x"}).status_code == 404
    assert client.delete("/notes/7").status_code == 404
    assert session.deleted == []


# --- notes.manage roles unchanged -------------------------------------------------


@pytest.mark.parametrize("role", ["full_access", "super_admin", "engineer"])
def test_managers_still_edit_and_delete_anyones_note(client, role):
    session = _Session(note=_note(author=SOMEONE_ELSE))
    _as(role, session)
    response = client.patch("/notes/7", json={"body": "Corrected."})
    assert response.status_code == 200
    assert response.json()["can_edit"] is True
    assert client.delete("/notes/7").status_code == 204


@pytest.mark.parametrize("role", ["full_access", "super_admin"])
def test_managers_still_add_event_notes(client, role):
    session = _Session()
    _as(role, session)
    response = client.post("/notes", json={"entity_type": "event", "entity_id": 5, "body": "x"})
    assert response.status_code == 201


# --- view_only still read-only -----------------------------------------------------


def test_view_only_still_cannot_write(client):
    session = _Session(note=_note(author=ME))
    _as("view_only", session)
    assert client.post("/notes", json=_ALUMNI_NOTE).status_code == 403
    assert client.patch("/notes/7", json={"body": "x"}).status_code == 403
    assert client.delete("/notes/7").status_code == 403
    assert session.added == [] and session.deleted == []


# --- the rule follows the LIVE config ----------------------------------------------


def test_revoking_notes_create_from_student_closes_writes(client):
    session = _Session(note=_note(author=ME))
    _as(
        "student",
        session,
        student=DEFAULT_GRANTS["student"] - {Capability.NOTES_CREATE},
    )
    assert client.post("/notes", json=_ALUMNI_NOTE).status_code == 403
    assert client.patch("/notes/7", json={"body": "x"}).status_code == 403
    assert client.delete("/notes/7").status_code == 403


def test_granting_notes_manage_to_student_widens_to_anyones_note(client):
    session = _Session(note=_note(author=SOMEONE_ELSE))
    _as("student", session, student=DEFAULT_GRANTS["student"] | {Capability.NOTES_MANAGE})
    assert client.patch("/notes/7", json={"body": "x"}).status_code == 200


def test_a_manager_narrowed_to_notes_create_is_own_only(client):
    session = _Session(note=_note(author=SOMEONE_ELSE))
    _as(
        "full_access",
        session,
        full_access=DEFAULT_GRANTS["full_access"] - {Capability.NOTES_MANAGE},
    )
    assert client.patch("/notes/7", json={"body": "x"}).status_code == 403


# --- can_edit on reads ---------------------------------------------------------------


def _list(client, role, **grants):
    rows = [
        _note(note_id=8, author=ME),
        _note(note_id=7, author=SOMEONE_ELSE),
        _note(note_id=6, author=None),
    ]
    _as(role, _Session(rows=rows), **grants)
    response = client.get("/notes", params={"entity_type": "alumni", "entity_id": 1})
    assert response.status_code == 200
    return {n["note_id"]: n["can_edit"] for n in response.json()}


def test_student_list_marks_only_own_notes_editable(client):
    assert _list(client, "student") == {8: True, 7: False, 6: False}


@pytest.mark.parametrize("role", ["full_access", "super_admin", "engineer"])
def test_manager_list_marks_every_note_editable(client, role):
    assert _list(client, role) == {8: True, 7: True, 6: True}


def test_view_only_list_marks_nothing_editable(client):
    assert _list(client, "view_only") == {8: False, 7: False, 6: False}


# --- the seed migration ------------------------------------------------------------


def _statements() -> str:
    return " ".join(
        line
        for line in MIGRATION.read_text(encoding="utf-8").upper().splitlines()
        if not line.strip().startswith("--")
    )


def test_the_migration_grants_managers_student_and_engineer_only():
    sql = _statements()
    assert "WHERE RC.CAPABILITY_CODE = 'NOTES.MANAGE'" in sql
    assert sql.count("'NOTES.CREATE'") == 3
    assert set(re.findall(r"ROLE_NAME = '([A-Z_]+)'", sql)) == {"STUDENT", "ENGINEER"}
    assert "VIEW_ONLY" not in sql
    # It grants nothing but notes.create.
    granted = set(re.findall(r"SELECT (?:DISTINCT )?\w+\.ROLE_ID, '([A-Z._]+)'", sql))
    assert granted == {"NOTES.CREATE"}


def test_the_migration_never_revokes_and_is_idempotent():
    sql = _statements()
    assert "DELETE FROM" not in sql
    assert "DROP" not in sql
    assert "UPDATE " not in sql
    assert "ALTER " not in sql
    assert sql.count("INSERT INTO ROLE_CAPABILITIES") == 3
    assert sql.count("ON CONFLICT ON CONSTRAINT UQ_ROLE_CAPABILITIES DO NOTHING") == 3


def test_the_migration_cannot_break_an_unseeded_database():
    sql = _statements()
    unguarded = [
        stmt
        for stmt in sql.split("INSERT INTO ROLE_CAPABILITIES")[1:]
        if "EXISTS (SELECT 1 FROM ROLE_CAPABILITIES)" not in stmt
    ]
    assert unguarded == []
