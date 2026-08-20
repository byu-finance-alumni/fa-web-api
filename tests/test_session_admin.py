"""Engineer session management: GET /admin/sessions and the two revoke routes.

Covers the gate (engineer only), the listing shape, and — the part that matters —
that a revoke performs BOTH halves and that the half we control genuinely ends
access: the ``users.active_session_id`` sentinel the route writes is fed straight
back into the real ``get_current_db_user`` resolver, which must then reject the
revoked session. Also covers the self-revocation guard in both directions.

Fake sessions/overrides in the style of test_login_failures.py and
test_single_session.py — no DB.
"""

import asyncio
import datetime
import uuid
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.sql.elements import TextClause

from app.api.dependencies import auth as auth_deps
from app.api.dependencies.auth import get_current_db_user
from app.core.database import get_session
from app.core.security import SessionSupersededError
from app.main import app
from app.models.audit import AuditLog
from app.schemas.auth import UserContext
from app.services import auth_sessions

_ACTOR_AUTH_UUID = uuid.UUID("11111111-1111-1111-1111-111111111111")
_TARGET_AUTH_UUID = uuid.UUID("22222222-2222-2222-2222-222222222222")
_SESSION_A = uuid.UUID("aaaaaaaa-0000-0000-0000-000000000001")
_SESSION_B = uuid.UUID("bbbbbbbb-0000-0000-0000-000000000002")

_NOW = datetime.datetime(2026, 8, 18, 12, 0, tzinfo=datetime.UTC)
_FIVE_WEEKS_AGO = _NOW - datetime.timedelta(days=35)


def _ctx(*roles: str, user_id: int = 1, session_id: str | None = None) -> UserContext:
    return UserContext(
        user_id=user_id,
        auth_user_id=_ACTOR_AUTH_UUID,
        roles=list(roles),
        session_id=session_id,
    )


def _engineer(session_id: str | None = "sess-engineer", user_id: int = 1):
    app.dependency_overrides[get_current_db_user] = lambda: _ctx(
        "engineer", user_id=user_id, session_id=session_id
    )


def _target_user(active_session_id: str | None, user_id: int = 9):
    """A stand-in for the ``users`` row the routes load and mutate."""
    return SimpleNamespace(
        user_id=user_id,
        auth_user_id=_TARGET_AUTH_UUID,
        email="colleague@byu.edu",
        first_name="C",
        last_name="W",
        active=True,
        must_change_password=False,
        active_session_id=active_session_id,
        active_session_at=None,
        roles=[SimpleNamespace(role_name="full_access")],
    )


# --- fake sessions ------------------------------------------------------------


class _ListSession:
    """Fake for GET /admin/sessions: the count (a TextClause via ``scalar``),
    the page (``execute`` -> ``.mappings().all()``) and the read-audit."""

    def __init__(self, rows):
        self.rows = rows
        self.added: list = []
        self.commits = 0

    async def scalar(self, _stmt):
        return len(self.rows)

    async def execute(self, _stmt, _params=None):
        rows = self.rows
        return SimpleNamespace(
            mappings=lambda: SimpleNamespace(all=lambda: rows)
        )

    def add(self, obj):
        self.added.append(obj)

    async def commit(self):
        self.commits += 1


class _RevokeSession:
    """Fake for the revoke routes.

    ``deleted_session_owner`` is the auth id the ``auth.sessions`` DELETE returns
    (None = the row was already gone). ``user`` is the ``users`` row the route
    then loads and stamps. ``deleted_statements`` records the raw SQL actually
    issued so a test can assert the Supabase half ran.
    """

    def __init__(self, *, user=None, deleted_session_owner=None, deleted_count=0):
        self.user = user
        self.deleted_session_owner = deleted_session_owner
        self.deleted_count = deleted_count
        self.deleted_statements: list[str] = []
        self.added: list = []
        self.commits = 0
        self.rollbacks = 0

    async def execute(self, stmt, params=None):
        sql = str(stmt)
        if isinstance(stmt, TextClause) and "DELETE FROM auth.sessions" in sql:
            self.deleted_statements.append(sql)
            if "WHERE id =" in sql:
                rows = (
                    [(self.deleted_session_owner,)]
                    if self.deleted_session_owner is not None
                    else []
                )
            else:
                rows = [(uuid.uuid4(),) for _ in range(self.deleted_count)]
            return SimpleNamespace(
                first=lambda: rows[0] if rows else None, all=lambda: rows
            )
        return SimpleNamespace(first=lambda: None, all=lambda: [])

    async def scalar(self, _stmt):
        return self.user

    def add(self, obj):
        self.added.append(obj)

    async def commit(self):
        self.commits += 1

    async def rollback(self):
        self.rollbacks += 1


