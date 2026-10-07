"""#592 — ``POST /auth/password/change``: the API sets the password itself.

The old flow cleared ``must_change_password`` on the caller's word
(``/auth/password/complete``), so a user could keep the admin-issued temp
password. The new route only clears the flag after the Supabase Admin API has
actually set the new password, and refuses the temp password itself when that is
detectable. All offline: the Admin API call and the reuse check are faked.
"""

import uuid
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app.api.dependencies.auth import get_current_db_user_allow_must_change
from app.api.routes import auth as auth_routes
from app.core.database import get_session
from app.core.errors import ServiceError
from app.main import app
from app.schemas.auth import UserContext
from app.services import maintenance, supabase_admin

AUTH_UUID = uuid.UUID("22222222-2222-2222-2222-222222222222")


def _ctx(*, must_change=True, session_id=None, active_session_id=None, roles=("view_only",)):
    return UserContext(
        user_id=5,
        auth_user_id=AUTH_UUID,
        email="worker@byu.edu",
        roles=list(roles),
        must_change_password=must_change,
        session_id=session_id,
        active_session_id=active_session_id,
    )


class _Session:
    def __init__(self, db_user):
        self.db_user = db_user
        self.added: list = []
        self.commits = 0

    async def scalar(self, _stmt):
        return self.db_user

    def add(self, obj):
        self.added.append(obj)

    async def commit(self):
        self.commits += 1

    async def rollback(self):
        pass


@pytest.fixture
def calls(monkeypatch):
    """Fake the Admin API + reuse check, recording what they were asked."""
    seen: dict = {"set": [], "reuse": None}

    async def _set(auth_user_id, new_password):
        seen["set"].append((auth_user_id, new_password))

    async def _matches(_session, auth_user_id, candidate):
        return seen["reuse"]

    async def _maintenance_off(_session, _user):
        return None

    monkeypatch.setattr(supabase_admin, "set_user_password", _set)
    monkeypatch.setattr(supabase_admin, "password_matches_current", _matches)
    monkeypatch.setattr(auth_routes, "_enforce_maintenance_mode", _maintenance_off)
    return seen


def _post(session, ctx, body):
    async def _s():
        yield session

    app.dependency_overrides[get_session] = _s
    app.dependency_overrides[get_current_db_user_allow_must_change] = lambda: ctx
    try:
        with TestClient(app, raise_server_exceptions=False) as client:
            return client.post("/auth/password/change", json=body)
    finally:
        app.dependency_overrides.pop(get_session, None)
        app.dependency_overrides.pop(get_current_db_user_allow_must_change, None)


def _db_user(must_change=True):
    return SimpleNamespace(user_id=5, must_change_password=must_change)


def test_requires_auth():
    async def _none():
        yield None

    app.dependency_overrides[get_session] = _none
    try:
        with TestClient(app) as client:
            resp = client.post("/auth/password/change", json={"new_password": "x" * 12})
    finally:
        app.dependency_overrides.pop(get_session, None)
    assert resp.status_code == 401


def test_sets_password_then_clears_flag_and_audits(calls):
    user = _db_user()
    session = _Session(user)
    resp = _post(session, _ctx(), {"new_password": "a-brand-new-one"})
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"status": "ok"}
    # The API set the password itself, on the token's own auth identity.
    assert calls["set"] == [(AUTH_UUID, "a-brand-new-one")]
    assert user.must_change_password is False
    audit = next(a for a in session.added if type(a).__name__ == "AuditLog")
    assert (audit.action_type, audit.entity_type, audit.entity_id, audit.user_id) == (
        "password_changed",
        "user",
        5,
        5,
    )
    # The password is never written to the audit trail.
    assert "a-brand-new-one" not in repr(vars(audit))
    assert session.commits == 1


def test_upstream_failure_leaves_flag_set(calls, monkeypatch):
    async def _fail(_auth_user_id, _new_password):
        raise ServiceError("The authentication service rejected the password reset.")

    monkeypatch.setattr(supabase_admin, "set_user_password", _fail)
    user = _db_user()
    session = _Session(user)
    resp = _post(session, _ctx(), {"new_password": "a-brand-new-one"})
    assert resp.status_code == 502
    assert user.must_change_password is True
    assert session.added == []
    assert session.commits == 0


def test_refuses_the_current_temp_password(calls):
    calls["reuse"] = True
    user = _db_user()
    session = _Session(user)
    resp = _post(session, _ctx(), {"new_password": "TempPass-1234"})
    assert resp.status_code == 422
    assert "temporary" in resp.json()["error"]["message"]
    assert calls["set"] == []
    assert user.must_change_password is True


def test_unknown_reuse_answer_does_not_block(calls):
    calls["reuse"] = None  # the check could not run
    session = _Session(_db_user())
    resp = _post(session, _ctx(), {"new_password": "a-brand-new-one"})
    assert resp.status_code == 200, resp.text
    assert len(calls["set"]) == 1


