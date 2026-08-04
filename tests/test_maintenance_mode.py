"""Site-wide maintenance mode.

THE MOST IMPORTANT TEST IN THIS FILE is
``test_engineer_can_authenticate_and_disable_while_maintenance_is_active``. If a
maintenance switch can lock out the person who flipped it, the site is dead and
recovery means hand-editing the production database. Everything else here exists
to keep that property from eroding: the exempt set, the ordering of the gate, and
the fact that the engineer's path never depends on reading the maintenance row.

These tests deliberately do NOT override ``get_current_db_user``. Overriding it
would skip the very gate under test. Instead they override the token layer
(``get_current_user``) and monkeypatch the user lookup, so the real resolver
chain runs: user lookup -> single-session guard -> maintenance gate ->
must-change-password gate -> capability guard.
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
from app.core.security import MaintenanceModeError
from app.main import app
from app.models.maintenance import MaintenanceMode
from app.schemas.auth import AuthenticatedUser, UserContext
from app.services import maintenance

AUTH_UUID = "11111111-1111-1111-1111-111111111111"


@pytest.fixture(autouse=True)
def _clear_maintenance_cache():
    """``read_status`` memoises per process; every test starts from a clean read."""
    maintenance.reset_cache()
    rate_limit.reset()
    yield
    maintenance.reset_cache()
    rate_limit.reset()


# --- Fakes -------------------------------------------------------------------


class FakeResult:
    def __init__(self, rowcount: int = 0) -> None:
        self.rowcount = rowcount


class FakeSession:
    """Minimal AsyncSession stand-in.

    ``scalar`` is routed by what the statement SELECTS, so a test only has to
    declare the rows that exist rather than script an exact call order.
    """

    def __init__(
        self,
        *,
        maintenance_row: MaintenanceMode | None = None,
        user_row: object | None = None,
        user_email: str | None = None,
        rowcount: int = 0,
        raise_on_maintenance_read: bool = False,
    ) -> None:
        self.maintenance_row = maintenance_row
        self.user_row = user_row
        self.user_email = user_email
        self.rowcount = rowcount
        self.raise_on_maintenance_read = raise_on_maintenance_read
        self.added: list[object] = []
        self.executed: list[object] = []
        self.commits = 0

    async def scalar(self, stmt):
        selected = stmt.column_descriptions[0]["name"]
        if selected == "MaintenanceMode":
            if self.raise_on_maintenance_read:
                raise RuntimeError('relation "maintenance_mode" does not exist')
            return self.maintenance_row
        if selected == "email":
            return self.user_email
        return self.user_row

    async def execute(self, stmt):
        self.executed.append(stmt)
        return FakeResult(self.rowcount)

    def add(self, obj) -> None:
        self.added.append(obj)

    async def commit(self) -> None:
        self.commits += 1

    async def rollback(self) -> None:  # pragma: no cover - defensive
        pass


def _audits(session: FakeSession) -> list:
    return [a for a in session.added if type(a).__name__ == "AuditLog"]


def _row(*, enabled: bool, message: str | None = None) -> MaintenanceMode:
    row = MaintenanceMode(id=1)
    row.enabled = enabled
    row.message = message
    row.enabled_at = (
        datetime.datetime(2026, 8, 3, 12, 0, tzinfo=datetime.UTC) if enabled else None
    )
    row.enabled_by_user_id = 9 if enabled else None
    return row


def _db_user(*roles: str, user_id: int = 1, active_session_id: str | None = None):
    """An ORM-shaped user for the monkeypatched lookup."""
    return SimpleNamespace(
        user_id=user_id,
        auth_user_id=uuid.UUID(AUTH_UUID),
        email="engineer@byu.edu",
        first_name="Test",
        last_name="Engineer",
        active=True,
        must_change_password=False,
        active_session_id=active_session_id,
        roles=[SimpleNamespace(role_name=r) for r in roles],
    )


def _client(
    session,
    *,
    roles: tuple[str, ...] | None,
    monkeypatch,
    token_session=None,
    active_session_id: str | None = None,
):
    """A TestClient wired so the REAL auth resolver chain runs.

    ``roles=None`` means unauthenticated (no token override, so the bearer
    dependency raises as it would in production).
    """

    async def _session():
        yield session

    app.dependency_overrides[get_session] = _session
    app.dependency_overrides[get_permission_config] = lambda: dict(DEFAULT_GRANTS)

    if roles is not None:
        async def _fake_lookup(_session, _auth_uuid):
            return _db_user(*roles, active_session_id=active_session_id)

        monkeypatch.setattr(auth_deps, "get_user_with_roles_by_auth_id", _fake_lookup)
        app.dependency_overrides[get_current_user] = lambda: AuthenticatedUser(
            auth_user_id=AUTH_UUID,
            email="engineer@byu.edu",
            session_id=token_session,
        )

    return TestClient(app)


@pytest.fixture
def clean_overrides():
    yield
    app.dependency_overrides.clear()


# --- THE CRITICAL TEST -------------------------------------------------------


def test_engineer_can_authenticate_and_disable_while_maintenance_is_active(
    monkeypatch, clean_overrides
):
    """An engineer can still authenticate and TURN MAINTENANCE OFF while it is ON.

    This is the property the whole design exists to guarantee. It runs the real
    dependency chain (token -> user lookup -> single-session -> maintenance gate
    -> engineer capability) against a database whose maintenance row says
    ENABLED, and asserts the engineer reaches the disable endpoint and flips it.
    """
    session = FakeSession(maintenance_row=_row(enabled=True, message="Back soon."))

    with _client(session, roles=("engineer",), monkeypatch=monkeypatch) as client:
        resp = client.post("/maintenance/disable")

    assert resp.status_code == 200, resp.text
    assert resp.json()["enabled"] is False
    # The switch is actually off in the database, not just in the response.
    assert session.maintenance_row.enabled is False
    assert session.commits == 1
    audit = _audits(session)
    assert len(audit) == 1
    assert audit[0].action_type == "maintenance_mode_disabled"
    assert audit[0].user_id == 1


def test_engineer_reaches_the_console_state_while_maintenance_is_active(
    monkeypatch, clean_overrides
):
    """The engineer can also READ the state while it is on, so the console renders."""
    session = FakeSession(
        maintenance_row=_row(enabled=True, message="Back soon."),
        user_email="engineer@byu.edu",
    )

    with _client(session, roles=("engineer",), monkeypatch=monkeypatch) as client:
        resp = client.get("/maintenance")

    assert resp.status_code == 200, resp.text
    assert resp.json()["enabled"] is True


def test_engineer_can_record_a_fresh_login_while_maintenance_is_active(
    monkeypatch, clean_overrides
):
    """The second half of the recovery path: an engineer whose session is GONE
    can sign in again while maintenance is on.

    If the acting engineer's browser dies (or a different engineer has to take
    over), ``POST /auth/login`` must still claim their session — otherwise every
    data route would keep rejecting them as superseded and the console would be
    unreachable.
    """
    session = FakeSession(
        maintenance_row=_row(enabled=True),
        user_row=_db_user("engineer"),
    )

    with _client(
        session, roles=("engineer",), monkeypatch=monkeypatch, token_session="sess-new"
    ) as client:
        resp = client.post("/auth/login")

    assert resp.status_code == 200, resp.text
    assert session.user_row.active_session_id == "sess-new"


# --- The pause actually pauses ----------------------------------------------


def test_login_is_refused_for_non_engineers_while_maintenance_is_active(
    monkeypatch, clean_overrides
):
    session = FakeSession(
        maintenance_row=_row(enabled=True, message="Back soon."),
        user_row=_db_user("super_admin"),
    )

    with _client(
        session,
        roles=("super_admin",),
        monkeypatch=monkeypatch,
        token_session="sess-x",
    ) as client:
        resp = client.post("/auth/login")

    assert resp.status_code == 503
    assert resp.json()["error"]["code"] == "maintenance_mode"
    # Refused BEFORE any write: the session claim must not undo the force-logout.
    assert session.commits == 0
    assert session.user_row.active_session_id is None


def test_authenticated_routes_are_refused_for_non_engineers(
    monkeypatch, clean_overrides
):
    """The gate is on the strict resolver, so holding a valid token is not enough."""
    session = FakeSession(maintenance_row=_row(enabled=True))

    with _client(session, roles=("super_admin",), monkeypatch=monkeypatch) as client:
        resp = client.get("/maintenance")

    assert resp.status_code == 503
    assert resp.json()["error"]["code"] == "maintenance_mode"


def test_normal_operation_is_untouched_when_maintenance_is_off(
    monkeypatch, clean_overrides
):
    session = FakeSession(
        maintenance_row=_row(enabled=False), user_row=_db_user("super_admin")
    )

    with _client(
        session,
        roles=("super_admin",),
        monkeypatch=monkeypatch,
        token_session="sess-x",
    ) as client:
        resp = client.post("/auth/login")

    assert resp.status_code == 200, resp.text


# --- Who may flip the switch -------------------------------------------------


@pytest.mark.parametrize("path", ["/maintenance/enable", "/maintenance/disable"])
def test_flipping_the_switch_requires_authentication(path, clean_overrides):
    session = FakeSession(maintenance_row=_row(enabled=False))

    async def _session():
        yield session

    app.dependency_overrides[get_session] = _session
    with TestClient(app) as client:
        resp = client.post(path)

    assert resp.status_code == 401
    assert session.commits == 0


@pytest.mark.parametrize("path", ["/maintenance/enable", "/maintenance/disable"])
@pytest.mark.parametrize(
    "role", ["super_admin", "full_access", "student", "view_only"]
)
def test_non_engineers_cannot_flip_the_switch(
    path, role, monkeypatch, clean_overrides
):
    """No non-engineer can enable OR disable — including super_admin.

    Checked with maintenance OFF so the refusal is the capability guard (403),
    not the maintenance gate incidentally shadowing it.
    """
    session = FakeSession(maintenance_row=_row(enabled=False))

    with _client(session, roles=(role,), monkeypatch=monkeypatch) as client:
        resp = client.post(path)

    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "forbidden"
    assert session.commits == 0


def test_non_engineer_cannot_read_the_engineer_state(monkeypatch, clean_overrides):
    session = FakeSession(maintenance_row=_row(enabled=False))

    with _client(session, roles=("super_admin",), monkeypatch=monkeypatch) as client:
        resp = client.get("/maintenance")

    assert resp.status_code == 403


# --- Rate limiting: braked in one direction only -----------------------------


def test_turning_maintenance_on_is_rate_limited(monkeypatch, clean_overrides):
    """A runaway loop can't thrash the switch — enabling has a budget."""
    session = FakeSession(maintenance_row=_row(enabled=False))

    with _client(session, roles=("engineer",), monkeypatch=monkeypatch) as client:
        codes = [client.post("/maintenance/enable").status_code for _ in range(21)]

    assert codes[:20] == [200] * 20
    assert codes[20] == 429


