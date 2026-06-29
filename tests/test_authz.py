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
from app.core.capabilities import DEFAULT_GRANTS
from app.core.security import AuthError, AuthorizationError, DeactivatedAccountError
from app.schemas.auth import AuthenticatedUser, UserContext

AUTH_UUID = "11111111-1111-1111-1111-111111111111"

# The capability guards now resolve against the permission config; DEFAULT_GRANTS
# reproduces the historical hardcoded allow-lists, so these tests assert the
# default (unedited) behaviour.
CONFIG = DEFAULT_GRANTS


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
        must_change_password=False,
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
    assert asyncio.run(auth_deps.require_full_access(ctx, CONFIG)) is ctx
    assert asyncio.run(auth_deps.require_view_only(ctx, CONFIG)) is ctx


def test_view_only_can_read_but_not_write():
    ctx = _ctx("view_only")
    assert asyncio.run(auth_deps.require_view_only(ctx, CONFIG)) is ctx
    with pytest.raises(AuthorizationError):
        asyncio.run(auth_deps.require_full_access(ctx, CONFIG))


def test_no_roles_is_denied_everywhere():
    ctx = _ctx()
    with pytest.raises(AuthorizationError):
        asyncio.run(auth_deps.require_view_only(ctx, CONFIG))
    with pytest.raises(AuthorizationError):
        asyncio.run(auth_deps.require_full_access(ctx, CONFIG))


def test_super_admin_passes_every_guard():
    ctx = _ctx("super_admin")
    assert ctx.is_super_admin
    assert asyncio.run(auth_deps.require_super_admin(ctx, CONFIG)) is ctx
    assert asyncio.run(auth_deps.require_full_access(ctx, CONFIG)) is ctx
    assert asyncio.run(auth_deps.require_view_only(ctx, CONFIG)) is ctx


def test_super_admin_guard_rejects_lesser_roles():
    for role in ("full_access", "student", "view_only"):
        with pytest.raises(AuthorizationError):
            asyncio.run(auth_deps.require_super_admin(_ctx(role), CONFIG))


# --- engineer (top role, above super_admin) -----------------------------------


def test_engineer_passes_every_guard():
    ctx = _ctx("engineer")
    assert ctx.is_engineer
    assert asyncio.run(auth_deps.require_super_admin(ctx, CONFIG)) is ctx
    assert asyncio.run(auth_deps.require_full_access(ctx, CONFIG)) is ctx
    assert asyncio.run(auth_deps.require_alumni_edit(ctx, CONFIG)) is ctx
    assert asyncio.run(auth_deps.require_view_only(ctx, CONFIG)) is ctx
    assert asyncio.run(auth_deps.require_vocab_admin(ctx, CONFIG)) is ctx


# --- student (edit existing only) ---------------------------------------------


def test_student_can_edit_and_read_but_not_create_or_admin():
    ctx = _ctx("student")
    assert ctx.is_student and ctx.can_edit_alumni
    # Read + edit-existing are allowed.
    assert asyncio.run(auth_deps.require_view_only(ctx, CONFIG)) is ctx
    assert asyncio.run(auth_deps.require_alumni_edit(ctx, CONFIG)) is ctx
    # Create / archive / import (full_access), user admin, and vocab admin are not.
    for guard in (
        auth_deps.require_full_access,
        auth_deps.require_super_admin,
        auth_deps.require_vocab_admin,
    ):
        with pytest.raises(AuthorizationError):
            asyncio.run(guard(ctx, CONFIG))


def test_full_access_can_edit_existing():
    ctx = _ctx("full_access")
    assert asyncio.run(auth_deps.require_alumni_edit(ctx, CONFIG)) is ctx


def test_view_only_cannot_edit_existing():
    ctx = _ctx("view_only")
    assert not ctx.can_edit_alumni
    with pytest.raises(AuthorizationError):
        asyncio.run(auth_deps.require_alumni_edit(ctx, CONFIG))


# --- vocab admin (engineer-only) ----------------------------------------------


def test_vocab_admin_is_engineer_only():
    # Controlled-vocabulary administration is the engineer's domain. The engineer
    # is allowed; super_admin (and every lesser role) is forbidden.
    eng = _ctx("engineer")
    assert asyncio.run(auth_deps.require_vocab_admin(eng, CONFIG)) is eng
    for role in ("super_admin", "full_access", "student", "view_only"):
        with pytest.raises(AuthorizationError):
            asyncio.run(auth_deps.require_vocab_admin(_ctx(role), CONFIG))


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
