"""Authorization-gating tests for the alumni routes (no database).

The DB-user dependency is overridden so we can assert role gating and request
validation without a live database — these paths reject before any query runs.

The hygiene/preview tests at the bottom drive the routes through a tiny fake
session (queued scalar/execute results) so duplicate-blocking and the preview
shape are covered end to end without a real DATABASE_URL.
"""

import datetime
import uuid
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app.api.dependencies.auth import get_current_db_user
from app.core.database import get_session
from app.main import app
from app.schemas.auth import UserContext


def _ctx(*roles: str) -> UserContext:
    return UserContext(
        user_id=1,
        auth_user_id=uuid.UUID("11111111-1111-1111-1111-111111111111"),
        roles=list(roles),
    )


async def _no_db_session():
    """Stand-in for get_session so these auth/validation tests don't require a
    real DATABASE_URL (CI has none). No test here reaches a real query."""
    yield None


@pytest.fixture
def client():
    app.dependency_overrides[get_session] = _no_db_session
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


def test_create_requires_auth(client):
    response = client.post("/alumni", json={"last_name": "Smith"})
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "unauthorized"


def test_create_forbidden_for_view_only(client):
    app.dependency_overrides[get_current_db_user] = lambda: _ctx("view_only")
    response = client.post("/alumni", json={"last_name": "Smith"})
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "forbidden"


def test_patch_forbidden_for_view_only(client):
    app.dependency_overrides[get_current_db_user] = lambda: _ctx("view_only")
    response = client.patch("/alumni/1", json={"last_name": "Smith"})
    assert response.status_code == 403


def test_delete_forbidden_for_view_only(client):
    app.dependency_overrides[get_current_db_user] = lambda: _ctx("view_only")
    response = client.delete("/alumni/1")
    assert response.status_code == 403


def test_restore_forbidden_for_view_only(client):
    app.dependency_overrides[get_current_db_user] = lambda: _ctx("view_only")
    response = client.post("/alumni/1/restore")
    assert response.status_code == 403


def test_create_rejects_empty_identifier(client):
    # full_access passes the guard; the body fails the "at least one identifier"
    # rule, so this is a 422 (validation), not a 403.
    app.dependency_overrides[get_current_db_user] = lambda: _ctx("full_access")
    response = client.post("/alumni", json={"gender": "F"})
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"


def test_create_rejects_unknown_field(client):
    app.dependency_overrides[get_current_db_user] = lambda: _ctx("full_access")
    response = client.post(
        "/alumni", json={"last_name": "Smith", "not_a_field": "x"}
    )
    assert response.status_code == 422


# --- #160 "needs surveying" view: admin-tier role gating ---------------------
#
# The filter rides on the shared GET /alumni list (all roles can read the list),
# so it is gated INSIDE the handler: admin tier (engineer / super_admin /
# full_access) may use it; student and view_only ("professor") get a 403 that
# fires before any query. The allowed path is exercised by capturing the service
# kwargs so we can confirm both that it was NOT denied and that the 2-year cutoff
# is computed server-side and forwarded.


@pytest.mark.parametrize("role", ["student", "view_only"])
def test_needs_survey_forbidden_for_non_admin(client, role):
    app.dependency_overrides[get_current_db_user] = lambda: _ctx(role)
    response = client.get("/alumni", params={"needs_survey": "true"})
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "forbidden"


@pytest.mark.parametrize("role", ["engineer", "super_admin", "full_access"])
def test_needs_survey_allowed_for_admin_tier(client, monkeypatch, role):
    from app.api.routes import alumni as alumni_routes

    captured: dict = {}

    async def fake_list_alumni(session, **kwargs):
        captured.update(kwargs)
        return [], 0

    async def fake_log_search(session, **kwargs):
        return None

    monkeypatch.setattr(alumni_routes.service, "list_alumni", fake_list_alumni)
    monkeypatch.setattr(alumni_routes.service, "log_search", fake_log_search)
    app.dependency_overrides[get_current_db_user] = lambda: _ctx(role)

    response = client.get("/alumni", params={"needs_survey": "true"})
    assert response.status_code == 200
    # Not denied, AND the server computed + forwarded a 2-year staleness cutoff.
    assert captured["needs_survey"] is True
    cutoff = captured["survey_due_before"]
    assert isinstance(cutoff, datetime.datetime)
    expected = datetime.datetime.now(datetime.UTC) - datetime.timedelta(days=365 * 2)
    assert abs((cutoff - expected).total_seconds()) < 60


def test_needs_survey_omitted_does_not_forward_threshold(client, monkeypatch):
    from app.api.routes import alumni as alumni_routes

    captured: dict = {}

    async def fake_list_alumni(session, **kwargs):
        captured.update(kwargs)
        return [], 0

    async def fake_log_search(session, **kwargs):
        return None

    monkeypatch.setattr(alumni_routes.service, "list_alumni", fake_list_alumni)
    monkeypatch.setattr(alumni_routes.service, "log_search", fake_log_search)
    app.dependency_overrides[get_current_db_user] = lambda: _ctx("view_only")

    # No needs_survey param: a plain list read is allowed for view_only and the
    # threshold is never computed (stays None).
    response = client.get("/alumni")
    assert response.status_code == 200
    assert captured["needs_survey"] is False
    assert captured["survey_due_before"] is None


