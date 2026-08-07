"""#423: the unauthenticated pre-login routes' rate limit and record retention.

`POST /auth/login/precheck` and `POST /auth/login/record` run before anyone has a
session, so they are open to the internet. `record` upserts a `login_attempts`
row keyed on the caller's own email string and inserts a permanent
`login_failures` row on every failure, and neither table had any retention. The
only claimed brake was a WAF rule that cannot be verified from this repo.

Four things are asserted here, in this order:

  1. Both routes now refuse a flood, at the real production budgets.
  2. The refusal is keyed on the CLIENT IP — the trusted rightmost forwarded hop,
     not the spoofable leftmost one — and never on the submitted email.
  3. Nothing about the lockout or the anti-enumeration contract changed: a
     registered account still hard-locks, and a registered and an unregistered
     address are still indistinguishable from the response.
  4. The retention purge deletes rows past their window and NOTHING else — in
     particular it cannot unlock a locked account.

(3) and (4) are the ones that matter most: a rate limit that quietly weakened the
lockout, or a purge that reset a counter under an attacker's feet, would be a
worse bug than the one being fixed.
"""

import asyncio
import datetime
import time
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.api.routes import auth as auth_routes
from app.core import rate_limit
from app.core.database import get_session
from app.main import app
from app.models.login_attempt import LoginAttempt
from app.models.login_failure import LoginFailure
from app.services import login_lockout


def _now() -> datetime.datetime:
    return datetime.datetime.now(datetime.UTC)


# --- fake session -------------------------------------------------------------


class _Session:
    """In-memory stand-in covering everything the two routes touch.

    ``user`` is the registered account for the attempted email (None = the email
    belongs to nobody). ``execute`` swallows the retention purge's DELETEs — the
    purge is exercised for real against SQLite further down.
    """

    def __init__(self, user=None):
        self.user = user
        self.attempts: dict = {}
        self.added: list = []
        self.executed: list = []
        self.commits = 0

    async def scalar(self, _stmt):
        return self.user

    async def get(self, model, pk):
        return self.attempts.get(pk) if model is LoginAttempt else None

    def add(self, obj):
        if isinstance(obj, LoginAttempt):
            self.attempts[obj.email_lc] = obj
        self.added.append(obj)

    async def delete(self, obj):
        if isinstance(obj, LoginAttempt):
            self.attempts.pop(obj.email_lc, None)

    async def execute(self, stmt):
        self.executed.append(stmt)

    async def commit(self):
        self.commits += 1

    async def rollback(self):
        pass


@pytest.fixture
def client():
    """A TestClient wired to a fresh fake session, with the purge DISARMED.

    The purge throttle is a module global, so it is stamped to "just ran" here:
    the route tests below are about the limiter and the lockout, and none of them
    should trigger — or accidentally depend on the state left by — retention. The
    two that do want it re-arm the clock themselves.
    """
    session = _Session()

    async def _session():
        yield session

    app.dependency_overrides[get_session] = _session
    auth_routes._last_purge_at = time.monotonic()
    with TestClient(app) as c:
        yield c, session
    app.dependency_overrides.clear()
    auth_routes._reset_purge_clock()


def _record(client, email="probe@byu.edu", **kwargs):
    return client.post(
        "/auth/login/record", json={"email": email, "success": False}, **kwargs
    )


def _precheck(client, email="probe@byu.edu", **kwargs):
    return client.post("/auth/login/precheck", json={"email": email}, **kwargs)


# --- 1. the limiter rejects a flood ------------------------------------------


def test_record_rejects_a_flood_at_the_configured_budget(client):
    c, _ = client
    for i in range(rate_limit.LOGIN_RECORD_LIMIT):
        assert _record(c).status_code == 200, f"request {i} should be inside budget"
    over = _record(c)
    assert over.status_code == 429
    assert over.headers["Retry-After"]
    # The 429 goes through the app's standard error envelope, not a bare detail.
    assert over.json()["error"]["code"]


def test_precheck_rejects_a_flood_at_the_configured_budget(client):
    c, _ = client
    for i in range(rate_limit.LOGIN_PRECHECK_LIMIT):
        assert _precheck(c).status_code == 200, f"request {i} should be inside budget"
    assert _precheck(c).status_code == 429


