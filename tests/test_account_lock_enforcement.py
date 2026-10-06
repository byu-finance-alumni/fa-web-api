"""A hard-locked account is refused AFTER authentication (2026-10-06).

The hard lock (``users.locked_at``, armed by app/services/login_lockout.py) used
to be enforced ONLY by the frontend's unauthenticated pre-login check. Nothing on
the API side read it, so a locked user who already held a token — or who signed
in straight against Supabase, skipping our frontend — was served normally. And
the pre-login check's ``locked`` answer was itself an anonymous "this is a real,
locked staff account" oracle.

The fix moves enforcement to the auth resolver (403 / ``account_locked`` on every
authenticated route) and makes the pre-login routes lock-blind. These tests pin
the resolver half:

  * a locked account is refused on the base resolver, the strict one, and
    ``POST /auth/login`` — where it must not claim the single active session
    (#147) or clear its failed-login counter;
  * ENGINEERS ARE NEVER REFUSED for a lock — no one else can reset their
    password (#178 ceiling), so an enforced lock would have no way back;
  * the super_admin password reset — the documented unlock — still restores
    access.

Like test_maintenance_mode.py, these override only the TOKEN layer and patch the
user lookup, so the real resolver chain runs.
"""

import asyncio
import datetime
import uuid
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app.api.dependencies import auth as auth_deps
from app.api.dependencies.auth import get_current_user, get_permission_config
from app.core import rate_limit
from app.core.capabilities import DEFAULT_GRANTS
from app.core.database import get_session
from app.core.security import AccountLockedError, DeactivatedAccountError
from app.main import app
from app.schemas.auth import AuthenticatedUser
from app.services import auth_sessions, login_lockout, maintenance

AUTH_UUID = "44444444-4444-4444-4444-444444444444"
LOCKED_AT = datetime.datetime(2026, 10, 2, 3, 0, tzinfo=datetime.UTC)


@pytest.fixture(autouse=True)
def _clean():
    maintenance.reset_cache()
    rate_limit.reset()
    yield
    app.dependency_overrides.clear()
    maintenance.reset_cache()
    rate_limit.reset()


def _db_user(*roles: str, locked: bool = True, active: bool = True):
    return SimpleNamespace(
        user_id=4,
        auth_user_id=uuid.UUID(AUTH_UUID),
        email="Locked@BYU.edu",
        first_name="L",
        last_name="U",
        active=active,
        must_change_password=False,
        locked_at=LOCKED_AT if locked else None,
        locked_reason=login_lockout.LOCK_REASON_TOO_MANY_FAILED if locked else None,
        active_session_id="sess-old",
        active_session_at=None,
        last_login_at=None,
        roles=[SimpleNamespace(role_name=r) for r in roles],
    )


class _Session:
    """Answers the user lookup inside ``POST /auth/login`` and the maintenance
    read (no row = off); records every write so a refusal can prove it wrote
    nothing."""

    def __init__(self, user):
        self.user = user
        self.added: list = []
        self.executed: list = []
        self.commits = 0

    async def scalar(self, stmt):
        if stmt.column_descriptions[0]["name"] == "MaintenanceMode":
            return None
        return self.user

    async def execute(self, stmt):
        self.executed.append(stmt)

    def add(self, obj):
        self.added.append(obj)

    async def commit(self):
        self.commits += 1

    async def rollback(self):
        pass


def _client(session, user, monkeypatch, token_session="sess-new"):
    async def _session():
        yield session

    async def _lookup(_session, _auth_uuid):
        return user

    async def _live(_session, _sid):
        return datetime.datetime.now(datetime.UTC)

    monkeypatch.setattr(auth_deps, "get_user_with_roles_by_auth_id", _lookup)
    monkeypatch.setattr(auth_sessions, "live_session_created_at", _live)
    app.dependency_overrides[get_session] = _session
    app.dependency_overrides[get_permission_config] = lambda: dict(DEFAULT_GRANTS)
    app.dependency_overrides[get_current_user] = lambda: AuthenticatedUser(
        auth_user_id=AUTH_UUID, email="locked@byu.edu", session_id=token_session
    )
    return TestClient(app)


def _resolve(user, monkeypatch, resolver):
    async def _lookup(_session, _auth_uuid):
        return user

    monkeypatch.setattr(auth_deps, "get_user_with_roles_by_auth_id", _lookup)
    current = SimpleNamespace(auth_user_id=AUTH_UUID, session_id=None)
    return asyncio.run(resolver(current, _Session(user)))


# --- refused everywhere -------------------------------------------------------