# --- Hygiene / preview / duplicate-blocking (fake session) -------------------


class _Scalars:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return list(self._rows)


class _ExecResult:
    def __init__(self, rows):
        self._rows = rows

    def scalars(self):
        return _Scalars(self._rows)


class _FakeSession:
    """Returns queued scalar/execute results. ``flush`` assigns an id and
    ``refresh`` fills the columns AlumniRead requires (a real refresh would load
    these from DB defaults)."""

    def __init__(self, scalars=(), execute_rows=(), get_result=None):
        self._scalars = list(scalars)
        self._execute_rows = list(execute_rows)
        self._get_result = get_result
        self.added = []

    def add(self, obj):
        self.added.append(obj)

    async def get(self, model, pk):
        # Used by repo.get (service.get_alumni). Returns the configured record.
        return self._get_result

    async def scalar(self, stmt):
        return self._scalars.pop(0) if self._scalars else None

    async def execute(self, stmt):
        rows = self._execute_rows.pop(0) if self._execute_rows else []
        return _ExecResult(rows)

    async def flush(self):
        for obj in self.added:
            if getattr(obj, "alumni_id", None) is None:
                obj.alumni_id = 100

    async def commit(self):
        pass

    async def refresh(self, obj):
        now = datetime.datetime(2026, 6, 12, tzinfo=datetime.UTC)
        for attr, default in (
            ("alumni_id", 100),
            ("deceased", False),
            ("is_alumni", True),
            ("archived", False),
            ("created_at", now),
            ("updated_at", now),
        ):
            if getattr(obj, attr, None) is None:
                setattr(obj, attr, default)


def _alum(**kw):
    base = dict(
        alumni_id=1,
        first_name="Jane",
        last_name="Doe",
        graduation_year=2018,
        byu_id=None,
        net_id=None,
        archived=False,
        is_alumni=True,
    )
    base.update(kw)
    return SimpleNamespace(**base)


def _with_session(session):
    async def _override():
        yield session

    return _override


def _full_access_client(session):
    app.dependency_overrides[get_session] = _with_session(session)
    app.dependency_overrides[get_current_db_user] = lambda: _ctx("full_access")
    return TestClient(app, raise_server_exceptions=False)


def test_preview_create_returns_changes_blockers_warnings():
    # Dirty + duplicate byu_id payload: byu_id lookup hits an existing alum.
    session = _FakeSession(scalars=[_alum(byu_id="123456789")])
    with _full_access_client(session) as c:
        resp = c.post(
            "/alumni/preview",
            json={
                "byu_id": "123456789",
                "first_name": "JANE",
                "last_name": "doe",
                "contact": {"personal_email": "JANE@X.COM", "state": "Utah"},
            },
        )
    app.dependency_overrides.clear()
    assert resp.status_code == 200
    body = resp.json()
    assert set(body.keys()) == {"cleaned", "changes", "warnings", "blockers"}
    # Cleaning normalized name + email + state.
    assert body["cleaned"]["first_name"] == "Jane"
    assert body["cleaned"]["contact"]["state"] == "UT"
    changed = {(c["section"], c["field"]) for c in body["changes"]}
    assert ("core", "first_name") in changed
    assert ("contact", "state") in changed
    # Exact duplicate -> one blocker, surfaced with code + alumni_id.
    assert len(body["blockers"]) == 1
    assert body["blockers"][0]["code"] == "duplicate_byu_id"
    # Recommended warnings present (no employer, no grad year).
    warn_codes = {w["code"] for w in body["warnings"]}
    assert "missing_employer" in warn_codes


def test_exact_duplicate_blocks_create_with_409():
    session = _FakeSession(scalars=[_alum(byu_id="123456789")])
    with _full_access_client(session) as c:
        resp = c.post(
            "/alumni", json={"byu_id": "123456789", "last_name": "Doe"}
        )
    app.dependency_overrides.clear()
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "conflict"


def test_fuzzy_duplicate_only_warns_create_succeeds():
    # No exact id; fuzzy execute returns a same-name same-year match. Create
    # must still succeed (201). scalars: byu (n/a, none provided) -> only the
    # fuzzy execute runs during detect; then create has no further scalars.
    session = _FakeSession(
        scalars=[],
        execute_rows=[[_alum(alumni_id=2)]],
    )
    with _full_access_client(session) as c:
        # Preview first: should warn, not block.
        preview = c.post(
            "/alumni/preview",
            json={
                "first_name": "Jane",
                "last_name": "Doe",
                "graduation_year": 2018,
            },
        )
        assert preview.status_code == 200
        assert preview.json()["blockers"] == []
        assert any(
            w["code"] == "possible_duplicate"
            for w in preview.json()["warnings"]
        )

        # Real create: fuzzy match again, but it does not block.
        session._execute_rows = [[_alum(alumni_id=2)]]
        created = c.post(
            "/alumni",
            json={
                "first_name": "Jane",
                "last_name": "Doe",
                "graduation_year": 2018,
            },
        )
    app.dependency_overrides.clear()
    assert created.status_code == 201
    assert created.json()["first_name"] == "Jane"