def test_turning_maintenance_OFF_is_never_rate_limited(monkeypatch, clean_overrides):
    """THE RECOVERY DIRECTION MUST NOT HAVE A BUDGET.

    A limiter on disable is itself a lockout: burn the budget — by accident, by a
    retry loop, or deliberately — and the site stays down for the length of the
    window with nothing that can bring it back. Well past the enable budget here,
    so a shared limiter would have tripped.
    """
    session = FakeSession(maintenance_row=_row(enabled=True))

    with _client(session, roles=("engineer",), monkeypatch=monkeypatch) as client:
        codes = [client.post("/maintenance/disable").status_code for _ in range(40)]

    assert set(codes) == {200}


def test_the_enable_limiter_still_refuses_non_engineers(
    monkeypatch, clean_overrides
):
    """The limiter resolves its actor through ``require_engineer``, so wrapping
    the route in it must not weaken the gate."""
    session = FakeSession(maintenance_row=_row(enabled=False))

    with _client(session, roles=("super_admin",), monkeypatch=monkeypatch) as client:
        resp = client.post("/maintenance/enable")

    assert resp.status_code == 403
    assert session.commits == 0


# --- Routes deliberately OUTSIDE the gate ------------------------------------
#
# The maintenance gate lives on the STRICT resolver, so the three routes on the
# force-password-change-exempt resolver are not covered by it. That is a
# decision, not an oversight, and these tests pin it so it stays visible: if
# someone later moves the gate onto the base resolver, the session probe below
# starts failing and takes the force-logout signal down with it.