def _override_session(session):
    async def _session():
        yield session

    app.dependency_overrides[get_session] = _session


def _audit(session) -> AuditLog:
    return next(a for a in session.added if isinstance(a, AuditLog))


# --- the gate -----------------------------------------------------------------


def test_list_sessions_forbidden_below_engineer():
    """A super_admin — the top NON-engineer role — is refused. Live sessions and
    the power to end them are engineer-only, like the login logs next to them."""
    app.dependency_overrides[get_current_db_user] = lambda: _ctx("super_admin")
    _override_session(_ListSession([]))
    with TestClient(app) as client:
        resp = client.get("/admin/sessions")
    app.dependency_overrides.clear()
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "forbidden"


def test_revoke_forbidden_below_engineer_and_changes_nothing():
    session = _RevokeSession(
        user=_target_user("sess-live"), deleted_session_owner=_TARGET_AUTH_UUID
    )
    app.dependency_overrides[get_current_db_user] = lambda: _ctx("super_admin")
    _override_session(session)
    with TestClient(app) as client:
        resp = client.request("DELETE", f"/admin/sessions/{_SESSION_A}")
    app.dependency_overrides.clear()
    assert resp.status_code == 403
    # Refused BEFORE either half ran.
    assert session.deleted_statements == []
    assert session.commits == 0


def test_revoke_user_sessions_forbidden_below_engineer():
    session = _RevokeSession(user=_target_user("sess-live"))
    app.dependency_overrides[get_current_db_user] = lambda: _ctx("super_admin")
    _override_session(session)
    with TestClient(app) as client:
        resp = client.request("DELETE", "/admin/users/9/sessions")
    app.dependency_overrides.clear()
    assert resp.status_code == 403
    assert session.deleted_statements == []


def test_list_sessions_requires_auth():
    async def _no_db():
        yield None

    app.dependency_overrides[get_session] = _no_db
    with TestClient(app) as client:
        resp = client.get("/admin/sessions")
    app.dependency_overrides.clear()
    assert resp.status_code == 401


# --- the listing --------------------------------------------------------------


def _row(session_id, created_at, *, is_active=True, user_id=9, roles=("full_access",)):
    return {
        "session_id": session_id,
        "created_at": created_at,
        "last_active_at": created_at,
        "refreshed_at": None,
        "not_after": None,
        "user_id": user_id,
        "email": "colleague@byu.edu",
        "account_active": True,
        "is_account_active_session": is_active,
        "roles": list(roles),
    }


def test_list_sessions_returns_age_and_marks_own_session_and_audits():
    """The five-week-old session is returned with its age in seconds, and the
    caller's OWN session is flagged so the UI can present self-revocation as the
    deliberate act it is."""
    rows = [
        _row(_SESSION_A, _FIVE_WEEKS_AGO),
        _row(_SESSION_B, _NOW - datetime.timedelta(minutes=5), user_id=1),
    ]
    session = _ListSession(rows)
    _override_session(session)
    _engineer(session_id=str(_SESSION_B))
    with TestClient(app) as client:
        resp = client.get("/admin/sessions")
    app.dependency_overrides.clear()

    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 2
    first, second = body["items"]
    assert first["session_id"] == str(_SESSION_A)
    # ~35 days old; assert the order of magnitude rather than an exact clock read.
    assert first["age_seconds"] > 34 * 24 * 3600
    assert first["is_current"] is False
    assert first["roles"] == ["full_access"]
    assert second["is_current"] is True

    audit = _audit(session)
    assert audit.action_type == "read_active_sessions"
    assert audit.entity_type == "auth_session"
    assert audit.user_id == 1
    assert session.commits == 1


