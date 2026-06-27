"""Tests for the editable permission config (#164): capability resolution,
the engineer permission matrix endpoint, and the toggle endpoint's guards.

Pure-unit tests for ``effective_capabilities`` plus offline route tests that
override the auth + config dependencies (no database), mirroring
tests/test_dashboard_presets.py.
"""

import asyncio
import uuid

import pytest
from fastapi.testclient import TestClient

from app.api.dependencies.auth import get_current_db_user, get_permission_config
from app.api.routes import engineer as engineer_routes
from app.core.capabilities import (
    ALL_CAPABILITY_CODES,
    ASSIGNABLE_CAPABILITY_CODES,
    DEFAULT_GRANTS,
    Capability,
    effective_capabilities,
)
from app.core.database import get_session
from app.main import app
from app.models.user import Role
from app.repositories import permissions as perms_repo
from app.schemas.auth import UserContext


def _ctx(*roles: str, user_id: int = 1) -> UserContext:
    return UserContext(
        user_id=user_id,
        auth_user_id=uuid.UUID("11111111-1111-1111-1111-111111111111"),
        roles=list(roles),
    )


async def _no_db_session():
    yield None


# --- effective_capabilities (pure) -------------------------------------------


def test_engineer_always_holds_every_capability_even_with_empty_config():
    # Hard override: a corrupt/empty config can never lock the engineer out.
    assert effective_capabilities({}, ["engineer"]) == set(ALL_CAPABILITY_CODES)


def test_default_grants_reproduce_historical_guards():
    def caps(*r):
        return effective_capabilities(DEFAULT_GRANTS, list(r))

    assert caps("view_only") == {Capability.VIEW}
    assert caps("student") == {Capability.VIEW, Capability.ALUMNI_EDIT}
    assert caps("full_access") == {
        Capability.VIEW,
        Capability.ALUMNI_EDIT,
        Capability.ALUMNI_FULL,
    }
    assert caps("super_admin") == {
        Capability.VIEW,
        Capability.ALUMNI_EDIT,
        Capability.ALUMNI_FULL,
        Capability.USER_ADMIN,
    }


def test_no_roles_has_no_capabilities():
    assert effective_capabilities(DEFAULT_GRANTS, []) == set()


def test_config_edit_grants_capability_to_a_role():
    # Granting alumni.full to student via the config takes effect immediately.
    config = dict(DEFAULT_GRANTS)
    config["student"] = frozenset(
        {Capability.VIEW, Capability.ALUMNI_EDIT, Capability.ALUMNI_FULL}
    )
    assert Capability.ALUMNI_FULL in effective_capabilities(config, ["student"])


def test_union_across_multiple_roles():
    caps = effective_capabilities(DEFAULT_GRANTS, ["view_only", "student"])
    assert caps == {Capability.VIEW, Capability.ALUMNI_EDIT}


def test_engineer_capability_is_not_assignable():
    assert Capability.ENGINEER not in ASSIGNABLE_CAPABILITY_CODES
    # Every other capability is assignable.
    assert ASSIGNABLE_CAPABILITY_CODES == (
        set(ALL_CAPABILITY_CODES) - {Capability.ENGINEER}
    )


# --- load_grants (real DB read, fail-safe) -----------------------------------


class _GrantsResult:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return self._rows


class _GrantsSession:
    """Returns canned (role_name, capability_code) rows for the grants query."""

    def __init__(self, rows, raise_exc=None):
        self._rows = rows
        self._raise = raise_exc

    async def execute(self, _stmt):
        if self._raise is not None:
            raise self._raise
        return _GrantsResult(self._rows)


def test_load_grants_parses_rows_by_role():
    session = _GrantsSession(
        [
            ("student", "view"),
            ("student", "alumni.edit"),
            ("view_only", "view"),
        ]
    )
    grants = asyncio.run(perms_repo.load_grants(session))
    assert grants["student"] == frozenset({"view", "alumni.edit"})
    assert grants["view_only"] == frozenset({"view"})