def test_the_session_probe_still_answers_while_maintenance_is_active(
    monkeypatch, clean_overrides
):
    """The force-logout is only observable because this route stays open.

    A paused user's browser polls it to learn its session was ended and sign
    itself out. If maintenance 503'd it, force-logged-out clients would sit on a
    dead session with no idea, and never reach the maintenance page.
    """
    session = FakeSession(maintenance_row=_row(enabled=True))
    sentinel = maintenance._new_sentinel()

    with _client(
        session,
        roles=("view_only",),
        monkeypatch=monkeypatch,
        token_session="their-real-session",
        active_session_id=sentinel,
    ) as client:
        resp = client.get("/auth/session/active")

    assert resp.status_code == 200
    # The sentinel doesn't match their token's session, so the client is told to
    # sign out — which is exactly what "force logout" means from the browser.
    assert resp.json()["active"] is False


def test_auth_context_still_answers_while_maintenance_is_active(
    monkeypatch, clean_overrides
):
    """Accepted exception: ``/auth/context`` returns the CALLER'S OWN identity
    only — no alumni data, nobody else's record — and the frontend needs it to
    decide that this user is not an engineer and belongs on the maintenance page.
    """
    session = FakeSession(maintenance_row=_row(enabled=True))

    with _client(session, roles=("view_only",), monkeypatch=monkeypatch) as client:
        resp = client.get("/auth/context")

    assert resp.status_code == 200
    assert resp.json()["roles"] == ["view_only"]