def test_the_two_routes_have_independent_budgets(client):
    """Separate buckets: exhausting `record` must not close the (read-only)
    pre-check, which is the call the frontend makes on the way IN."""
    c, _ = client
    for _ in range(rate_limit.LOGIN_RECORD_LIMIT):
        _record(c)
    assert _record(c).status_code == 429
    assert _precheck(c).status_code == 200


# --- 2. it keys on the client IP, and on the TRUSTED hop ----------------------


def test_budget_is_per_ip_not_per_email(client):
    """Rotating the email must not buy a fresh budget, and — the point of keying
    on the IP rather than the address — a 429 must not be something an attacker
    can inflict on one specific account."""
    c, _ = client
    for i in range(rate_limit.LOGIN_RECORD_LIMIT):
        _record(c, email=f"victim{i}@byu.edu")
    assert _record(c, email="somebody-completely-different@byu.edu").status_code == 429


def test_a_different_client_ip_gets_its_own_budget(client):
    c, _ = client
    for _ in range(rate_limit.LOGIN_RECORD_LIMIT):
        _record(c)
    assert _record(c).status_code == 429
    # The edge-set header names a different caller, who is unaffected.
    other = _record(c, headers={"x-vercel-forwarded-for": "203.0.113.7"})
    assert other.status_code == 200


def test_spoofed_leftmost_forwarded_hop_does_not_refresh_the_budget(client):
    """A proxy chain APPENDS hops, so the leftmost X-Forwarded-For value is
    whatever the caller wrote. Keying on it would let an attacker mint a new
    identity per request; the limiter takes the rightmost (edge-added) hop."""
    c, _ = client
    for i in range(rate_limit.LOGIN_RECORD_LIMIT):
        _record(c, headers={"x-forwarded-for": f"10.0.0.{i % 250}, 198.51.100.4"})
    over = _record(c, headers={"x-forwarded-for": "10.0.99.99, 198.51.100.4"})
    assert over.status_code == 429


# --- 3. the lockout and anti-enumeration are unchanged ------------------------


def test_lockout_still_trips_through_the_route(client):
    """The whole feature this route exists for: LOCK_THRESHOLD failures against a
    REGISTERED address still hard-lock the account. A sticky lock under attack is
    the intended behaviour, not something the new brakes may soften."""
    c, session = client
    user = SimpleNamespace(user_id=2, email="alum@byu.edu", locked_at=None, locked_reason=None)
    session.user = user

    for _ in range(login_lockout.LOCK_THRESHOLD - 1):
        assert _record(c, email="alum@byu.edu").status_code == 200
    assert user.locked_at is None

    body = _record(c, email="alum@byu.edu").json()
    assert user.locked_at is not None
    assert user.locked_reason == login_lockout.LOCK_REASON_TOO_MANY_FAILED
    assert body["reason"] == "locked"
    assert body["allowed"] is False


def test_cooldown_still_trips_through_the_route(client):
    c, _ = client
    for _ in range(login_lockout.COOLDOWN_THRESHOLD - 1):
        assert _record(c).status_code == 200
    body = _record(c).json()
    assert body["reason"] == "cooldown"
    assert body["allowed"] is False
    assert body["retry_after_seconds"] is not None


def _responses_for(user, email, n):
    """Drive ``n`` failures + a precheck against a fresh app wired to ``user``,
    returning every response body seen. Used to compare a registered address
    against an unregistered one."""
    session = _Session(user=user)

    async def _session():
        yield session

    app.dependency_overrides[get_session] = _session
    try:
        with TestClient(app) as c:
            bodies = [
                c.post(
                    "/auth/login/record", json={"email": email, "success": False}
                ).json()
                for _ in range(n)
            ]
            bodies.append(c.post("/auth/login/precheck", json={"email": email}).json())
    finally:
        app.dependency_overrides.clear()
    return bodies


def test_registered_and_unregistered_emails_are_indistinguishable():
    """Anti-enumeration. Below the lock threshold the two must be byte-identical:
    `locked` is never echoed, and the cooldown path applies to an address with no
    account too. (At and above LOCK_THRESHOLD the reason legitimately becomes
    `locked` for a real account — that distinction is INTERNAL, and the frontend
    collapses both reasons into one message; see login_lockout's module docstring.)
    """
    n = login_lockout.LOCK_THRESHOLD - 1
    registered = _responses_for(
        SimpleNamespace(user_id=2, email="alum@byu.edu", locked_at=None, locked_reason=None),
        "alum@byu.edu",
        n,
    )
    rate_limit.reset()
    unknown = _responses_for(None, "nobody@example.com", n)

    assert registered == unknown
    # And the shape never leaks the internal flag.
    assert all(set(b) == {"allowed", "reason", "retry_after_seconds"} for b in registered)