def test_list_sessions_paginates():
    session = _ListSession([])
    _override_session(session)
    _engineer()
    with TestClient(app) as client:
        ok = client.get("/admin/sessions?limit=5&offset=10")
        too_big = client.get("/admin/sessions?limit=201")
        negative = client.get("/admin/sessions?offset=-1")
    app.dependency_overrides.clear()

    assert ok.status_code == 200
    assert ok.json()["limit"] == 5
    assert ok.json()["offset"] == 10
    assert too_big.status_code == 422
    assert negative.status_code == 422


def test_list_sessions_empty_is_a_clean_empty_page():
    """No live sessions is a normal, reassuring state — 200 with an empty list,
    never an error."""
    session = _ListSession([])
    _override_session(session)
    _engineer()
    with TestClient(app) as client:
        resp = client.get("/admin/sessions")
    app.dependency_overrides.clear()
    assert resp.status_code == 200
    assert resp.json()["items"] == []
    assert resp.json()["total"] == 0


# --- revoking one session: both halves ---------------------------------------


def test_revoke_session_deletes_supabase_row_and_stamps_sentinel():
    """The Supabase half (DELETE auth.sessions, which cascades the refresh token)
    AND our half (the active_session_id sentinel) both run, in one commit."""
    user = _target_user(str(_SESSION_A))
    session = _RevokeSession(user=user, deleted_session_owner=_TARGET_AUTH_UUID)
    _override_session(session)
    _engineer()
    with TestClient(app) as client:
        resp = client.request("DELETE", f"/admin/sessions/{_SESSION_A}")
    app.dependency_overrides.clear()

    assert resp.status_code == 200
    body = resp.json()
    assert body == {
        "revoked": True,
        "sessions_deleted": 1,
        "access_revoked": True,
        "self_revoked": False,
        "user_id": 9,
        "email": "colleague@byu.edu",
    }
    # Supabase half: the row really was deleted.
    assert any(
        "DELETE FROM auth.sessions" in s and "WHERE id =" in s
        for s in session.deleted_statements
    )
    # Our half: a sentinel no Supabase session id can equal.
    assert user.active_session_id.startswith(auth_sessions.SESSION_SENTINEL_PREFIX)
    assert user.active_session_id != str(_SESSION_A)
    assert user.active_session_at is not None
    # Both halves land in ONE commit, so a revoke cannot half-apply.
    assert session.commits == 1

    audit = _audit(session)
    assert audit.action_type == "revoke_session"
    assert audit.entity_type == "auth_session"
    assert audit.entity_id == 9
    assert audit.old_value == str(_SESSION_A)


def test_revoke_session_stamps_when_account_has_no_claimed_session():
    """NULL active_session_id is #147's fail-OPEN state, so without a stamp the
    revoked session's access token would still be accepted. Stamp it."""
    user = _target_user(None)
    session = _RevokeSession(user=user, deleted_session_owner=_TARGET_AUTH_UUID)
    _override_session(session)
    _engineer()
    with TestClient(app) as client:
        resp = client.request("DELETE", f"/admin/sessions/{_SESSION_A}")
    app.dependency_overrides.clear()

    assert resp.status_code == 200
    assert resp.json()["access_revoked"] is True
    assert user.active_session_id.startswith(auth_sessions.SESSION_SENTINEL_PREFIX)


def test_revoke_stale_session_does_not_disturb_the_live_one():
    """Revoking an OLD device when the account has since claimed a different
    session must not sign the user out of the session they are actually using —
    #147 already rejects the old one."""
    user = _target_user("sess-current-and-legitimate")
    session = _RevokeSession(user=user, deleted_session_owner=_TARGET_AUTH_UUID)
    _override_session(session)
    _engineer()
    with TestClient(app) as client:
        resp = client.request("DELETE", f"/admin/sessions/{_SESSION_A}")
    app.dependency_overrides.clear()

    assert resp.status_code == 200
    assert resp.json()["access_revoked"] is False
    # Untouched.
    assert user.active_session_id == "sess-current-and-legitimate"
    # The Supabase half still ran — the stale refresh token is gone for good.
    assert session.deleted_statements


