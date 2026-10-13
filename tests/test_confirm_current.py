""" "Info confirmed" — ``POST /alumni/{id}/confirm-current`` (#605). Offline.

A staffer has checked a record and nothing on it needs changing. The contract:

  * "Last updated" moves to now and is attributed to the caller — the same
    provenance pair (#285) a real edit stamps — even though no field changed;
  * the current role's ``last_verified_at`` is stamped when there is one, and no
    employment row is invented when there is not;
  * NO data field changes, and ``manually_edited_at`` (which means "a human
    changed a value", #420) is left alone;
  * one ``confirm_current`` audit row, which the version history shows;
  * gated like ``PATCH /alumni/{id}`` (``alumni.edit``): view_only is refused;
  * unknown and archived records 404.

Mirrors tests/test_profile_updated_by.py: a fake session and a monkeypatched
repository ``get``, so no database is needed.
"""

from __future__ import annotations

import asyncio
import datetime
import uuid

import pytest
from fastapi.testclient import TestClient

from app.api.dependencies.auth import get_current_db_user, get_permission_config
from app.core.capabilities import DEFAULT_GRANTS
from app.core.database import get_session
from app.core.errors import NotFoundError
from app.main import app
from app.models.alumni import Alumni
from app.models.audit import AuditLog
from app.models.employment import CurrentEmployment
from app.schemas.auth import UserContext
from app.services import alumni as service
from app.services.alumni_history import HISTORY_ACTIONS

_LONG_AGO = datetime.datetime(2025, 1, 2, 3, 4, tzinfo=datetime.UTC)


class FakeSession:
    """Answers the one current-role lookup with ``role`` (or None)."""

    def __init__(self, role: CurrentEmployment | None = None) -> None:
        self.role = role
        self.added: list[object] = []
        self.committed = 0

    def add(self, obj: object) -> None:
        self.added.append(obj)

    async def scalar(self, stmt: object):
        return self.role

    async def commit(self) -> None:
        self.committed += 1

    async def refresh(self, obj: object) -> None:
        pass

    @property
    def audits(self) -> list[AuditLog]:
        return [o for o in self.added if isinstance(o, AuditLog)]


def _alum(**kw) -> Alumni:
    base = dict(
        alumni_id=1,
        first_name="Jane",
        last_name="Doe",
        graduation_year=2018,
        is_alumni=True,
        archived=False,
        deceased=False,
        created_at=_LONG_AGO,
        updated_at=_LONG_AGO,
    )
    base.update(kw)
    alumnus = Alumni(**base)
    alumnus.profile_updated_by_user_id = 3
    return alumnus


def _patch_get(monkeypatch, value):
    async def fake_get(session, alumni_id):
        return value

    monkeypatch.setattr(service.repo, "get", fake_get)


def _confirm(session, actor=9):
    return asyncio.run(service.confirm_current(session, 1, actor_user_id=actor))


# --- service ------------------------------------------------------------------


def test_it_bumps_updated_at_and_attributes_it_to_the_caller(monkeypatch):
    alumnus = _alum()
    _patch_get(monkeypatch, alumnus)
    before = datetime.datetime.now(datetime.UTC)
    obj = _confirm(FakeSession())
    assert obj.updated_at >= before
    assert obj.profile_updated_by_user_id == 9


def test_it_stamps_the_current_role_as_verified(monkeypatch):
    _patch_get(monkeypatch, _alum())
    role = CurrentEmployment(alumni_id=1, current_employer="Acme", last_verified_at=None)
    session = FakeSession(role)
    obj = _confirm(session)
    assert role.last_verified_at is not None
    # One "now" for both, so the profile and the role agree to the instant.
    assert role.last_verified_at == obj.updated_at
    assert role.current_employer == "Acme"


def test_no_role_on_file_means_no_role_is_created(monkeypatch):
    _patch_get(monkeypatch, _alum())
    session = FakeSession(role=None)
    _confirm(session)
    assert not [o for o in session.added if isinstance(o, CurrentEmployment)]