@pytest.mark.parametrize(
    ("password", "fragment"),
    [
        ("short7!", "at least 8"),
        ("x" * 73, "72 characters or fewer"),
        # 25 x 3-byte chars = 75 bytes: under 72 CHARACTERS, over bcrypt's limit.
        ("€" * 25, "72 characters or fewer"),
        ("Worker@BYU.edu", "email"),
        ("  worker@byu.edu  ", "email"),
    ],
)
def test_strength_rules_match_the_app(calls, password, fragment):
    user = _db_user()
    session = _Session(user)
    resp = _post(session, _ctx(), {"new_password": password})
    assert resp.status_code == 422
    assert fragment in resp.json()["error"]["message"]
    assert calls["set"] == []
    assert user.must_change_password is True


def test_refused_when_no_change_is_pending(calls):
    """Not a general change-my-password route: without the flag, a stolen access
    token must not be able to set a permanent password."""
    session = _Session(_db_user(must_change=False))
    resp = _post(session, _ctx(must_change=False), {"new_password": "a-brand-new-one"})
    assert resp.status_code == 409
    assert calls["set"] == []


def test_superseded_session_is_refused(calls):
    session = _Session(_db_user())
    resp = _post(
        session,
        _ctx(session_id="old-device", active_session_id="new-device"),
        {"new_password": "a-brand-new-one"},
    )
    assert resp.status_code in (401, 403)
    assert resp.json()["error"]["code"] == "session_superseded"
    assert calls["set"] == []


def test_maintenance_mode_refuses_non_engineers(calls, monkeypatch):
    # Undo the fixture's bypass and drive the real gate with maintenance ON.
    from app.api.dependencies import auth as auth_deps

    async def _on(_session):
        return SimpleNamespace(enabled=True, message=None)

    monkeypatch.setattr(
        auth_routes, "_enforce_maintenance_mode", auth_deps._enforce_maintenance_mode
    )
    monkeypatch.setattr(maintenance, "read_status", _on)
    session = _Session(_db_user())
    resp = _post(session, _ctx(), {"new_password": "a-brand-new-one"})
    assert resp.status_code == 503
    assert calls["set"] == []


def test_rejects_unknown_fields_and_ignores_no_user_id(calls):
    session = _Session(_db_user())
    resp = _post(session, _ctx(), {"new_password": "a-brand-new-one", "user_id": 999})
    assert resp.status_code == 422
    assert calls["set"] == []


def test_is_rate_limited_per_user(calls):
    for _ in range(5):
        _post(_Session(_db_user()), _ctx(), {"new_password": "short"})
    resp = _post(_Session(_db_user()), _ctx(), {"new_password": "a-brand-new-one"})
    assert resp.status_code == 429
    assert calls["set"] == []


def test_old_complete_route_still_works(calls):
    """Kept during the rollout so app builds that predate the switch still get
    through the gate (#592 follow-up removes it)."""
    user = _db_user()
    session = _Session(user)

    async def _s():
        yield session

    app.dependency_overrides[get_session] = _s
    app.dependency_overrides[get_current_db_user_allow_must_change] = lambda: _ctx()
    try:
        with TestClient(app) as client:
            resp = client.post("/auth/password/complete")
    finally:
        app.dependency_overrides.pop(get_session, None)
        app.dependency_overrides.pop(get_current_db_user_allow_must_change, None)
    assert resp.status_code == 200
    assert user.must_change_password is False


# --- the reuse check itself ---------------------------------------------------


class _ScalarSession:
    def __init__(self, result=None, error=None):
        self.result = result
        self.error = error
        self.params = None
        self.rolled_back = False

    async def scalar(self, _stmt, params=None):
        self.params = params
        if self.error is not None:
            raise self.error
        return self.result

    async def rollback(self):
        self.rolled_back = True


@pytest.mark.parametrize(("result", "expected"), [(True, True), (False, False), (None, None)])
def test_password_matches_current_maps_the_db_answer(result, expected):
    import asyncio

    session = _ScalarSession(result=result)
    got = asyncio.run(supabase_admin.password_matches_current(session, AUTH_UUID, "pw-123456"))
    assert got is expected
    # The candidate travels as a bind parameter, never spliced into SQL.
    assert session.params == {"candidate": "pw-123456", "auth_user_id": AUTH_UUID}


def test_password_matches_current_fails_open_without_logging_the_password(caplog):
    import asyncio

    session = _ScalarSession(error=RuntimeError("boom: candidate='pw-secret-1'"))
    got = asyncio.run(
        supabase_admin.password_matches_current(session, AUTH_UUID, "pw-secret-1")
    )
    assert got is None
    assert session.rolled_back is True
    assert "pw-secret-1" not in caplog.text