def test_revoke_missing_session_is_404_and_rolls_back():
    session = _RevokeSession(user=None, deleted_session_owner=None)
    _override_session(session)
    _engineer()
    with TestClient(app) as client:
        resp = client.request("DELETE", f"/admin/sessions/{_SESSION_A}")
    app.dependency_overrides.clear()

    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "not_found"
    assert session.commits == 0
    assert session.rollbacks == 1


def test_revoke_rejects_a_non_uuid_session_id():
    session = _RevokeSession()
    _override_session(session)
    _engineer()
    with TestClient(app) as client:
        resp = client.request("DELETE", "/admin/sessions/not-a-uuid")
    app.dependency_overrides.clear()
    assert resp.status_code == 422
    assert session.deleted_statements == []


# --- self-revocation ----------------------------------------------------------


def test_revoking_own_session_requires_explicit_confirmation():
    """Signing yourself out is allowed but must be deliberate: without the flag
    the call is refused and NOTHING is written."""
    session = _RevokeSession(
        user=_target_user(str(_SESSION_B)), deleted_session_owner=_ACTOR_AUTH_UUID
    )
    _override_session(session)
    _engineer(session_id=str(_SESSION_B))
    with TestClient(app) as client:
        resp = client.request("DELETE", f"/admin/sessions/{_SESSION_B}")
    app.dependency_overrides.clear()

    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "conflict"
    assert session.deleted_statements == []
    assert session.commits == 0


def test_revoking_own_session_with_confirmation_succeeds():
    user = _target_user(str(_SESSION_B), user_id=1)
    session = _RevokeSession(user=user, deleted_session_owner=_ACTOR_AUTH_UUID)
    _override_session(session)
    _engineer(session_id=str(_SESSION_B))
    with TestClient(app) as client:
        resp = client.request(
            "DELETE", f"/admin/sessions/{_SESSION_B}?confirm_self=true"
        )
    app.dependency_overrides.clear()

    assert resp.status_code == 200
    assert resp.json()["self_revoked"] is True
    assert resp.json()["access_revoked"] is True
    assert user.active_session_id.startswith(auth_sessions.SESSION_SENTINEL_PREFIX)


def test_revoking_your_own_other_device_needs_no_confirmation():
    """Ending a DIFFERENT session on your own account does not sign you out of
    the console, so it is not the act the guard is protecting against."""
    user = _target_user("sess-engineer", user_id=1)
    session = _RevokeSession(user=user, deleted_session_owner=_ACTOR_AUTH_UUID)
    _override_session(session)
    _engineer(session_id="sess-engineer")
    with TestClient(app) as client:
        resp = client.request("DELETE", f"/admin/sessions/{_SESSION_A}")
    app.dependency_overrides.clear()

    assert resp.status_code == 200
    assert resp.json()["self_revoked"] is False
    # The console session survives untouched.
    assert user.active_session_id == "sess-engineer"


def test_revoke_all_for_own_account_requires_confirmation():
    """'Revoke all' on your own account necessarily includes the session you are
    using, so it needs the same explicit act."""
    session = _RevokeSession(user=_target_user("sess-engineer", user_id=1))
    _override_session(session)
    _engineer(user_id=1)
    with TestClient(app) as client:
        resp = client.request("DELETE", "/admin/users/1/sessions")
    app.dependency_overrides.clear()

    assert resp.status_code == 409
    assert session.deleted_statements == []
    assert session.commits == 0


# --- revoking every session for a user ---------------------------------------


def test_revoke_user_sessions_deletes_all_and_always_stamps():
    user = _target_user("sess-live")
    session = _RevokeSession(user=user, deleted_count=3)
    _override_session(session)
    _engineer()
    with TestClient(app) as client:
        resp = client.request("DELETE", "/admin/users/9/sessions")
    app.dependency_overrides.clear()

    assert resp.status_code == 200
    body = resp.json()
    assert body["sessions_deleted"] == 3
    assert body["access_revoked"] is True
    assert body["self_revoked"] is False
    assert user.active_session_id.startswith(auth_sessions.SESSION_SENTINEL_PREFIX)
    assert session.commits == 1

    audit = _audit(session)
    assert audit.action_type == "revoke_user_sessions"
    assert audit.entity_id == 9


