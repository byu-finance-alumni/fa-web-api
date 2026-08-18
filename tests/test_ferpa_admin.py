"""FERPA / privacy hardening tests for the admin, audit, dashboard, and
geography surfaces.

Covered here:
  * sensitive dashboard / geography drill-downs now require full_access
    (view_only -> 403);
  * GET /admin/users paginates and writes a list_users audit row;
  * destructive admin mutations are rate-limited (429 once the per-actor budget
    is exhausted);
  * remove_role refuses to drop the last super_admin / engineer system-wide;
  * the GET /audit `user` email filter rejects <3-char values (422).

All run against stubbed sessions — no DATABASE_URL needed (CI has none).
"""

import uuid
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app.api.dependencies.auth import get_current_db_user
from app.core import rate_limit
from app.core.database import get_session
from app.main import app
from app.schemas.auth import UserContext


def _ctx(*roles: str, user_id: int = 1) -> UserContext:
    return UserContext(
        user_id=user_id,
        auth_user_id=uuid.UUID("11111111-1111-1111-1111-111111111111"),
        roles=list(roles),
    )


async def _no_db_session():
    yield None


@pytest.fixture(autouse=True)
def _clear_rate_limit():
    """Each test starts with a clean in-process rate-limit window."""
    rate_limit.reset()
    yield
    rate_limit.reset()


@pytest.fixture
def client():
    app.dependency_overrides[get_session] = _no_db_session
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


def _with_session(session):
    async def _override():
        yield session

    return _override


class _Result:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return self._rows


class _Scalars:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return self._rows


class _FakeSession:
    """Records added objects + commits; serves queued execute/scalar/scalars."""

    def __init__(self, *, scalars=(), executes=(), scalars_results=()):
        self._scalars = list(scalars)
        self._executes = list(executes)
        self._scalars_results = list(scalars_results)
        self.added: list = []
        self.commits = 0

    async def execute(self, _stmt):
        return _Result(self._executes.pop(0) if self._executes else [])

    async def scalar(self, _stmt):
        return self._scalars.pop(0) if self._scalars else 0

    async def scalars(self, _stmt):
        return _Scalars(self._scalars_results.pop(0) if self._scalars_results else [])

    def add(self, obj):
        self.added.append(obj)

    async def delete(self, _obj):
        pass

    async def commit(self):
        self.commits += 1

    async def rollback(self):
        pass


# --- #1 drill-downs now require full_access -----------------------------------


@pytest.mark.parametrize(
    "path",
    [
        "/dashboard/contacted-this-month",
        "/dashboard/follow-ups",
        "/dashboard/activity",
        "/geography/states/UT/alumni",
        "/geography/cities?state=UT&city=Provo",
    ],
)
def test_sensitive_drilldowns_forbidden_for_view_only(client, path):
    app.dependency_overrides[get_current_db_user] = lambda: _ctx("view_only")
    app.dependency_overrides[get_session] = _with_session(_FakeSession())
    response = client.get(path)
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "forbidden"


def test_birthdays_drops_birth_year_for_view_only(client):
    import datetime

    alum = SimpleNamespace(
        alumni_id=7,
        first_name="Jane",
        last_name="Doe",
        graduation_year=2019,
        birth_date=datetime.date(1997, 6, 3),
    )
    session = _FakeSession(executes=[[(alum, "Goldman Sachs")]])
    app.dependency_overrides[get_current_db_user] = lambda: _ctx("view_only")
    app.dependency_overrides[get_session] = _with_session(session)

    response = client.get("/dashboard/birthdays")
    assert response.status_code == 200
    row = response.json()[0]
    # Recurring month+day only — the full DOB / birth year must NOT be present.
    assert row["birth_month"] == 6
    assert row["birth_day"] == 3
    assert "birth_date" not in row


# --- #3 list_users paginates + audits -----------------------------------------


def test_list_users_paginates_and_audits(client):
    users = [
        SimpleNamespace(
            user_id=2,
            email="a@byu.edu",
            first_name="A",
            last_name="A",
            active=True,
            locked_at=None,
            created_at=None,
            roles=[],
        )
    ]
    # scalar -> total count; scalars -> the page of users.
    session = _FakeSession(scalars=[1], scalars_results=[users])
    app.dependency_overrides[get_current_db_user] = lambda: _ctx(
        "super_admin", user_id=1
    )
    app.dependency_overrides[get_session] = _with_session(session)

    response = client.get("/admin/users?limit=10&offset=0")
    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 1
    assert body["limit"] == 10
    assert body["offset"] == 0
    assert len(body["items"]) == 1
    # A list_users audit row was written for the access.
    assert session.commits == 1
    audit = next(a for a in session.added if type(a).__name__ == "AuditLog")
    assert audit.action_type == "list_users"
    assert audit.entity_type == "user"
    assert audit.user_id == 1


def test_list_users_caps_limit_at_200(client):
    app.dependency_overrides[get_current_db_user] = lambda: _ctx("super_admin")
    app.dependency_overrides[get_session] = _with_session(_FakeSession())
    # ge/le bounds: 201 is rejected (422) by FastAPI's Query validation.
    response = client.get("/admin/users?limit=201")
    assert response.status_code == 422


# --- #4 rate-limit destructive admin mutations --------------------------------