def test_load_grants_empty_table_falls_back_to_defaults():
    grants = asyncio.run(perms_repo.load_grants(_GrantsSession([])))
    assert grants == dict(DEFAULT_GRANTS)


def test_load_grants_read_failure_falls_back_to_defaults():
    # Fail-safe: an unreadable config degrades to the historical baseline, never
    # denies everything or raises.
    grants = asyncio.run(
        perms_repo.load_grants(_GrantsSession(None, raise_exc=RuntimeError("boom")))
    )
    assert grants == dict(DEFAULT_GRANTS)


# --- GET /engineer/permissions -----------------------------------------------


def _override_config(config=None):
    app.dependency_overrides[get_permission_config] = lambda: (
        config if config is not None else dict(DEFAULT_GRANTS)
    )


def test_matrix_requires_engineer():
    # A super_admin (lacks the engineer capability) is forbidden.
    app.dependency_overrides[get_session] = _no_db_session
    _override_config()
    app.dependency_overrides[get_current_db_user] = lambda: _ctx("super_admin")
    with TestClient(app) as client:
        resp = client.get("/engineer/permissions")
    app.dependency_overrides.clear()
    assert resp.status_code == 403


def test_matrix_returns_full_config_for_engineer():
    app.dependency_overrides[get_session] = _no_db_session
    _override_config()
    app.dependency_overrides[get_current_db_user] = lambda: _ctx("engineer")
    with TestClient(app) as client:
        resp = client.get("/engineer/permissions")
    app.dependency_overrides.clear()
    assert resp.status_code == 200
    body = resp.json()
    # Engineer row: all capabilities, not editable.
    eng = next(r for r in body["roles"] if r["role"] == "engineer")
    assert eng["editable"] is False
    assert set(eng["capabilities"]) == set(ALL_CAPABILITY_CODES)
    # view_only row: only view, editable.
    prof = next(r for r in body["roles"] if r["role"] == "view_only")
    assert prof["editable"] is True
    assert prof["capabilities"] == [Capability.VIEW]
    # The engineer capability is marked non-assignable in the registry.
    eng_cap = next(c for c in body["capabilities"] if c["code"] == "engineer")
    assert eng_cap["assignable"] is False


# --- GET /admin/role-capabilities (read-only, super_admin) -------------------


def test_role_capabilities_read_allows_super_admin():
    # The capabilities table (#163) lives in the user-admin-gated Users section,
    # so a super_admin can READ the matrix even though only the engineer edits it.
    app.dependency_overrides[get_session] = _no_db_session
    _override_config()
    app.dependency_overrides[get_current_db_user] = lambda: _ctx("super_admin")
    with TestClient(app) as client:
        resp = client.get("/admin/role-capabilities")
    app.dependency_overrides.clear()
    assert resp.status_code == 200
    assert {r["role"] for r in resp.json()["roles"]} >= {
        "super_admin",
        "full_access",
        "student",
        "view_only",
    }


def test_role_capabilities_read_forbidden_for_full_access():
    app.dependency_overrides[get_session] = _no_db_session
    _override_config()
    app.dependency_overrides[get_current_db_user] = lambda: _ctx("full_access")
    with TestClient(app) as client:
        resp = client.get("/admin/role-capabilities")
    app.dependency_overrides.clear()
    assert resp.status_code == 403


# --- PATCH /engineer/permissions guards --------------------------------------


def _engineer_actor():
    app.dependency_overrides[get_session] = _no_db_session
    _override_config()
    app.dependency_overrides[get_current_db_user] = lambda: _ctx(
        "engineer", user_id=9
    )


