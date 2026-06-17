"""Tests for the engineer-managed support-contact routes.

`GET /support-contacts` is readable by any provisioned role (shown on the in-app
error screen). The `/admin/support-contacts` CRUD is engineer-only. Route logic
runs against minimal in-memory fake sessions (no database), mirroring
tests/test_admin_user_mgmt.py.
"""

import uuid
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app.api.dependencies.auth import get_current_db_user
from app.core.database import get_session
from app.main import app
from app.models.support_contact import SupportContact
from app.schemas.auth import UserContext


def _ctx(*roles: str, user_id: int = 1) -> UserContext:
    return UserContext(
        user_id=user_id,
        auth_user_id=uuid.UUID("11111111-1111-1111-1111-111111111111"),
        roles=list(roles),
    )


async def _no_db_session():
    yield None


def _contact(cid=1, role="Engineer", name="Eng User", email="eng@byu.edu", sort=1):
    return SimpleNamespace(
        support_contact_id=cid,
        role_label=role,
        name=name,
        email=email,
        sort_order=sort,
    )


class _Scalars:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return self._rows


class _ListSession:
    def __init__(self, rows):
        self.rows = rows

    async def scalars(self, _stmt):
        return _Scalars(self.rows)


class _CreateSession:
    def __init__(self):
        self.added: list = []
        self.commits = 0
        self._created = None

    def add(self, obj):
        self.added.append(obj)
        if isinstance(obj, SupportContact):
            self._created = obj

    async def flush(self):
        if self._created is not None and self._created.support_contact_id is None:
            self._created.support_contact_id = 7

    async def commit(self):
        self.commits += 1

    async def refresh(self, _obj):
        pass


class _OneSession:
    def __init__(self, contact):
        self.contact = contact
        self.added: list = []
        self.deleted: list = []
        self.commits = 0

    async def scalar(self, _stmt):
        return self.contact

    def add(self, obj):
        self.added.append(obj)

    async def commit(self):
        self.commits += 1

    async def refresh(self, _obj):
        pass

    async def delete(self, obj):
        self.deleted.append(obj)


def _wire(session, ctx):
    async def _session():
        yield session

    app.dependency_overrides[get_session] = _session
    app.dependency_overrides[get_current_db_user] = lambda: ctx


# --- read (any logged-in role) ----------------------------------------------


def test_list_requires_auth():
    app.dependency_overrides[get_session] = _no_db_session
    with TestClient(app) as client:
        resp = client.get("/support-contacts")
    app.dependency_overrides.clear()
    assert resp.status_code == 401


def test_list_allows_view_only():
    _wire(_ListSession([_contact(1, "Super Admin", "A", "a@byu.edu", 1)]), _ctx("view_only"))
    with TestClient(app) as client:
        resp = client.get("/support-contacts")
    app.dependency_overrides.clear()
    assert resp.status_code == 200
    body = resp.json()
    assert body == [
        {
            "support_contact_id": 1,
            "role_label": "Super Admin",
            "name": "A",
            "email": "a@byu.edu",
            "sort_order": 1,
        }
    ]


# --- engineer-only CRUD ------------------------------------------------------


@pytest.mark.parametrize("role", ["view_only", "full_access", "super_admin"])
def test_admin_create_forbidden_below_engineer(role):
    app.dependency_overrides[get_session] = _no_db_session
    app.dependency_overrides[get_current_db_user] = lambda: _ctx(role)
    with TestClient(app) as client:
        resp = client.post(
            "/admin/support-contacts",
            json={"role_label": "Engineer", "name": "X", "email": "x@byu.edu"},
        )
    app.dependency_overrides.clear()
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "forbidden"


def test_admin_create_happy_path():
    session = _CreateSession()
    _wire(session, _ctx("engineer", user_id=1))
    with TestClient(app) as client:
        resp = client.post(
            "/admin/support-contacts",
            json={
                "role_label": "Engineer",
                "name": "Eng User",
                "email": "Eng@BYU.edu",
                "sort_order": 2,
            },
        )
    app.dependency_overrides.clear()
    assert resp.status_code == 201
    body = resp.json()
    assert body["role_label"] == "Engineer"
    assert body["email"] == "eng@byu.edu"  # normalized lowercase
    assert body["support_contact_id"] == 7
    audit = next(
        a for a in session.added
        if getattr(a, "action_type", None) == "add_support_contact"
    )
    assert audit.user_id == 1


def test_admin_create_rejects_bad_email():
    app.dependency_overrides[get_session] = _no_db_session
    app.dependency_overrides[get_current_db_user] = lambda: _ctx("engineer")
    with TestClient(app) as client:
        resp = client.post(
            "/admin/support-contacts",
            json={"role_label": "Engineer", "name": "X", "email": "not-an-email"},
        )
    app.dependency_overrides.clear()
    assert resp.status_code == 422


def test_admin_update_changes_field_and_audits():
    contact = SupportContact(role_label="Engineer", name="Old", email="old@byu.edu", sort_order=1)
    contact.support_contact_id = 3
    session = _OneSession(contact)
    _wire(session, _ctx("engineer", user_id=1))
    with TestClient(app) as client:
        resp = client.patch("/admin/support-contacts/3", json={"email": "new@byu.edu"})
    app.dependency_overrides.clear()
    assert resp.status_code == 200
    assert resp.json()["email"] == "new@byu.edu"
    assert any(a.action_type == "update_support_contact" for a in session.added)


def test_admin_delete_removes_and_audits():
    contact = SupportContact(role_label="Engineer", name="Eng", email="eng@byu.edu", sort_order=1)
    contact.support_contact_id = 4
    session = _OneSession(contact)
    _wire(session, _ctx("engineer", user_id=1))
    with TestClient(app) as client:
        resp = client.delete("/admin/support-contacts/4")
    app.dependency_overrides.clear()
    assert resp.status_code == 200
    assert session.deleted == [contact]
    assert any(a.action_type == "delete_support_contact" for a in session.added)


def test_admin_update_404_when_missing():
    session = _OneSession(None)
    _wire(session, _ctx("engineer"))
    with TestClient(app) as client:
        resp = client.patch("/admin/support-contacts/999", json={"name": "X"})
    app.dependency_overrides.clear()
    assert resp.status_code == 404