def test_reset_password_rate_limited_returns_429(client):
    # Drive the limiter directly: after 5 hits in the window, the 6th -> 429.
    app.dependency_overrides[get_current_db_user] = lambda: _ctx(
        "super_admin", user_id=1
    )
    # Each call needs a fresh single-user session (reset_password loads the user
    # then sets a password via the Supabase Admin API — which we never reach for
    # the blocked call, but the allowed calls would; so we assert purely on the
    # limiter by exhausting the budget through repeated requests and checking the
    # final one is 429 regardless of downstream).
    seen = []
    for _ in range(6):
        # _load_user returns None -> a clean 404 before any Supabase call, so the
        # allowed requests never hit the network; we only care that the 6th is
        # blocked by the limiter (429) regardless of that downstream 404.
        app.dependency_overrides[get_session] = _with_session(
            _FakeSession(scalars=[None])
        )
        resp = client.post("/admin/users/2/reset-password")
        seen.append(resp.status_code)
    # The first five are allowed (404 — user not found in the stub); the sixth is
    # rate-limited.
    assert seen[:5] == [404] * 5
    assert seen[-1] == 429


def test_delete_user_rate_limited_returns_429(client):
    # #425: permanent deletion carries the same 5-per-10-minutes budget as
    # reset-password (it was 20). Drive the limiter directly: the 6th call in
    # the window is refused before the endpoint does anything.
    app.dependency_overrides[get_current_db_user] = lambda: _ctx(
        "super_admin", user_id=1
    )
    seen = []
    for _ in range(6):
        # _load_user returns None -> a clean 404 before any Supabase call, so
        # the allowed requests never delete anything; we only care that the 6th
        # is blocked by the limiter (429) regardless of that downstream 404.
        app.dependency_overrides[get_session] = _with_session(
            _FakeSession(scalars=[None])
        )
        seen.append(client.delete("/admin/users/2").status_code)
    assert seen[:5] == [404] * 5
    assert seen[-1] == 429


def test_rate_limit_is_per_actor(client):
    # Actor 1 exhausts their budget; actor 2 is unaffected (independent window).
    rate_limit.reset()

    def _post():
        app.dependency_overrides[get_session] = _with_session(
            _FakeSession(scalars=[None])
        )
        return client.post("/admin/users/2/reset-password").status_code

    app.dependency_overrides[get_current_db_user] = lambda: _ctx(
        "super_admin", user_id=1
    )
    statuses = [_post() for _ in range(6)]
    assert statuses[-1] == 429

    # A different actor still has a full budget.
    app.dependency_overrides[get_current_db_user] = lambda: _ctx(
        "super_admin", user_id=999
    )
    assert _post() != 429


# --- #7 last super_admin / engineer guard in remove_role ----------------------


def test_remove_last_super_admin_blocked(client):
    target = SimpleNamespace(
        user_id=2,
        email="target@byu.edu",
        first_name="T",
        last_name="T",
        active=True,
        locked_at=None,
        created_at=None,
        roles=[SimpleNamespace(role_name="super_admin")],
    )
    role = SimpleNamespace(role_id=2, role_name="super_admin")
    link = SimpleNamespace(user_id=2, role_id=2)
    # remove_role scalars in order: _load_user -> Role lookup -> UserRole link ->
    # holders COUNT (==1 -> last holder -> blocked).
    session = _FakeSession(scalars=[target, role, link, 1])
    # Actor is a DIFFERENT super_admin (so the own-role guard does NOT fire and we
    # reach the system-wide last-holder COUNT guard).
    app.dependency_overrides[get_current_db_user] = lambda: _ctx(
        "super_admin", user_id=1
    )
    app.dependency_overrides[get_session] = _with_session(session)

    response = client.delete("/admin/users/2/roles/super_admin")
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "conflict"
    assert "last super_admin" in response.json()["error"]["message"]
    assert session.commits == 0


def test_remove_super_admin_allowed_when_other_holders_exist(client):
    target = SimpleNamespace(
        user_id=2,
        email="target@byu.edu",
        first_name="T",
        last_name="T",
        active=True,
        locked_at=None,
        created_at=None,
        roles=[],
    )
    role = SimpleNamespace(role_id=2, role_name="super_admin")
    link = SimpleNamespace(user_id=2, role_id=2)
    # holders COUNT == 2 -> not the last holder -> removal proceeds. The trailing
    # scalar is the _load_user re-serialize after delete.
    session = _FakeSession(scalars=[target, role, link, 2, target])
    app.dependency_overrides[get_current_db_user] = lambda: _ctx(
        "super_admin", user_id=1
    )
    app.dependency_overrides[get_session] = _with_session(session)

    response = client.delete("/admin/users/2/roles/super_admin")
    assert response.status_code == 200
    assert session.commits == 1


# --- #6 audit `user` filter must be >= 3 chars --------------------------------


@pytest.mark.parametrize("value", ["a", "ab"])
def test_audit_user_filter_too_short_is_422(client, value):
    app.dependency_overrides[get_current_db_user] = lambda: _ctx("super_admin")
    app.dependency_overrides[get_session] = _with_session(_FakeSession())
    response = client.get(f"/audit?user={value}")
    assert response.status_code == 422


def test_audit_read_is_audited(client):
    # scalar -> total count; execute -> rows page (empty). The read writes a
    # read_audit_log row recording the actor + applied filters (never values).
    session = _FakeSession(scalars=[0], executes=[[]])
    app.dependency_overrides[get_current_db_user] = lambda: _ctx(
        "super_admin", user_id=5
    )
    app.dependency_overrides[get_session] = _with_session(session)

    response = client.get("/audit?action_type=update")
    assert response.status_code == 200
    audit = next(a for a in session.added if type(a).__name__ == "AuditLog")
    assert audit.action_type == "read_audit_log"
    assert audit.entity_type == "audit_log"
    assert audit.user_id == 5
    # The applied filters are recorded; the returned values are NOT.
    assert "action_type=update" in (audit.field_name or "")