@pytest.mark.parametrize(
    "resolver",
    [
        auth_deps.get_current_db_user_allow_must_change,
        auth_deps.get_current_db_user,
    ],
)
@pytest.mark.parametrize("role", ["super_admin", "full_access", "student", "view_only"])
def test_both_resolvers_refuse_a_locked_non_engineer(monkeypatch, resolver, role):
    with pytest.raises(AccountLockedError):
        _resolve(_db_user(role), monkeypatch, resolver)


@pytest.mark.parametrize("path", ["/auth/context", "/auth/session/active", "/alumni"])
def test_a_locked_token_gets_403_account_locked(monkeypatch, path):
    """The exempt resolver's routes and a data route alike: a valid token for a
    locked account is a 403 with its own machine code, not data."""
    user = _db_user("full_access")
    with _client(_Session(user), user, monkeypatch) as client:
        resp = client.get(path)
    assert resp.status_code == 403, resp.text
    assert resp.json()["error"]["code"] == "account_locked"


def test_a_locked_sign_in_claims_nothing_and_clears_nothing(monkeypatch):
    """A locked user who signs in directly against Supabase and calls
    ``POST /auth/login`` must not become the active session (#147), must not
    clear the failed-login counter, and must not get a login_events row."""
    user = _db_user("full_access")
    session = _Session(user)
    with _client(session, user, monkeypatch) as client:
        resp = client.post("/auth/login")
    assert resp.status_code == 403, resp.text
    assert resp.json()["error"]["code"] == "account_locked"
    assert user.active_session_id == "sess-old"
    assert user.last_login_at is None
    assert session.executed == []  # no login_attempts DELETE
    assert session.added == []
    assert session.commits == 0


def test_deactivation_still_wins_over_the_lock(monkeypatch):
    """Order is unchanged: a deactivated account keeps its own error/event."""
    with pytest.raises(DeactivatedAccountError):
        _resolve(
            _db_user("full_access", active=False),
            monkeypatch,
            auth_deps.get_current_db_user_allow_must_change,
        )


def test_an_unlocked_account_is_unaffected(monkeypatch):
    ctx = _resolve(
        _db_user("view_only", locked=False),
        monkeypatch,
        auth_deps.get_current_db_user,
    )
    assert ctx.user_id == 4


# --- the engineer can never be locked out ------------------------------------


def test_a_locked_engineer_is_not_refused(monkeypatch):
    """No one can reset the engineer's password but an engineer (#178 ceiling),
    and the lock can be armed by anonymous reports — so enforcing it here would
    let a stranger brick the only account that can recover the others."""
    ctx = _resolve(_db_user("engineer"), monkeypatch, auth_deps.get_current_db_user)
    assert ctx.is_engineer


def test_a_locked_engineer_can_still_sign_in_and_claim_the_session(monkeypatch):
    user = _db_user("engineer")
    session = _Session(user)
    with _client(session, user, monkeypatch) as client:
        resp = client.post("/auth/login")
    assert resp.status_code == 200, resp.text
    assert user.active_session_id == "sess-new"
    # The lock stays RECORDED for visibility; it just isn't enforced.
    assert user.locked_at == LOCKED_AT


# --- the documented unlock still works ---------------------------------------


def test_super_admin_reset_restores_access(monkeypatch):
    """The recovery path end to end: locked -> refused; super_admin resets the
    password (clears locked_at) -> the same account resolves again (onto the
    force-change screen, which is the exempt resolver's job to allow)."""
    from app.api.dependencies.auth import get_current_db_user
    from app.api.routes import admin as admin_routes
    from app.schemas.auth import UserContext

    user = _db_user("full_access")
    with pytest.raises(AccountLockedError):
        _resolve(user, monkeypatch, auth_deps.get_current_db_user_allow_must_change)

    async def _fake_set_password(_auth_user_id, _pw):
        return None

    monkeypatch.setattr(admin_routes, "set_user_password", _fake_set_password)
    session = _Session(user)

    async def _session():
        yield session

    app.dependency_overrides[get_session] = _session
    app.dependency_overrides[get_permission_config] = lambda: dict(DEFAULT_GRANTS)
    app.dependency_overrides[get_current_db_user] = lambda: UserContext(
        user_id=1,
        auth_user_id=uuid.UUID("11111111-1111-1111-1111-111111111111"),
        roles=["super_admin"],
    )
    with TestClient(app) as client:
        resp = client.post("/admin/users/4/reset-password")
    assert resp.status_code == 200, resp.text
    assert user.locked_at is None

    ctx = _resolve(user, monkeypatch, auth_deps.get_current_db_user_allow_must_change)
    assert ctx.user_id == 4
    assert ctx.must_change_password is True