@pytest.mark.parametrize(
    "payload",
    [
        # The engineer role's grants are fixed.
        {"role": "engineer", "capability": "view", "granted": False},
        # The engineer capability can't be handed to another role.
        {"role": "super_admin", "capability": "engineer", "granted": True},
        # Unknown role / capability.
        {"role": "nobody", "capability": "view", "granted": True},
        {"role": "student", "capability": "made.up", "granted": True},
    ],
)
def test_toggle_rejects_invalid_requests(payload):
    _engineer_actor()
    with TestClient(app) as client:
        resp = client.patch("/engineer/permissions", json=payload)
    app.dependency_overrides.clear()
    assert resp.status_code == 422


def test_toggle_forbidden_for_non_engineer():
    app.dependency_overrides[get_session] = _no_db_session
    _override_config()
    app.dependency_overrides[get_current_db_user] = lambda: _ctx("super_admin")
    with TestClient(app) as client:
        resp = client.patch(
            "/engineer/permissions",
            json={"role": "student", "capability": "alumni.full", "granted": True},
        )
    app.dependency_overrides.clear()
    assert resp.status_code == 403


class _ToggleSession:
    """Minimal session for the toggle happy path: resolves the role and records
    the audit row. The repository calls are monkeypatched, so this only needs to
    return the Role and collect added objects."""

    def __init__(self, role):
        self._role = role
        self.added: list = []
        self.commits = 0

    async def scalar(self, _stmt):
        return self._role

    def add(self, obj):
        self.added.append(obj)

    async def commit(self):
        self.commits += 1


def test_toggle_happy_path_grants_and_audits(monkeypatch):
    role = Role(role_name="student")
    role.role_id = 4
    session = _ToggleSession(role)

    async def _session():
        yield session

    async def _fake_set_grant(s, *, role_id, capability_code, granted):
        assert role_id == 4 and capability_code == "alumni.full" and granted is True
        return True

    async def _fake_load_grants(s):
        cfg = dict(DEFAULT_GRANTS)
        cfg["student"] = frozenset(
            {Capability.VIEW, Capability.ALUMNI_EDIT, Capability.ALUMNI_FULL}
        )
        return cfg

    monkeypatch.setattr(engineer_routes.perms_repo, "set_grant", _fake_set_grant)
    monkeypatch.setattr(engineer_routes.perms_repo, "load_grants", _fake_load_grants)

    app.dependency_overrides[get_session] = _session
    _override_config()
    app.dependency_overrides[get_current_db_user] = lambda: _ctx(
        "engineer", user_id=9
    )
    with TestClient(app) as client:
        resp = client.patch(
            "/engineer/permissions",
            json={"role": "student", "capability": "alumni.full", "granted": True},
        )
    app.dependency_overrides.clear()

    assert resp.status_code == 200
    body = resp.json()
    student = next(r for r in body["roles"] if r["role"] == "student")
    assert "alumni.full" in student["capabilities"]
    assert any(
        getattr(a, "action_type", None) == "grant_capability"
        for a in session.added
    )
    assert session.commits == 1


# --- POST /engineer/preview-log ----------------------------------------------


def test_preview_log_records_audit(monkeypatch):
    session = _ToggleSession(None)

    async def _session():
        yield session

    app.dependency_overrides[get_session] = _session
    _override_config()
    app.dependency_overrides[get_current_db_user] = lambda: _ctx(
        "engineer", user_id=9
    )
    with TestClient(app) as client:
        resp = client.post("/engineer/preview-log", json={"role": "student"})
    app.dependency_overrides.clear()
    assert resp.status_code == 200
    assert any(
        getattr(a, "action_type", None) == "preview_as_role"
        for a in session.added
    )


def test_preview_log_rejects_unknown_role():
    app.dependency_overrides[get_session] = _no_db_session
    _override_config()
    app.dependency_overrides[get_current_db_user] = lambda: _ctx("engineer")
    with TestClient(app) as client:
        resp = client.post("/engineer/preview-log", json={"role": "nobody"})
    app.dependency_overrides.clear()
    assert resp.status_code == 422