def test_create_persists_and_returns_secondary_affiliation():
    # #47: POST /alumni with the new secondary-affiliation fields persists them
    # and the AlumniRead response echoes them back. No exact-dup scalars needed
    # (no byu_id / net_id provided).
    session = _FakeSession(scalars=[])
    with _full_access_client(session) as c:
        resp = c.post(
            "/alumni",
            json={
                "last_name": "Doe",
                "mba_program": "BYU Marriott MBA",
                "law_school": "Harvard Law",
                "medical_school": "Johns Hopkins",
                "graduate_school": "MIT",
                "startup_involvement": "Co-founded Acme",
                "advisory_roles": "Board advisor at Foo Inc.",
                "secondary_employment": "Adjunct professor",
            },
        )
    app.dependency_overrides.clear()
    assert resp.status_code == 201
    body = resp.json()
    # The written ORM row carries the values...
    written = session.added[0]
    assert written.mba_program == "BYU Marriott MBA"
    assert written.secondary_employment == "Adjunct professor"
    # ...and the AlumniRead response surfaces every new field (same shape the
    # GET /{id}/profile read returns, which serializes via AlumniRead too).
    assert body["mba_program"] == "BYU Marriott MBA"
    assert body["law_school"] == "Harvard Law"
    assert body["medical_school"] == "Johns Hopkins"
    assert body["graduate_school"] == "MIT"
    assert body["startup_involvement"] == "Co-founded Acme"
    assert body["advisory_roles"] == "Board advisor at Foo Inc."
    assert body["secondary_employment"] == "Adjunct professor"


def test_update_preview_excludes_self_from_dup_detection():
    # Updating alum 5's byu_id to a value: the only DB row with that id is alum
    # 5 itself, which the query excludes -> no blocker. get_alumni returns the
    # record; detect byu_id scalar returns None (self excluded); then effective
    # loads contact + career rows.
    existing = _alum(alumni_id=5, graduation_year=2018)
    contact_row = SimpleNamespace(personal_email="jane@x.com", work_email=None)
    career_row = SimpleNamespace(current_employer="Goldman")
    # get() -> existing; scalars: active byu_id dup lookup (None, self
    # excluded), archived byu_id ghost lookup (None), then effective loads
    # contact then career.
    session = _FakeSession(
        scalars=[None, None, contact_row, career_row], get_result=existing
    )
    with _full_access_client(session) as c:
        resp = c.post(
            "/alumni/5/preview", json={"byu_id": "123456789"}
        )
    app.dependency_overrides.clear()
    assert resp.status_code == 200
    assert resp.json()["blockers"] == []
    assert resp.json()["cleaned"]["byu_id"] == "123456789"


# --- L3: boolean query params accept standard truthy values -------------------


@pytest.mark.parametrize("truthy", ["1", "true", "True", "TRUE", "yes", "on"])
def test_list_boolean_filter_accepts_truthy_values(monkeypatch, truthy):
    """``?missing_email=true`` (and other truthy spellings) must filter exactly
    like ``=1`` — the API coerces standard boolean strings, not just ``1``."""
    captured: dict = {}

    async def _fake_list(session, *, limit, offset, **filters):
        captured.update(filters)
        return [], 0

    async def _fake_log(session, *, actor_user_id, filters):
        return None

    from app.api.routes import alumni as alumni_routes

    monkeypatch.setattr(alumni_routes.service, "list_alumni", _fake_list)
    monkeypatch.setattr(alumni_routes.service, "log_search", _fake_log)

    app.dependency_overrides[get_current_db_user] = lambda: _ctx("full_access")
    app.dependency_overrides[get_session] = _no_db_session
    try:
        resp = client_get_list(truthy)
    finally:
        app.dependency_overrides.clear()
    assert resp.status_code == 200
    # Every truthy spelling resolves to the same True the repo would get from "1".
    assert captured["missing_email"] is True
    assert captured["cfa"] is True


def client_get_list(value: str):
    with TestClient(app) as c:
        return c.get(f"/alumni?missing_email={value}&cfa={value}")


# --- #185: numeric path ids are bounded (out-of-range -> 422, never 500) ------


@pytest.mark.parametrize(
    "bad_id",
    [
        "99999999999999999999",  # beyond int64: would 500 at the asyncpg bind
        "0",  # below the 1-based identity floor (ge=1)
    ],
)
def test_out_of_range_alumni_id_returns_422(client, bad_id):
    # #185: a numeric path id outside the bigint range must be rejected as a 422
    # validation error BEFORE the query runs — it must never reach asyncpg and
    # surface as a 500.
    app.dependency_overrides[get_current_db_user] = lambda: _ctx("view_only")
    response = client.get(f"/alumni/{bad_id}")
    assert response.status_code == 422
    body = response.json()
    assert body["error"]["code"] == "validation_error"
    assert any(f["field"] == "alumni_id" for f in body["error"]["fields"])