def test_revoke_user_sessions_stamps_even_with_zero_supabase_rows():
    """No ``auth.sessions`` rows does not mean no access: an access token already
    in flight is still valid for up to an hour, so our half must still run."""
    user = _target_user("sess-live")
    session = _RevokeSession(user=user, deleted_count=0)
    _override_session(session)
    _engineer()
    with TestClient(app) as client:
        resp = client.request("DELETE", "/admin/users/9/sessions")
    app.dependency_overrides.clear()

    assert resp.status_code == 200
    assert resp.json()["sessions_deleted"] == 0
    assert resp.json()["access_revoked"] is True
    assert user.active_session_id.startswith(auth_sessions.SESSION_SENTINEL_PREFIX)


# --- the proof: our half genuinely ends access -------------------------------


class _NoWriteSession:
    async def execute(self, _stmt):
        return None

    async def commit(self):
        pass


def test_the_stamped_sentinel_is_rejected_by_the_real_resolver(monkeypatch):
    """END-TO-END PROOF for the half we control.

    Revoke a session through the route, then hand the very row the route mutated
    to the REAL ``get_current_db_user`` together with a token still carrying the
    revoked ``session_id``. The resolver every data route depends on must refuse
    it — that is what "revocation ends access immediately" means, as opposed to
    waiting out the access token's remaining ~hour.
    """
    user = _target_user(str(_SESSION_A))
    session = _RevokeSession(user=user, deleted_session_owner=_TARGET_AUTH_UUID)
    _override_session(session)
    _engineer()
    with TestClient(app) as client:
        resp = client.request("DELETE", f"/admin/sessions/{_SESSION_A}")
    app.dependency_overrides.clear()
    assert resp.status_code == 200

    async def _lookup(_session, _auth_id):
        return user  # the SAME object the route just stamped

    monkeypatch.setattr(auth_deps, "get_user_with_roles_by_auth_id", _lookup)
    still_holding_the_token = SimpleNamespace(
        auth_user_id=str(_TARGET_AUTH_UUID), session_id=str(_SESSION_A)
    )
    with pytest.raises(SessionSupersededError):
        asyncio.run(
            auth_deps.get_current_db_user(
                still_holding_the_token, _NoWriteSession()
            )
        )


def test_a_fresh_sign_in_recovers_from_a_self_revoke(monkeypatch):
    """The self-revocation guard prevents an ACCIDENT, not a lockout: the
    sentinel is overwritten by the next sign-in, which runs on the
    force-change-EXEMPT resolver and is therefore never blocked by it."""
    user = _target_user(auth_sessions.new_sentinel(), user_id=1)

    async def _lookup(_session, _auth_id):
        return user

    monkeypatch.setattr(auth_deps, "get_user_with_roles_by_auth_id", _lookup)
    current = SimpleNamespace(
        auth_user_id=str(_TARGET_AUTH_UUID), session_id="sess-brand-new"
    )
    # The EXEMPT resolver (what POST /auth/login uses) resolves fine despite the
    # sentinel — so the sign-in that re-claims the session can always run.
    ctx = asyncio.run(
        auth_deps.get_current_db_user_allow_must_change(current, _NoWriteSession())
    )
    assert ctx.user_id == 1

    # ...and the strict resolver, by contrast, is still refusing the old session.
    with pytest.raises(SessionSupersededError):
        asyncio.run(auth_deps.get_current_db_user(current, _NoWriteSession()))


# --- the stamping rule in isolation ------------------------------------------


@pytest.mark.parametrize(
    ("active", "revoked", "expected"),
    [
        ("sess-a", "sess-a", True),  # the live one -> must stamp
        (None, "sess-a", True),  # fail-open NULL -> must stamp
        ("sess-b", "sess-a", False),  # already superseded -> leave alone
    ],
)
def test_should_stamp_sentinel(active, revoked, expected):
    assert (
        auth_sessions.should_stamp_sentinel(
            active_session_id=active, revoked_session_id=revoked
        )
        is expected
    )


def test_sentinel_can_never_equal_a_supabase_session_id():
    sentinel = auth_sessions.new_sentinel()
    assert sentinel.startswith(auth_sessions.SESSION_SENTINEL_PREFIX)
    with pytest.raises(ValueError):
        uuid.UUID(sentinel)
    assert auth_sessions.new_sentinel() != sentinel
