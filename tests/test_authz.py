"""Tests for DB-resolved role authorization.

Fully offline — the user repository is monkeypatched, so no database is touched.
Verifies that authorization is decided from database roles and that the
require_* guards gate correctly.
"""

import asyncio
import uuid
from types import SimpleNamespace

import pytest

from app.api.dependencies import auth as auth_deps
from app.core.security import AuthError, AuthorizationError, DeactivatedAccountError
from app.schemas.auth import AuthenticatedUser, UserContext

AUTH_UUID = "11111111-1111-1111-1111-111111111111"


def _ctx(*roles: str) -> UserContext:
    return UserContext(
        user_id=1,
        auth_user_id=uuid.UUID(AUTH_UUID),
        email="worker@byu.edu",
        roles=list(roles),
    )


def _fake_user(*roles: str, active: bool = True) -> SimpleNamespace:
    return SimpleNamespace(
        user_id=1,
        auth_user_id=uuid.UUID(AUTH_UUID),
        email="worker@byu.edu",
        first_name="Test",
        last_name="Worker",
        active=active,
        roles=[SimpleNamespace(role_name=r) for r in roles],
    )


# --- UserContext --------------------------------------------------------------


def test_user_context_role_helpers():
    full = _ctx("full_access")
    view = _ctx("view_only")
    assert full.is_full_access and not full.is_view_only
    assert view.is_view_only and not view.is_full_access


def test_user_context_from_orm_user():
    ctx = UserContext.from_orm_user(_fake_user("full_access", "view_only"))
    assert ctx.user_id == 1
    assert ctx.email == "worker@byu.edu"
    assert set(ctx.roles) == {"full_access", "view_only"}


# --- require_roles guards -----------------------------------------------------


def test_full_access_passes_both_guards():
    ctx = _ctx("full_access")
    assert asyncio.run(auth_deps.require_full_access(ctx)) is ctx
    assert asyncio.run(auth_deps.require_view_only(ctx)) is ctx


def test_view_only_can_read_but_not_write():
    ctx = _ctx("view_only")
    assert asyncio.run(auth_deps.require_view_only(ctx)) is ctx
    with pytest.raises(AuthorizationError):
        asyncio.run(auth_deps.require_full_access(ctx))


def test_no_roles_is_denied_everywhere():
    ctx = _ctx()
    with pytest.raises(AuthorizationError):
        asyncio.run(auth_deps.require_view_only(ctx))
    with pytest.raises(AuthorizationError):
        asyncio.run(auth_deps.require_full_access(ctx))


def test_super_admin_passes_every_guard():
    ctx = _ctx("super_admin")
    assert ctx.is_super_admin
    assert asyncio.run(auth_deps.require_super_admin(ctx)) is ctx
    assert asyncio.run(auth_deps.require_full_access(ctx)) is ctx
    assert asyncio.run(auth_deps.require_view_only(ctx)) is ctx


def test_super_admin_guard_rejects_lesser_roles():
    for role in ("full_access", "view_only"):
        with pytest.raises(AuthorizationError):
            asyncio.run(auth_deps.require_super_admin(_ctx(role)))


# --- get_current_db_user ------------------------------------------------------


def test_get_current_db_user_resolves_roles(monkeypatch):
    async def fake_lookup(session, auth_uuid):
        assert auth_uuid == uuid.UUID(AUTH_UUID)
        return _fake_user("full_access")

    monkeypatch.setattr(auth_deps, "get_user_with_roles_by_auth_id", fake_lookup)
    current = AuthenticatedUser(auth_user_id=AUTH_UUID, email="worker@byu.edu")

    ctx = asyncio.run(auth_deps.get_current_db_user(current, session=None))
    assert ctx.roles == ["full_access"]


def test_get_current_db_user_unprovisioned_is_forbidden(monkeypatch):
    async def fake_lookup(session, auth_uuid):
        return None

    monkeypatch.setattr(auth_deps, "get_user_with_roles_by_auth_id", fake_lookup)
    current = AuthenticatedUser(auth_user_id=AUTH_UUID)

    with pytest.raises(AuthorizationError):
        asyncio.run(auth_deps.get_current_db_user(current, session=None))


def test_get_current_db_user_inactive_is_blocked(monkeypatch):
    # A deactivated account is blocked here — on EVERY authenticated route — with
    # the dedicated DeactivatedAccountError (a subclass of AuthorizationError, so
    # still 403) that the app logs as its own security event.
    async def fake_lookup(session, auth_uuid):
        return _fake_user("full_access", active=False)

    monkeypatch.setattr(auth_deps, "get_user_with_roles_by_auth_id", fake_lookup)
    current = AuthenticatedUser(auth_user_id=AUTH_UUID)

    with pytest.raises(DeactivatedAccountError):
        asyncio.run(auth_deps.get_current_db_user(current, session=None))
    # And it is still an AuthorizationError for any handler keyed on the base type.
    assert issubclass(DeactivatedAccountError, AuthorizationError)


def test_get_current_db_user_bad_subject_is_unauthorized():
    current = AuthenticatedUser(auth_user_id="not-a-uuid")
    with pytest.raises(AuthError):
        asyncio.run(auth_deps.get_current_db_user(current, session=None))