def test_a_throttled_request_says_nothing_about_the_email(client):
    """The limiter is a ROUTE dependency, so it resolves before the body is even
    parsed. A 429 is therefore identical whether the address is registered, not
    registered, or not a valid payload at all — it cannot become an oracle."""
    c, session = client
    for _ in range(rate_limit.LOGIN_RECORD_LIMIT):
        _record(c)

    session.user = SimpleNamespace(
        user_id=2, email="alum@byu.edu", locked_at=None, locked_reason=None
    )
    known = _record(c, email="alum@byu.edu")
    session.user = None
    unknown = _record(c, email="nobody@example.com")
    # Even a payload that would normally 422 gets the same 429.
    malformed = c.post("/auth/login/record", json={"email": "x@byu.edu", "nope": 1})

    assert known.status_code == unknown.status_code == malformed.status_code == 429
    assert known.json() == unknown.json() == malformed.json()


# --- 4. retention purges only what is past its window -------------------------
#
# Driven against a real (SQLite) database rather than a statement-shape
# assertion: "removes only rows past the window" is a claim about which ROWS
# survive, and the only way to be sure of that is to look at the rows.


class _AsyncSession:
    """Async facade over a synchronous SQLAlchemy Session.

    The purge is `async` but issues plain Core DELETEs, so a thin adapter lets it
    run against the in-process SQLite database the rest of the suite uses (there
    is no async SQLite driver in the project's dependencies).
    """

    def __init__(self, session: Session):
        self._s = session
        self.rollbacks = 0

    async def execute(self, stmt):
        return self._s.execute(stmt)

    async def commit(self):
        self._s.commit()

    async def rollback(self):
        self.rollbacks += 1
        self._s.rollback()


@pytest.fixture
def db():
    # StaticPool: every checkout is the SAME connection, so the schema created
    # here is still there for the session below.
    engine = create_engine(
        "sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False}
    )
    tables = [LoginAttempt.__table__, LoginFailure.__table__]
    LoginAttempt.metadata.create_all(engine, tables=tables)
    with Session(engine) as session:
        yield session
    engine.dispose()


def _purge(session: Session) -> _AsyncSession:
    auth_routes._reset_purge_clock()
    adapter = _AsyncSession(session)
    asyncio.run(auth_routes._purge_expired_login_records(adapter))
    return adapter


def test_purge_drops_attempts_past_the_window_and_keeps_the_rest(db):
    now = _now()
    window = datetime.timedelta(minutes=login_lockout.ATTEMPT_WINDOW_MINUTES)
    db.add_all(
        [
            # Just inside the window — an in-progress burst. MUST survive.
            LoginAttempt(
                email_lc="fresh@byu.edu",
                failed_count=9,
                first_failed_at=now - window + datetime.timedelta(minutes=5),
                last_failed_at=now - datetime.timedelta(minutes=1),
                updated_at=now,
            ),
            # Just past it. The service already treats this as a fresh start, so
            # deleting it changes storage, not behaviour.
            LoginAttempt(
                email_lc="stale@byu.edu",
                failed_count=19,
                first_failed_at=now - window * 2,
                last_failed_at=now - window - datetime.timedelta(minutes=1),
                updated_at=now - window,
            ),
            # Long past it — the abandoned-probe case that used to accumulate for
            # ever, because only a SUCCESSFUL login ever removed a row.
            LoginAttempt(
                email_lc="ancient@example.com",
                failed_count=3,
                first_failed_at=now - datetime.timedelta(days=400),
                last_failed_at=now - datetime.timedelta(days=400),
                updated_at=now - datetime.timedelta(days=400),
            ),
        ]
    )
    db.commit()

    _purge(db)

    survivors = set(db.scalars(select(LoginAttempt.email_lc)).all())
    assert survivors == {"fresh@byu.edu"}


