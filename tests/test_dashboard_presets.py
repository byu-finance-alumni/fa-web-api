"""Tests for the dashboard quick-filter preset routes.

`GET /dashboard/presets` is readable by any provisioned role (shown on the
dashboard Quick search tab). The `/admin/dashboard-presets` CRUD is restricted to
engineer + super_admin. Route logic runs against minimal in-memory fake sessions
(no database), mirroring tests/test_support_contacts.py.
"""

import uuid

import pytest
from fastapi.testclient import TestClient

from app.api.dependencies.auth import get_current_db_user
from app.core.database import get_session
from app.main import app
from app.models.dashboard_preset import DashboardPreset
from app.schemas.auth import UserContext


def _ctx(*roles: str, user_id: int = 1) -> UserContext:
    return UserContext(
        user_id=user_id,
        auth_user_id=uuid.UUID("11111111-1111-1111-1111-111111111111"),
        roles=list(roles),
    )


async def _no_db_session():
    yield None


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
        if isinstance(obj, DashboardPreset):
            self._created = obj

    async def flush(self):
        if self._created is not None and self._created.dashboard_preset_id is None:
            self._created.dashboard_preset_id = 7

    async def commit(self):
        self.commits += 1

    async def refresh(self, _obj):
        pass


class _OneSession:
    def __init__(self, preset):
        self.preset = preset
        self.added: list = []
        self.deleted: list = []
        self.commits = 0

    async def scalar(self, _stmt):
        return self.preset

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


def _preset(pid=1, label="CFAs in Utah", href="/alumni?cfa=1&state=UT", sort=1):
    p = DashboardPreset(label=label, href=href, sort_order=sort)
    p.dashboard_preset_id = pid
    return p


# --- read (any logged-in role) ----------------------------------------------


def test_list_requires_auth():
    app.dependency_overrides[get_session] = _no_db_session
    with TestClient(app) as client:
        resp = client.get("/dashboard/presets")
    app.dependency_overrides.clear()
    assert resp.status_code == 401


def test_list_allows_view_only():
    _wire(_ListSession([_preset()]), _ctx("view_only"))
    with TestClient(app) as client:
        resp = client.get("/dashboard/presets")
    app.dependency_overrides.clear()
    assert resp.status_code == 200
    assert resp.json() == [
        {
            "dashboard_preset_id": 1,
            "label": "CFAs in Utah",
            "href": "/alumni?cfa=1&state=UT",
            "sort_order": 1,
        }
    ]


# --- engineer + super_admin CRUD --------------------------------------------


@pytest.mark.parametrize("role", ["view_only", "student", "full_access"])
def test_admin_create_forbidden_below_super_admin(role):
    app.dependency_overrides[get_session] = _no_db_session
    app.dependency_overrides[get_current_db_user] = lambda: _ctx(role)
    with TestClient(app) as client:
        resp = client.post(
            "/admin/dashboard-presets",
            json={"label": "X", "href": "/alumni?cfa=1"},
        )
    app.dependency_overrides.clear()
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "forbidden"


@pytest.mark.parametrize("role", ["engineer", "super_admin"])
def test_admin_create_happy_path(role):
    session = _CreateSession()
    _wire(session, _ctx(role, user_id=1))
    with TestClient(app) as client:
        resp = client.post(
            "/admin/dashboard-presets",
            json={"label": "CFAs in Utah", "href": "/alumni?cfa=1&state=UT", "sort_order": 2},
        )
    app.dependency_overrides.clear()
    assert resp.status_code == 201
    body = resp.json()
    assert body["label"] == "CFAs in Utah"
    assert body["dashboard_preset_id"] == 7
    assert any(
        getattr(a, "action_type", None) == "add_dashboard_preset"
        for a in session.added
    )


def test_admin_create_rejects_offsite_href():
    app.dependency_overrides[get_session] = _no_db_session
    app.dependency_overrides[get_current_db_user] = lambda: _ctx("engineer")
    with TestClient(app) as client:
        resp = client.post(
            "/admin/dashboard-presets",
            json={"label": "Bad", "href": "https://evil.example.com"},
        )
    app.dependency_overrides.clear()
    assert resp.status_code == 422


def test_admin_update_changes_field_and_audits():
    session = _OneSession(_preset(pid=3, href="/alumni?cfa=1"))
    _wire(session, _ctx("super_admin", user_id=1))
    with TestClient(app) as client:
        resp = client.patch(
            "/admin/dashboard-presets/3", json={"href": "/alumni?cpa=1"}
        )
    app.dependency_overrides.clear()
    assert resp.status_code == 200
    assert resp.json()["href"] == "/alumni?cpa=1"
    assert any(a.action_type == "update_dashboard_preset" for a in session.added)


def test_admin_delete_removes_and_audits():
    preset = _preset(pid=4)
    session = _OneSession(preset)
    _wire(session, _ctx("engineer", user_id=1))
    with TestClient(app) as client:
        resp = client.delete("/admin/dashboard-presets/4")
    app.dependency_overrides.clear()
    assert resp.status_code == 200
    assert session.deleted == [preset]
    assert any(a.action_type == "delete_dashboard_preset" for a in session.added)


def test_admin_update_404_when_missing():
    session = _OneSession(None)
    _wire(session, _ctx("engineer"))
    with TestClient(app) as client:
        resp = client.patch("/admin/dashboard-presets/999", json={"label": "X"})
    app.dependency_overrides.clear()
    assert resp.status_code == 404