# --- The public status endpoint ---------------------------------------------


def test_public_status_needs_no_auth_and_leaks_nothing(clean_overrides):
    """Unauthenticated, and capped to exactly ``{enabled, message}``.

    The key-set assertion is the point: it fails the moment anyone adds the
    actor, a timestamp, a build id, or anything else to the public payload.
    """
    session = FakeSession(
        maintenance_row=_row(enabled=True, message="Back at 5pm."),
        user_email="engineer@byu.edu",
    )

    async def _session():
        yield session

    app.dependency_overrides[get_session] = _session
    with TestClient(app) as client:
        resp = client.get("/maintenance/status")

    assert resp.status_code == 200
    body = resp.json()
    assert set(body) == {"enabled", "message"}
    assert body == {"enabled": True, "message": "Back at 5pm."}


def test_public_status_when_off_reveals_no_message(clean_overrides):
    session = FakeSession(maintenance_row=_row(enabled=False, message="internal note"))

    async def _session():
        yield session

    app.dependency_overrides[get_session] = _session
    with TestClient(app) as client:
        resp = client.get("/maintenance/status")

    assert resp.json() == {"enabled": False, "message": None}


def test_public_status_falls_back_to_default_copy(clean_overrides):
    session = FakeSession(maintenance_row=_row(enabled=True, message=None))

    async def _session():
        yield session

    app.dependency_overrides[get_session] = _session
    with TestClient(app) as client:
        resp = client.get("/maintenance/status")

    assert resp.json() == {"enabled": True, "message": maintenance.DEFAULT_MESSAGE}


# --- enable(): force-logout mechanics ---------------------------------------


def test_enable_stamps_a_sentinel_on_signed_in_non_engineers_only():
    session = FakeSession(maintenance_row=_row(enabled=False), rowcount=4)

    result = asyncio.run(
        maintenance.enable(session, actor_user_id=9, message="Back soon.")
    )

    assert result.enabled is True
    assert result.sessions_ended == 4
    assert session.maintenance_row.enabled is True
    assert session.maintenance_row.enabled_by_user_id == 9

    assert len(session.executed) == 1
    stmt = session.executed[0]
    sql = str(stmt)
    params = stmt.compile().params
    # Engineers are excluded from the force-logout, by role, in SQL.
    assert "NOT IN" in sql
    assert "roles.role_name" in sql
    assert "engineer" in params.values()
    # Only rows that already hold a session are touched (see the service
    # docstring: NULL is the fail-open state and must stay NULL).
    assert "users.active_session_id IS NOT NULL" in sql
    # The stamped value can never equal a real Supabase session id.
    stamped = [
        v
        for v in params.values()
        if isinstance(v, str) and v.startswith(maintenance.SESSION_SENTINEL_PREFIX)
    ]
    assert len(stamped) == 1
    uuid.UUID(stamped[0].removeprefix(maintenance.SESSION_SENTINEL_PREFIX))


def test_enable_audits_the_actor():
    session = FakeSession(maintenance_row=_row(enabled=False), rowcount=2)

    asyncio.run(maintenance.enable(session, actor_user_id=9))

    audit = _audits(session)
    assert len(audit) == 1
    assert audit[0].action_type == "maintenance_mode_enabled"
    assert audit[0].entity_type == "maintenance_mode"
    assert audit[0].user_id == 9
    assert "sessions_ended=2" in audit[0].new_value
    assert session.commits == 1


def test_enable_creates_the_row_when_it_is_missing():
    """The migration seeds the row, but a missing row must not break the switch."""
    session = FakeSession(maintenance_row=None, rowcount=0)

    asyncio.run(maintenance.enable(session, actor_user_id=9))

    rows = [a for a in session.added if isinstance(a, MaintenanceMode)]
    assert len(rows) == 1
    assert rows[0].enabled is True


def test_blank_message_falls_back_to_the_default():
    session = FakeSession(maintenance_row=_row(enabled=False))

    result = asyncio.run(maintenance.enable(session, actor_user_id=9, message="   "))

    assert session.maintenance_row.message is None
    assert result.message == maintenance.DEFAULT_MESSAGE


