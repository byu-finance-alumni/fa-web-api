"""Renaming an alumnus must not silently create a duplicate (#627).

Two separate failures made a rename the one edit that could collide in total
silence, and both are covered here:

1. Duplicate detection ran against the PARTIAL payload. ``clean_alumni_payload``
   is ``exclude_unset``, and the fuzzy check needs first name + last name +
   graduation year all present — so a focused edit form that submits only the
   name fields produced a ``cleaned`` with no graduation year and the check did
   nothing at all. It now runs against the effective record (stored row +
   patch).
2. The warning it produced was assigned to ``_warnings`` and dropped on the
   floor. Only ``/preview`` surfaced it, and the focused edit forms don't call
   preview. It now rides back on the write response as ``duplicate_warnings``.

Warn-and-continue, not block: two alumni really can share a name and a year, so
the write still succeeds — the point is that the person doing it is told.
"""

import datetime
import uuid
from types import SimpleNamespace

from fastapi.testclient import TestClient

from app.api.dependencies.auth import get_current_db_user
from app.core.database import get_session
from app.main import app
from app.schemas.auth import UserContext
from app.services import hygiene

# --- The pure half: the effective record the checks are measured against ------


def test_effective_identity_fills_the_legs_the_patch_did_not_send():
    stored = SimpleNamespace(
        byu_id="123456789",
        net_id="jdoe12",
        first_name="Jane",
        last_name="Doe",
        graduation_year=2018,
        employment_status="Employed full-time",
    )
    # A marriage rename: the form sends the surname and nothing else.
    effective = hygiene.effective_identity(stored, {"last_name": "Smith"})

    assert effective["last_name"] == "Smith"
    # The legs the fuzzy check needs, recovered from the stored row. Without
    # these it returns early and reports nothing.
    assert effective["first_name"] == "Jane"
    assert effective["graduation_year"] == 2018


def test_effective_identity_keeps_an_explicit_null_from_the_patch():
    stored = SimpleNamespace(
        byu_id="123456789",
        net_id="jdoe12",
        first_name="Jane",
        last_name="Doe",
        graduation_year=2018,
        employment_status=None,
    )
    # An explicit clear IS "set", so it must win over the stored value rather
    # than being treated as "not supplied".
    effective = hygiene.effective_identity(stored, {"graduation_year": None})

    assert effective["graduation_year"] is None


# --- The wired half: the warning reaches the caller ---------------------------


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


class _FakeSession:
    def __init__(self, scalars=(), execute_rows=(), get_result=None):
        self._scalars = list(scalars)
        self._execute_rows = list(execute_rows)
        self._get_result = get_result
        self.added = []

    def add(self, obj):
        self.added.append(obj)

    async def get(self, model, pk):
        return self._get_result

    async def scalar(self, stmt):
        return self._scalars.pop(0) if self._scalars else None

    async def execute(self, stmt):
        rows = self._execute_rows.pop(0) if self._execute_rows else []
        return _ExecResult(rows)

    async def flush(self):
        pass

    async def commit(self):
        pass

    async def refresh(self, obj):
        now = datetime.datetime(2026, 8, 6, tzinfo=datetime.UTC)
        for attr, default in (
            ("deceased", False),
            ("is_alumni", True),
            ("archived", False),
            ("created_at", now),
            ("updated_at", now),
        ):
            if getattr(obj, attr, None) is None:
                setattr(obj, attr, default)


def _stored_alumnus():
    return SimpleNamespace(
        alumni_id=5,
        byu_id=None,
        net_id=None,
        first_name="Jane",
        last_name="Doe",
        graduation_year=2018,
        employment_status=None,
        archived=False,
        is_alumni=True,
        manually_edited_at=None,
        profile_updated_by_user_id=None,
        spouse_alumni_id=None,
    )


def _with_session(session):
    async def _override():
        yield session

    return _override


def _client(session):
    app.dependency_overrides[get_session] = _with_session(session)
    app.dependency_overrides[get_current_db_user] = lambda: UserContext(
        user_id=1,
        auth_user_id=uuid.UUID("11111111-1111-1111-1111-111111111111"),
        roles=["full_access"],
    )
    return TestClient(app, raise_server_exceptions=False)


def test_rename_into_a_collision_saves_but_reports_the_duplicate():
    # The record being renamed is Jane Doe (2018). The rename makes her Jane
    # Smith — and a DIFFERENT live Jane Smith, class of 2018, already exists.
    collision = SimpleNamespace(alumni_id=77, first_name="Jane", last_name="Smith")
    session = _FakeSession(
        # byu_id / net_id are null on both the stored row and the patch, so the
        # exact-duplicate lookups never run. The one query is the fuzzy match.
        execute_rows=[[collision]],
        get_result=_stored_alumnus(),
    )
    with _client(session) as c:
        resp = c.patch("/alumni/5", json={"last_name": "Smith"})
    app.dependency_overrides.clear()

    # Warn-and-continue: the rename is saved.
    assert resp.status_code == 200
    body = resp.json()
    assert body["last_name"] == "Smith"

    # ...and the collision is reported rather than discarded. The patch carried
    # no graduation year at all; the warning proves the check was measured
    # against the stored 2018 instead.
    assert [w["code"] for w in body["duplicate_warnings"]] == ["possible_duplicate"]
    warning = body["duplicate_warnings"][0]
    assert warning["alumni_id"] == 77
    assert "Class of 2018" in warning["message"]


def test_an_ordinary_edit_reports_no_duplicate_warnings():
    session = _FakeSession(execute_rows=[[]], get_result=_stored_alumnus())
    with _client(session) as c:
        resp = c.patch("/alumni/5", json={"last_name": "Smith"})
    app.dependency_overrides.clear()

    assert resp.status_code == 200
    assert resp.json()["duplicate_warnings"] == []