def test_it_changes_no_data_and_is_not_a_manual_edit(monkeypatch):
    alumnus = _alum(linkedin_url="https://linkedin.example/in/jane")
    _patch_get(monkeypatch, alumnus)
    _confirm(FakeSession())
    assert (alumnus.first_name, alumnus.last_name, alumnus.graduation_year) == (
        "Jane",
        "Doe",
        2018,
    )
    assert alumnus.linkedin_url == "https://linkedin.example/in/jane"
    assert alumnus.manually_edited_at is None
    # The intake-sheet provenance text is not the write path's to touch.
    assert alumnus.profile_updated_by is None


def test_it_writes_one_confirm_current_audit_row(monkeypatch):
    _patch_get(monkeypatch, _alum())
    session = FakeSession()
    _confirm(session, actor=42)
    (entry,) = session.audits
    assert entry.action_type == "confirm_current"
    assert entry.entity_type == "alumni"
    assert entry.entity_id == 1
    assert entry.user_id == 42
    assert entry.field_name is None
    assert entry.change_set_id
    assert session.committed == 1


def test_the_confirmation_shows_in_version_history():
    # tests/test_alumni_history.py fails on an unclassified alumni action; this
    # one is a record event an editor should see ("confirmed by <name>").
    assert "confirm_current" in HISTORY_ACTIONS


def test_without_an_actor_it_neither_attributes_nor_audits(monkeypatch):
    alumnus = _alum()
    _patch_get(monkeypatch, alumnus)
    session = FakeSession()
    asyncio.run(service.confirm_current(session, 1))
    assert alumnus.profile_updated_by_user_id == 3
    assert session.audits == []


def test_an_archived_record_404s(monkeypatch):
    _patch_get(monkeypatch, _alum(archived=True))
    session = FakeSession()
    with pytest.raises(NotFoundError):
        _confirm(session)
    assert session.committed == 0


def test_an_unknown_record_404s(monkeypatch):
    _patch_get(monkeypatch, None)
    with pytest.raises(NotFoundError):
        _confirm(FakeSession())


# --- route --------------------------------------------------------------------

PATH = "/alumni/1/confirm-current"


def _ctx(role: str) -> UserContext:
    return UserContext(
        user_id=9,
        auth_user_id=uuid.UUID("11111111-1111-1111-1111-111111111111"),
        roles=[role],
    )


@pytest.fixture
def client():
    with TestClient(app, raise_server_exceptions=False) as test_client:
        yield test_client
    app.dependency_overrides.clear()


def _use(session, role: str | None) -> None:
    async def _session():
        yield session

    app.dependency_overrides[get_session] = _session
    app.dependency_overrides[get_permission_config] = lambda: dict(DEFAULT_GRANTS)
    if role is not None:
        app.dependency_overrides[get_current_db_user] = lambda: _ctx(role)


def test_route_requires_auth(client):
    _use(FakeSession(), None)
    assert client.post(PATH).status_code == 401


def test_route_forbidden_for_view_only(client):
    _use(FakeSession(), "view_only")
    response = client.post(PATH)
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "forbidden"


@pytest.mark.parametrize("role", ["student", "full_access", "super_admin", "engineer"])
def test_route_returns_the_edit_response_shape(client, monkeypatch, role):
    alumnus = _alum()
    _patch_get(monkeypatch, alumnus)
    _use(FakeSession(), role)
    response = client.post(PATH)
    assert response.status_code == 200
    body = response.json()
    # `AlumniWriteResult`, exactly as PATCH /alumni/{id} returns it.
    assert body["alumni_id"] == 1
    assert body["duplicate_warnings"] == []
    assert body["updated_at"] > _LONG_AGO.isoformat()


def test_route_404s_for_an_archived_record(client, monkeypatch):
    _patch_get(monkeypatch, _alum(archived=True))
    _use(FakeSession(), "full_access")
    assert client.post(PATH).status_code == 404