def test_disable_does_not_restore_the_ended_sessions():
    """Ending maintenance must not resurrect sessions the switch killed, and must
    not touch ``users`` at all (clearing to NULL would revive genuinely
    superseded sessions and break #147)."""
    session = FakeSession(maintenance_row=_row(enabled=True))

    asyncio.run(maintenance.disable(session, actor_user_id=9))

    assert session.executed == []
    assert session.maintenance_row.enabled is False
    assert session.maintenance_row.message is None
    assert session.maintenance_row.enabled_at is None
    assert _audits(session)[0].action_type == "maintenance_mode_disabled"


def test_a_sentinel_never_matches_a_real_session_id():
    """The force-logout rests on this: a Supabase session id is a bare UUID, so a
    prefixed value can never be equal to one."""
    sentinel = maintenance._new_sentinel()
    assert sentinel != maintenance._new_sentinel()
    assert sentinel.startswith(maintenance.SESSION_SENTINEL_PREFIX)
    with pytest.raises(ValueError):
        uuid.UUID(sentinel)


# --- The exempt set ----------------------------------------------------------


def test_only_engineers_are_exempt():
    assert maintenance.is_exempt(["engineer"]) is True
    assert maintenance.is_exempt(["engineer", "super_admin"]) is True
    for role in ("super_admin", "full_access", "student", "view_only"):
        assert maintenance.is_exempt([role]) is False
    assert maintenance.is_exempt([]) is False


def test_the_gate_never_reads_the_database_for_an_engineer():
    """An engineer's access must not depend on the maintenance row being readable.

    The session here raises on any maintenance read; the gate must still let an
    engineer through, because the exemption is decided from roles first.
    """
    session = FakeSession(raise_on_maintenance_read=True)
    ctx = UserContext(user_id=1, auth_user_id=uuid.UUID(AUTH_UUID), roles=["engineer"])

    asyncio.run(auth_deps._enforce_maintenance_mode(session, ctx))


def test_the_gate_fails_open_when_the_switch_cannot_be_read():
    """An unreadable switch resolves to "site is up" — never to a lockout."""
    session = FakeSession(raise_on_maintenance_read=True)
    ctx = UserContext(
        user_id=1, auth_user_id=uuid.UUID(AUTH_UUID), roles=["view_only"]
    )

    asyncio.run(auth_deps._enforce_maintenance_mode(session, ctx))

    assert asyncio.run(maintenance.read_status(session)).enabled is False


def test_the_gate_blocks_a_non_exempt_user():
    session = FakeSession(maintenance_row=_row(enabled=True, message="Back soon."))
    ctx = UserContext(
        user_id=1, auth_user_id=uuid.UUID(AUTH_UUID), roles=["view_only"]
    )

    with pytest.raises(MaintenanceModeError) as exc:
        asyncio.run(auth_deps._enforce_maintenance_mode(session, ctx))

    assert exc.value.message == "Back soon."


def test_the_refusal_is_identical_for_every_account():
    """Anti-enumeration: the refusal is a single site-wide message with no
    account-specific content, so it cannot confirm that an account exists."""
    session = FakeSession(maintenance_row=_row(enabled=True))
    messages = set()
    for role in ("view_only", "student", "full_access", "super_admin"):
        maintenance.reset_cache()
        ctx = UserContext(
            user_id=7, auth_user_id=uuid.UUID(AUTH_UUID), email=f"{role}@byu.edu",
            roles=[role],
        )
        with pytest.raises(MaintenanceModeError) as exc:
            asyncio.run(auth_deps._enforce_maintenance_mode(session, ctx))
        messages.add(exc.value.message)

    assert messages == {maintenance.DEFAULT_MESSAGE}


# --- Cache behaviour ---------------------------------------------------------


def test_disable_publishes_the_off_state_to_this_process_immediately():
    """Turning it off must be felt at once locally, not after the cache TTL."""
    session = FakeSession(maintenance_row=_row(enabled=True))
    assert asyncio.run(maintenance.read_status(session)).enabled is True

    asyncio.run(maintenance.disable(session, actor_user_id=9))

    # Even against a session that would now raise, the cached OFF value stands.
    assert asyncio.run(
        maintenance.read_status(FakeSession(raise_on_maintenance_read=True))
    ).enabled is False


def test_status_is_cached_within_the_ttl():
    session = FakeSession(maintenance_row=_row(enabled=True))
    assert asyncio.run(maintenance.read_status(session)).enabled is True
    # Row flips underneath; the cached value is still served.
    session.maintenance_row.enabled = False
    assert asyncio.run(maintenance.read_status(session)).enabled is True
    maintenance.reset_cache()
    assert asyncio.run(maintenance.read_status(session)).enabled is False