def test_purge_never_removes_a_row_with_a_live_cooldown(db):
    """Belt-and-braces guard. COOLDOWN_MINUTES is far shorter than the window, so
    an out-of-window row cannot carry a live cooldown today — but if those
    constants are ever retuned, the purge must still not hand a throttled caller
    a clean slate."""
    now = _now()
    window = datetime.timedelta(minutes=login_lockout.ATTEMPT_WINDOW_MINUTES)
    db.add(
        LoginAttempt(
            email_lc="cooling@byu.edu",
            failed_count=10,
            first_failed_at=now - window * 3,
            last_failed_at=now - window - datetime.timedelta(minutes=1),
            cooldown_until=now + datetime.timedelta(minutes=5),
            updated_at=now,
        )
    )
    db.commit()

    _purge(db)

    assert db.scalars(select(LoginAttempt.email_lc)).all() == ["cooling@byu.edu"]


def test_purge_drops_login_failures_past_retention_only(db):
    now = _now()
    keep_edge = now - datetime.timedelta(days=auth_routes.LOGIN_FAILURE_RETENTION_DAYS - 1)
    drop_edge = now - datetime.timedelta(days=auth_routes.LOGIN_FAILURE_RETENTION_DAYS + 1)
    db.add_all(
        [
            LoginFailure(login_failure_id=1, email="a@byu.edu", occurred_at=now),
            LoginFailure(login_failure_id=2, email="b@byu.edu", occurred_at=keep_edge),
            LoginFailure(login_failure_id=3, email="c@byu.edu", occurred_at=drop_edge),
            LoginFailure(
                login_failure_id=4,
                email="d@byu.edu",
                occurred_at=now - datetime.timedelta(days=1000),
            ),
        ]
    )
    db.commit()

    _purge(db)

    assert set(db.scalars(select(LoginFailure.login_failure_id)).all()) == {1, 2}


def test_purge_runs_at_most_once_per_interval(db):
    now = _now()
    db.add(
        LoginAttempt(
            email_lc="stale@byu.edu",
            failed_count=1,
            first_failed_at=now - datetime.timedelta(days=5),
            last_failed_at=now - datetime.timedelta(days=5),
            updated_at=now - datetime.timedelta(days=5),
        )
    )
    db.commit()

    adapter = _purge(db)  # arms the clock and does the work
    assert db.scalars(select(LoginAttempt.email_lc)).all() == []

    # A second, immediate call is a no-op: it must not re-issue the DELETEs on
    # every failed login.
    db.add(
        LoginAttempt(
            email_lc="stale2@byu.edu",
            failed_count=1,
            first_failed_at=now - datetime.timedelta(days=5),
            last_failed_at=now - datetime.timedelta(days=5),
            updated_at=now - datetime.timedelta(days=5),
        )
    )
    db.commit()
    asyncio.run(auth_routes._purge_expired_login_records(adapter))
    assert db.scalars(select(LoginAttempt.email_lc)).all() == ["stale2@byu.edu"]


def test_purge_failure_is_swallowed_and_rolled_back():
    """Retention is housekeeping. A DB hiccup must not surface to the
    unauthenticated caller, because a purge that could raise would change the
    response — and any response that varies is a potential enumeration signal."""

    class _Boom:
        def __init__(self):
            self.rollbacks = 0

        async def execute(self, _stmt):
            raise RuntimeError("connection reset")

        async def commit(self):
            raise AssertionError("must not reach commit")

        async def rollback(self):
            self.rollbacks += 1

    session = _Boom()
    auth_routes._reset_purge_clock()
    asyncio.run(auth_routes._purge_expired_login_records(session))
    assert session.rollbacks == 1


def test_purge_only_touches_the_two_login_tables(client):
    """It must be impossible for retention to clear a hard lock: that lives on
    `users.locked_at`, and a purge that reached it would silently release every
    locked account on a timer."""
    c, session = client
    auth_routes._reset_purge_clock()
    _record(c)

    tables = {stmt.table.name for stmt in session.executed}
    assert tables == {"login_attempts", "login_failures"}
    assert all(stmt.is_delete for stmt in session.executed)


def test_purge_is_not_triggered_on_the_success_path(client):
    """`success:true` from this unauthenticated caller is a pure read — it must
    not commit, which was already true and stays true with the purge added."""
    c, session = client
    auth_routes._reset_purge_clock()
    resp = c.post("/auth/login/record", json={"email": "x@byu.edu", "success": True})
    assert resp.status_code == 200
    assert session.executed == []
    assert session.commits == 0
