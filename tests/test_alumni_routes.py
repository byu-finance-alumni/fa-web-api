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
from app.api.routes import alumni as alumni_routes
from app.core import rate_limit
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


# --- #584 / #362 new list filters --------------------------------------------
#
# The route is the only place these params are declared, forwarded AND echoed in
# the search-audit summary; the summary echo is what lets a saved/shared search
# round-trip, so it is asserted alongside the forwarding.


def test_new_filters_are_forwarded_and_echoed(client, monkeypatch):
    from app.api.routes import alumni as alumni_routes

    captured: dict = {}
    logged: dict = {}

    async def fake_list_alumni(session, **kwargs):
        captured.update(kwargs)
        return [], 0

    async def fake_log_search(session, **kwargs):
        logged.update(kwargs)
        return None

    monkeypatch.setattr(alumni_routes.service, "list_alumni", fake_list_alumni)
    monkeypatch.setattr(alumni_routes.service, "log_search", fake_log_search)
    app.dependency_overrides[get_current_db_user] = lambda: _ctx("full_access")

    response = client.get(
        "/alumni",
        params={
            "cfp": "true",
            "industry": "Consulting",
            "secondary_industry": "Real Estate",
            "employment_status": ["Full-time", "Graduate Student"],
        },
    )
    assert response.status_code == 200
    assert captured["cfp"] is True
    assert captured["industry"] == ["Consulting"]
    assert captured["secondary_industry"] == ["Real Estate"]
    assert captured["employment_status"] == ["Full-time", "Graduate Student"]

    filters = logged["filters"]
    assert filters["cfp"] is True
    assert filters["industry"] == "Consulting"
    assert filters["secondary_industry"] == "Real Estate"
    assert filters["employment_status"] == "Full-time|Graduate Student"


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
                "contact": {"personal_email": "JANE@X.COM", "state": "ut"},
            },
        )
    app.dependency_overrides.clear()
    assert resp.status_code == 200
    body = resp.json()
    assert set(body.keys()) == {"cleaned", "changes", "warnings", "blockers"}
    # Cleaning normalized name + email + state (code -> canonical full name).
    assert body["cleaned"]["first_name"] == "Jane"
    assert body["cleaned"]["contact"]["state"] == "Utah"
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


def test_create_persists_preferred_contact_method():
    # #301: POST /alumni with a contact section that flags preferred_contact_method
    # writes it onto the AlumniContactInfo row (via **cleaned.get("contact")).
    session = _FakeSession(scalars=[])
    with _full_access_client(session) as c:
        resp = c.post(
            "/alumni",
            json={
                "last_name": "Doe",
                "contact": {
                    "personal_email": "jane@x.com",
                    "preferred_contact_method": "phone",
                },
            },
        )
    app.dependency_overrides.clear()
    assert resp.status_code == 201
    # The contact row is added alongside the Alumni row; find it by attribute.
    contact_written = next(
        row
        for row in session.added
        if hasattr(row, "preferred_contact_method")
    )
    assert contact_written.preferred_contact_method == "phone"
    assert contact_written.personal_email == "jane@x.com"


def test_update_preview_excludes_self_from_dup_detection():
    # Updating alum 5's byu_id to a value: the only DB row with that id is alum
    # 5 itself, which the query excludes -> no blocker. get_alumni returns the
    # record; effective loads contact + career rows; detect byu_id scalar returns
    # None (self excluded).
    existing = _alum(alumni_id=5, graduation_year=2018)
    contact_row = SimpleNamespace(personal_email="jane@x.com", work_email=None)
    career_row = SimpleNamespace(current_employer="Goldman")
    # get() -> existing; scalars, in call order: effective loads contact then
    # career, THEN the active byu_id dup lookup (None, self excluded) and the
    # archived byu_id ghost lookup (None). The effective record is built FIRST
    # because duplicate detection now runs against it rather than against the
    # partial payload (#627) — a rename that omits the graduation year has to be
    # measured against the year the record actually holds.
    session = _FakeSession(
        scalars=[contact_row, career_row, None, None], get_result=existing
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


# --- Headshots ---------------------------------------------------------------


def test_headshot_get_requires_auth(client):
    assert client.get("/alumni/1/headshot").status_code == 401


def test_headshot_upload_forbidden_for_view_only(client):
    app.dependency_overrides[get_current_db_user] = lambda: _ctx("view_only")
    resp = client.put(
        "/alumni/1/headshot",
        files={"file": ("h.png", b"\x89PNG", "image/png")},
    )
    assert resp.status_code == 403


def test_headshot_upload_rejects_non_image(client):
    app.dependency_overrides[get_current_db_user] = lambda: _ctx("full_access")
    resp = client.put(
        "/alumni/1/headshot",
        files={"file": ("notes.txt", b"hello", "text/plain")},
    )
    # InvalidRequestError -> 422 (the app's validation-error status).
    assert resp.status_code == 422


def test_headshot_upload_url_requires_auth(client):
    assert client.post("/alumni/1/headshot/upload-url").status_code == 401


def test_headshot_upload_url_forbidden_for_view_only(client):
    app.dependency_overrides[get_current_db_user] = lambda: _ctx("view_only")
    assert client.post("/alumni/1/headshot/upload-url").status_code == 403


def test_headshot_confirm_forbidden_for_view_only(client):
    app.dependency_overrides[get_current_db_user] = lambda: _ctx("view_only")
    assert client.post("/alumni/1/headshot/confirm").status_code == 403


# Minimal valid magic-byte payloads for the accepted image types, used by the
# bulk-import tests below (an object's real leading bytes are what the confirm
# step sniffs).
_JPEG_BYTES = b"\xff\xd8\xff" + b"\x00" * 64
_PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64


# --- #404 designation list filter (route validation + passthrough) -----------


def test_designations_unknown_value_is_422(client):
    app.dependency_overrides[get_current_db_user] = lambda: _ctx("full_access")
    resp = client.get("/alumni", params={"designations": "MBA"})
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "validation_error"


def test_designations_valid_values_forwarded(monkeypatch):
    # Repeatable AND comma-separated tokens are accepted case-insensitively,
    # normalized to canonical upper-case, de-duped, and forwarded to the repo.
    captured: dict = {}

    async def _fake_list(session, *, limit, offset, **filters):
        captured.update(filters)
        return [], 0

    async def _fake_log(session, **kwargs):
        return None

    from app.api.routes import alumni as alumni_routes

    monkeypatch.setattr(alumni_routes.service, "list_alumni", _fake_list)
    monkeypatch.setattr(alumni_routes.service, "log_search", _fake_log)
    app.dependency_overrides[get_current_db_user] = lambda: _ctx("full_access")
    app.dependency_overrides[get_session] = _no_db_session
    try:
        with TestClient(app) as c:
            resp = c.get("/alumni?designations=cfp&designations=CFA,cpa")
    finally:
        app.dependency_overrides.clear()
    assert resp.status_code == 200
    assert captured["designations"] == ["CFP", "CFA", "CPA"]


# --- #401 bulk headshot import -----------------------------------------------


class _Scalars2:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return list(self._rows)


class _BulkHeadshotSession:
    """Returns the seeded alumni rows for the net_id lookup; records audit adds."""

    def __init__(self, alumni_rows=()):
        self._rows = list(alumni_rows)
        self.added: list = []
        self.committed = False

    async def scalars(self, stmt):
        return _Scalars2(self._rows)

    def add(self, obj):
        self.added.append(obj)

    async def commit(self):
        self.committed = True


def _alumnus(net_id, alumni_id):
    return SimpleNamespace(net_id=net_id, alumni_id=alumni_id)


def test_bulk_upload_urls_requires_auth(client):
    resp = client.post(
        "/alumni/headshots/bulk/upload-urls", json={"filenames": ["jdoe12.png"]}
    )
    assert resp.status_code == 401


def test_bulk_upload_urls_forbidden_for_view_only(client):
    app.dependency_overrides[get_current_db_user] = lambda: _ctx("view_only")
    resp = client.post(
        "/alumni/headshots/bulk/upload-urls", json={"filenames": ["jdoe12.png"]}
    )
    assert resp.status_code == 403


def test_bulk_confirm_requires_auth(client):
    resp = client.post(
        "/alumni/headshots/bulk/confirm",
        json={"files": [{"filename": "jdoe12.png", "uploaded": True}]},
    )
    assert resp.status_code == 401


def test_bulk_confirm_forbidden_for_view_only(client):
    app.dependency_overrides[get_current_db_user] = lambda: _ctx("view_only")
    resp = client.post(
        "/alumni/headshots/bulk/confirm",
        json={"files": [{"filename": "jdoe12.png", "uploaded": True}]},
    )
    assert resp.status_code == 403


def _mint_stub(calls):
    async def _create(bucket, path):
        calls.append((bucket, path))
        return f"https://storage.test/upload/{bucket}/{path}?token=abc"

    return _create


def test_bulk_upload_urls_mints_only_for_matched_net_ids(monkeypatch):
    """A URL is minted ONLY for a file whose net ID resolves to an alumnus. An
    unmatched net ID and a non-image name are reported back with no URL, so the
    browser is never handed anywhere to put those bytes."""
    calls: list = []
    monkeypatch.setattr(
        alumni_routes.supabase_storage, "create_signed_upload_url", _mint_stub(calls)
    )
    session = _BulkHeadshotSession([_alumnus("jdoe12", 5)])
    app.dependency_overrides[get_session] = _with_session(session)
    app.dependency_overrides[get_current_db_user] = lambda: _ctx("full_access")
    try:
        with TestClient(app) as c:
            resp = c.post(
                "/alumni/headshots/bulk/upload-urls",
                json={"filenames": ["jdoe12.png", "nobody99.png", "notes.txt"]},
            )
    finally:
        app.dependency_overrides.clear()
    assert resp.status_code == 200
    by_file = {t["filename"]: t for t in resp.json()["targets"]}
    assert by_file["jdoe12.png"]["status"] == "ready"
    assert by_file["jdoe12.png"]["upload_url"].startswith("https://storage.test/")
    assert by_file["nobody99.png"]["status"] == "no_match"
    assert by_file["nobody99.png"]["upload_url"] is None
    assert by_file["notes.txt"]["status"] == "invalid"
    assert by_file["notes.txt"]["upload_url"] is None
    # Exactly one mint, scoped to the headshots bucket + the STORED net ID.
    assert calls == [("headshots", "jdoe12")]
    # Minting is the attributable precondition for an image change, so it is
    # audited even if the browser never reaches confirm.
    audits = [a for a in session.added if getattr(a, "action_type", None)]
    assert [a.action_type for a in audits] == ["upload_headshot_started"]
    assert audits[0].entity_id == 5
    assert session.committed is True


def test_bulk_upload_urls_case_insensitive_net_id_match(monkeypatch):
    calls: list = []
    monkeypatch.setattr(
        alumni_routes.supabase_storage, "create_signed_upload_url", _mint_stub(calls)
    )
    session = _BulkHeadshotSession([_alumnus("jdoe12", 5)])
    app.dependency_overrides[get_session] = _with_session(session)
    app.dependency_overrides[get_current_db_user] = lambda: _ctx("full_access")
    try:
        with TestClient(app) as c:
            resp = c.post(
                "/alumni/headshots/bulk/upload-urls",
                json={"filenames": ["JDOE12.JPG"]},
            )
    finally:
        app.dependency_overrides.clear()
    assert resp.json()["targets"][0]["status"] == "ready"
    # Keyed by the alumnus's STORED net_id, not the file name's casing.
    assert calls == [("headshots", "jdoe12")]


def test_bulk_upload_urls_key_comes_from_the_db_not_the_filename(monkeypatch):
    """A crafted path in the file name cannot steer the upload: the object key is
    always the matched alumnus's stored net ID."""
    calls: list = []
    monkeypatch.setattr(
        alumni_routes.supabase_storage, "create_signed_upload_url", _mint_stub(calls)
    )
    session = _BulkHeadshotSession([_alumnus("jdoe12", 5)])
    app.dependency_overrides[get_session] = _with_session(session)
    app.dependency_overrides[get_current_db_user] = lambda: _ctx("full_access")
    try:
        with TestClient(app) as c:
            resp = c.post(
                "/alumni/headshots/bulk/upload-urls",
                json={"filenames": ["../../secrets/jdoe12.png"]},
            )
    finally:
        app.dependency_overrides.clear()
    assert resp.json()["targets"][0]["status"] == "ready"
    assert calls == [("headshots", "jdoe12")]


def test_bulk_upload_urls_over_per_request_cap_is_413(client):
    app.dependency_overrides[get_current_db_user] = lambda: _ctx("full_access")
    names = [f"user{i}.png" for i in range(alumni_routes._HEADSHOT_BULK_MAX_PER_REQUEST + 1)]
    resp = client.post("/alumni/headshots/bulk/upload-urls", json={"filenames": names})
    assert resp.status_code == 413
    assert resp.json()["error"]["code"] == "payload_too_large"


def test_bulk_upload_urls_storage_failure_reported_per_file(monkeypatch):
    """One unavailable mint fails that file only — never the whole batch."""
    from app.core.errors import ServiceError

    async def _boom(bucket, path):
        raise ServiceError("nope")

    monkeypatch.setattr(
        alumni_routes.supabase_storage, "create_signed_upload_url", _boom
    )
    session = _BulkHeadshotSession([_alumnus("jdoe12", 5)])
    app.dependency_overrides[get_session] = _with_session(session)
    app.dependency_overrides[get_current_db_user] = lambda: _ctx("full_access")
    try:
        with TestClient(app) as c:
            resp = c.post(
                "/alumni/headshots/bulk/upload-urls",
                json={"filenames": ["jdoe12.png"]},
            )
    finally:
        app.dependency_overrides.clear()
    assert resp.status_code == 200
    target = resp.json()["targets"][0]
    assert target["status"] == "error"
    assert target["upload_url"] is None
    assert session.committed is False


def _probe_stub(result, calls=None):
    async def _probe(bucket, path, *, head_bytes=16):
        if calls is not None:
            calls.append(path)
        return result

    return _probe


def _confirm_client(session):
    app.dependency_overrides[get_session] = _with_session(session)
    app.dependency_overrides[get_current_db_user] = lambda: _ctx("full_access")
    return TestClient(app)


def test_bulk_confirm_matched_object_is_audited(monkeypatch):
    monkeypatch.setattr(
        alumni_routes.supabase_storage,
        "probe_object_head",
        _probe_stub(("image/png", 1234, _PNG_BYTES[:16])),
    )
    session = _BulkHeadshotSession([_alumnus("jdoe12", 5)])
    try:
        with _confirm_client(session) as c:
            resp = c.post(
                "/alumni/headshots/bulk/confirm",
                json={"files": [{"filename": "jdoe12.png", "uploaded": True}]},
            )
    finally:
        app.dependency_overrides.clear()
    body = resp.json()
    assert body["total"] == 1 and body["matched"] == 1
    assert body["items"][0]["status"] == "matched"
    assert body["items"][0]["net_id"] == "jdoe12"
    audits = [a for a in session.added if getattr(a, "action_type", None)]
    assert [a.action_type for a in audits] == ["upload_headshot"]
    assert audits[0].entity_id == 5
    assert session.committed is True


def test_bulk_confirm_sniffs_real_bytes_and_purges_a_liar(monkeypatch):
    """An object that CLAIMS image/png but whose real leading bytes aren't a PNG
    is deleted and audited as rejected — the browser-supplied Content-Type on a
    direct PUT is only a label."""
    deleted: list = []

    async def _delete(bucket, path):
        deleted.append((bucket, path))

    monkeypatch.setattr(
        alumni_routes.supabase_storage,
        "probe_object_head",
        _probe_stub(("image/png", 1234, b"MZ\x90\x00 not an image")),
    )
    monkeypatch.setattr(alumni_routes.supabase_storage, "delete_object", _delete)
    session = _BulkHeadshotSession([_alumnus("jdoe12", 5)])
    try:
        with _confirm_client(session) as c:
            resp = c.post(
                "/alumni/headshots/bulk/confirm",
                json={"files": [{"filename": "jdoe12.png", "uploaded": True}]},
            )
    finally:
        app.dependency_overrides.clear()
    body = resp.json()
    assert body["invalid"] == 1
    assert body["items"][0]["status"] == "invalid"
    assert deleted == [("headshots", "jdoe12")]
    audits = [a for a in session.added if getattr(a, "action_type", None)]
    assert [a.action_type for a in audits] == ["upload_headshot_rejected"]


def test_bulk_confirm_rejects_disallowed_content_type(monkeypatch):
    deleted: list = []

    async def _delete(bucket, path):
        deleted.append(path)

    monkeypatch.setattr(
        alumni_routes.supabase_storage,
        "probe_object_head",
        _probe_stub(("application/pdf", 100, b"%PDF-1.7")),
    )
    monkeypatch.setattr(alumni_routes.supabase_storage, "delete_object", _delete)
    session = _BulkHeadshotSession([_alumnus("jdoe12", 5)])
    try:
        with _confirm_client(session) as c:
            resp = c.post(
                "/alumni/headshots/bulk/confirm",
                json={"files": [{"filename": "jdoe12.png", "uploaded": True}]},
            )
    finally:
        app.dependency_overrides.clear()
    assert resp.json()["items"][0]["status"] == "invalid"
    assert deleted == ["jdoe12"]


def test_bulk_confirm_rejects_oversized_object(monkeypatch):
    deleted: list = []

    async def _delete(bucket, path):
        deleted.append(path)

    monkeypatch.setattr(
        alumni_routes.supabase_storage,
        "probe_object_head",
        _probe_stub(
            ("image/png", alumni_routes._HEADSHOT_MAX_BYTES + 1, _PNG_BYTES[:16])
        ),
    )
    monkeypatch.setattr(alumni_routes.supabase_storage, "delete_object", _delete)
    session = _BulkHeadshotSession([_alumnus("jdoe12", 5)])
    try:
        with _confirm_client(session) as c:
            resp = c.post(
                "/alumni/headshots/bulk/confirm",
                json={"files": [{"filename": "jdoe12.png", "uploaded": True}]},
            )
    finally:
        app.dependency_overrides.clear()
    item = resp.json()["items"][0]
    assert item["status"] == "invalid"
    assert "20 MB" in item["message"]
    assert deleted == ["jdoe12"]


def test_bulk_confirm_unmatched_and_invalid_names_never_probe_storage(monkeypatch):
    probed: list = []
    monkeypatch.setattr(
        alumni_routes.supabase_storage,
        "probe_object_head",
        _probe_stub(("image/png", 10, _PNG_BYTES[:16]), probed),
    )
    session = _BulkHeadshotSession([_alumnus("jdoe12", 5)])
    try:
        with _confirm_client(session) as c:
            resp = c.post(
                "/alumni/headshots/bulk/confirm",
                json={
                    "files": [
                        {"filename": "nobody99.png", "uploaded": True},
                        {"filename": "notes.txt", "uploaded": True},
                    ]
                },
            )
    finally:
        app.dependency_overrides.clear()
    body = resp.json()
    assert body["no_match"] == 1 and body["invalid"] == 1
    assert probed == []
    assert session.committed is False


def test_bulk_confirm_client_failure_is_an_error_row_not_a_false_success(monkeypatch):
    """A file the browser could not PUT is reported as an error and is NEVER
    audited as uploaded — even though a conforming object is sitting at that key,
    because that object is the alumnus's PREVIOUS headshot, not this upload."""
    probed: list = []
    monkeypatch.setattr(
        alumni_routes.supabase_storage,
        "probe_object_head",
        _probe_stub(("image/png", 10, _PNG_BYTES[:16]), probed),
    )
    session = _BulkHeadshotSession([_alumnus("jdoe12", 5)])
    try:
        with _confirm_client(session) as c:
            resp = c.post(
                "/alumni/headshots/bulk/confirm",
                json={
                    "files": [
                        {
                            "filename": "jdoe12.png",
                            "uploaded": False,
                            "message": "Upload failed (503).",
                        }
                    ]
                },
            )
    finally:
        app.dependency_overrides.clear()
    item = resp.json()["items"][0]
    assert item["status"] == "error"
    assert item["message"] == "Upload failed (503)."
    assert session.committed is False
    assert [a.action_type for a in session.added if getattr(a, "action_type", None)] == []


def test_bulk_confirm_purges_a_bad_object_the_client_claims_it_never_uploaded(
    monkeypatch,
):
    """The ``uploaded`` flag decides what we REPORT, never whether we look.

    Otherwise a client could mint a URL, PUT a non-image, then claim the upload
    failed — skipping the byte-sniffing entirely and leaving that object serving
    as the alumnus's headshot with no terminal audit row."""
    probed: list = []
    deleted: list = []

    async def _delete(bucket, path):
        deleted.append(path)

    monkeypatch.setattr(
        alumni_routes.supabase_storage,
        "probe_object_head",
        _probe_stub(("image/png", 1234, b"MZ\x90\x00 not an image"), probed),
    )
    monkeypatch.setattr(alumni_routes.supabase_storage, "delete_object", _delete)
    session = _BulkHeadshotSession([_alumnus("jdoe12", 5)])
    try:
        with _confirm_client(session) as c:
            resp = c.post(
                "/alumni/headshots/bulk/confirm",
                json={"files": [{"filename": "jdoe12.png", "uploaded": False}]},
            )
    finally:
        app.dependency_overrides.clear()
    assert probed == ["jdoe12"]
    assert deleted == ["jdoe12"]
    assert resp.json()["items"][0]["status"] == "invalid"
    audits = [a for a in session.added if getattr(a, "action_type", None)]
    assert [a.action_type for a in audits] == ["upload_headshot_rejected"]


def test_bulk_confirm_client_detail_is_sanitized(monkeypatch):
    """The browser's failure detail is reflected back to the operator, so it is
    stripped of control characters and length-capped."""
    monkeypatch.setattr(
        alumni_routes.supabase_storage,
        "probe_object_head",
        _probe_stub(("image/png", 10, _PNG_BYTES[:16])),
    )
    session = _BulkHeadshotSession([_alumnus("jdoe12", 5)])
    try:
        with _confirm_client(session) as c:
            resp = c.post(
                "/alumni/headshots/bulk/confirm",
                json={
                    "files": [
                        {
                            "filename": "jdoe12.png",
                            "uploaded": False,
                            "message": "boom\r\n\x00" + "x" * 500,
                        }
                    ]
                },
            )
    finally:
        app.dependency_overrides.clear()
    message = resp.json()["items"][0]["message"]
    assert len(message) <= 120
    assert "\x00" not in message and "\n" not in message


def test_bulk_confirm_probe_unreadable_fails_open(monkeypatch):
    """A probe that can't read the object falls back to an existence check rather
    than rejecting a legitimate upload."""
    monkeypatch.setattr(
        alumni_routes.supabase_storage,
        "probe_object_head",
        _probe_stub((None, None, None)),
    )

    async def _signed(bucket, path, expires_in=3600):
        return "https://storage.test/signed"

    monkeypatch.setattr(alumni_routes.supabase_storage, "create_signed_url", _signed)
    session = _BulkHeadshotSession([_alumnus("jdoe12", 5)])
    try:
        with _confirm_client(session) as c:
            resp = c.post(
                "/alumni/headshots/bulk/confirm",
                json={"files": [{"filename": "jdoe12.png", "uploaded": True}]},
            )
    finally:
        app.dependency_overrides.clear()
    assert resp.json()["items"][0]["status"] == "matched"


def test_bulk_confirm_missing_object_cannot_be_confirmed(monkeypatch):
    """A key that was never uploaded to can't be talked into an
    ``upload_headshot`` audit row by claiming ``uploaded: true``."""
    monkeypatch.setattr(
        alumni_routes.supabase_storage,
        "probe_object_head",
        _probe_stub((None, None, None)),
    )

    async def _signed(bucket, path, expires_in=3600):
        return None

    monkeypatch.setattr(alumni_routes.supabase_storage, "create_signed_url", _signed)
    session = _BulkHeadshotSession([_alumnus("jdoe12", 5)])
    try:
        with _confirm_client(session) as c:
            resp = c.post(
                "/alumni/headshots/bulk/confirm",
                json={"files": [{"filename": "jdoe12.png", "uploaded": True}]},
            )
    finally:
        app.dependency_overrides.clear()
    assert resp.json()["items"][0]["status"] == "error"
    assert session.committed is False


def test_bulk_confirm_duplicate_net_id_probed_once(monkeypatch):
    """Two files for the same alumnus overwrite one object, so it is validated
    once and both rows share the verdict — probing twice could delete the image
    that actually survived."""
    probed: list = []
    monkeypatch.setattr(
        alumni_routes.supabase_storage,
        "probe_object_head",
        _probe_stub(("image/png", 500, _PNG_BYTES[:16]), probed),
    )
    session = _BulkHeadshotSession([_alumnus("jdoe12", 5)])
    try:
        with _confirm_client(session) as c:
            resp = c.post(
                "/alumni/headshots/bulk/confirm",
                json={
                    "files": [
                        {"filename": "jdoe12.jpg", "uploaded": True},
                        {"filename": "jdoe12.png", "uploaded": True},
                    ]
                },
            )
    finally:
        app.dependency_overrides.clear()
    body = resp.json()
    assert body["matched"] == 2
    assert probed == ["jdoe12"]


def test_bulk_confirm_over_per_request_cap_is_413(client):
    app.dependency_overrides[get_current_db_user] = lambda: _ctx("full_access")
    files = [
        {"filename": f"user{i}.png", "uploaded": True}
        for i in range(alumni_routes._HEADSHOT_BULK_MAX_PER_REQUEST + 1)
    ]
    resp = client.post("/alumni/headshots/bulk/confirm", json={"files": files})
    assert resp.status_code == 413


# --- #419 the SINGLE confirm sniffs the real bytes too ------------------------
#
# The single direct-upload confirm and the bulk one are the same operation on the
# same bucket, and they had drifted: bulk sniffed the object's real leading bytes
# (`test_bulk_confirm_sniffs_real_bytes_and_purges_a_liar` above), while single
# checked only the Content-Type the uploader's own PUT declared. Anyone holding
# the photo capability could mint a legitimate signed URL, skip the cropper, PUT
# arbitrary bytes labelled image/jpeg and have them audited as a verified
# headshot. The single path had only a 403 test, which is why nobody noticed —
# so these pin the behaviour rather than only the gate.


class _SingleConfirmSession:
    """Resolves the alumnus for the net_id lookup; records audit adds/commits."""

    def __init__(self, alumnus=None):
        self._alumnus = alumnus
        self.added: list = []
        self.commits = 0

    async def scalar(self, stmt):
        return self._alumnus

    def add(self, obj):
        self.added.append(obj)

    async def commit(self):
        self.commits += 1


def _single_confirm_audits(session):
    return [a.action_type for a in session.added if getattr(a, "action_type", None)]


def test_single_confirm_rejects_an_empty_object(monkeypatch):
    """A 0-byte object labelled ``image/jpeg`` is REJECTED, not confirmed.

    Found re-reviewing the #419 fix: `probe_object_head` returned ``None`` for
    the head both when the probe FAILED and when the object was genuinely
    EMPTY, and the call site tested truthiness — so an empty upload skipped the
    magic-byte check entirely and was audited as a successful headshot, in the
    one code path whose whole job is to distrust the label. An empty file is
    never a valid JPEG/PNG/WebP, so "we looked and there was nothing there" has
    to fail closed; only a failed probe fails open.
    """
    deleted: list = []

    async def _delete(bucket, path):
        deleted.append((bucket, path))

    monkeypatch.setattr(
        alumni_routes.supabase_storage,
        "probe_object_head",
        _probe_stub(("image/jpeg", 0, b"")),
    )
    monkeypatch.setattr(alumni_routes.supabase_storage, "delete_object", _delete)
    session = _SingleConfirmSession(_alumnus("jdoe12", 5))
    try:
        with _confirm_client(session) as c:
            resp = c.post("/alumni/5/headshot/confirm")
    finally:
        app.dependency_overrides.clear()
    assert resp.status_code == 422
    assert deleted == [("headshots", "jdoe12")]
    assert _single_confirm_audits(session) == ["upload_headshot_rejected"]


def test_single_confirm_still_fails_open_when_the_probe_fails(monkeypatch):
    """``head=None`` means the probe itself failed, and that must still FAIL
    OPEN — a storage hiccup must never reject a legitimate upload. This is the
    other half of the empty-object fix: the two cases used to be
    indistinguishable, and tightening one must not tighten this one.

    Note it does not fail open blindly: it still proves the object EXISTS via a
    signed URL, so a never-uploaded key cannot be confirmed. That is why this
    test has to stub `create_signed_url` — without it the route correctly 422s.
    """

    async def _signed(bucket, path, *, expires_in: int = 3600):
        return "https://storage.example/signed"

    monkeypatch.setattr(
        alumni_routes.supabase_storage,
        "probe_object_head",
        _probe_stub((None, None, None)),
    )
    monkeypatch.setattr(
        alumni_routes.supabase_storage, "create_signed_url", _signed
    )
    session = _SingleConfirmSession(_alumnus("jdoe12", 5))
    try:
        with _confirm_client(session) as c:
            resp = c.post("/alumni/5/headshot/confirm")
    finally:
        app.dependency_overrides.clear()
    assert resp.status_code == 204
    assert _single_confirm_audits(session) == ["upload_headshot"]


def test_single_confirm_sniffs_real_bytes_and_purges_a_liar(monkeypatch):
    """A non-image body labelled ``image/jpeg`` is REJECTED, deleted, and audited
    ``upload_headshot_rejected`` — never recorded as a verified headshot. The
    declared type came from the attacker's own PUT, so only the magic bytes are
    evidence of anything (#419)."""
    deleted: list = []

    async def _delete(bucket, path):
        deleted.append((bucket, path))

    monkeypatch.setattr(
        alumni_routes.supabase_storage,
        "probe_object_head",
        _probe_stub(("image/jpeg", 1234, b"MZ\x90\x00 not an image")),
    )
    monkeypatch.setattr(alumni_routes.supabase_storage, "delete_object", _delete)
    session = _SingleConfirmSession(_alumnus("jdoe12", 5))
    try:
        with _confirm_client(session) as c:
            resp = c.post("/alumni/5/headshot/confirm")
    finally:
        app.dependency_overrides.clear()
    # InvalidRequestError -> 422 (the app's validation-error status).
    assert resp.status_code == 422
    assert deleted == [("headshots", "jdoe12")]
    assert _single_confirm_audits(session) == ["upload_headshot_rejected"]
    audit = session.added[0]
    assert audit.entity_id == 5
    assert audit.field_name == "content"


def test_single_confirm_rejects_a_real_image_of_the_wrong_type(monkeypatch):
    """PNG bytes served as ``image/jpeg`` still contradict their label, so the
    object is purged — the same verdict ``_image_content_error`` gives on the
    multipart upload path."""
    deleted: list = []

    async def _delete(bucket, path):
        deleted.append(path)

    monkeypatch.setattr(
        alumni_routes.supabase_storage,
        "probe_object_head",
        _probe_stub(("image/jpeg", 1234, _PNG_BYTES[:16])),
    )
    monkeypatch.setattr(alumni_routes.supabase_storage, "delete_object", _delete)
    session = _SingleConfirmSession(_alumnus("jdoe12", 5))
    try:
        with _confirm_client(session) as c:
            resp = c.post("/alumni/5/headshot/confirm")
    finally:
        app.dependency_overrides.clear()
    assert resp.status_code == 422
    assert deleted == ["jdoe12"]
    assert _single_confirm_audits(session) == ["upload_headshot_rejected"]


def test_single_confirm_accepts_a_conforming_object(monkeypatch):
    """The happy path is unchanged: matching bytes + type + size are audited
    ``upload_headshot`` and nothing is deleted."""
    deleted: list = []

    async def _delete(bucket, path):
        deleted.append(path)

    monkeypatch.setattr(
        alumni_routes.supabase_storage,
        "probe_object_head",
        _probe_stub(("image/jpeg", 1234, _JPEG_BYTES[:16])),
    )
    monkeypatch.setattr(alumni_routes.supabase_storage, "delete_object", _delete)
    session = _SingleConfirmSession(_alumnus("jdoe12", 5))
    try:
        with _confirm_client(session) as c:
            resp = c.post("/alumni/5/headshot/confirm")
    finally:
        app.dependency_overrides.clear()
    assert resp.status_code == 204
    assert deleted == []
    assert _single_confirm_audits(session) == ["upload_headshot"]


def test_single_confirm_probe_unreadable_fails_open(monkeypatch):
    """A probe that can't read the object falls back to an existence check rather
    than rejecting a legitimate upload — the sniff must not turn a storage hiccup
    into a failed upload."""
    monkeypatch.setattr(
        alumni_routes.supabase_storage,
        "probe_object_head",
        _probe_stub((None, None, None)),
    )

    async def _signed(bucket, path, expires_in=3600):
        return "https://storage.test/signed"

    monkeypatch.setattr(alumni_routes.supabase_storage, "create_signed_url", _signed)
    session = _SingleConfirmSession(_alumnus("jdoe12", 5))
    try:
        with _confirm_client(session) as c:
            resp = c.post("/alumni/5/headshot/confirm")
    finally:
        app.dependency_overrides.clear()
    assert resp.status_code == 204
    assert _single_confirm_audits(session) == ["upload_headshot"]


def test_single_confirm_still_rejects_a_disallowed_content_type(monkeypatch):
    """The type check that was already here keeps working alongside the sniff."""
    deleted: list = []

    async def _delete(bucket, path):
        deleted.append(path)

    monkeypatch.setattr(
        alumni_routes.supabase_storage,
        "probe_object_head",
        _probe_stub(("application/pdf", 100, b"%PDF-1.7")),
    )
    monkeypatch.setattr(alumni_routes.supabase_storage, "delete_object", _delete)
    session = _SingleConfirmSession(_alumnus("jdoe12", 5))
    try:
        with _confirm_client(session) as c:
            resp = c.post("/alumni/5/headshot/confirm")
    finally:
        app.dependency_overrides.clear()
    assert resp.status_code == 422
    assert deleted == ["jdoe12"]
    audit = session.added[0]
    assert audit.field_name == "content_type"


def test_single_confirm_still_rejects_an_oversized_object(monkeypatch):
    deleted: list = []

    async def _delete(bucket, path):
        deleted.append(path)

    monkeypatch.setattr(
        alumni_routes.supabase_storage,
        "probe_object_head",
        _probe_stub(
            ("image/jpeg", alumni_routes._HEADSHOT_MAX_BYTES + 1, _JPEG_BYTES[:16])
        ),
    )
    monkeypatch.setattr(alumni_routes.supabase_storage, "delete_object", _delete)
    session = _SingleConfirmSession(_alumnus("jdoe12", 5))
    try:
        with _confirm_client(session) as c:
            resp = c.post("/alumni/5/headshot/confirm")
    finally:
        app.dependency_overrides.clear()
    assert resp.status_code == 413
    assert deleted == ["jdoe12"]
    audit = session.added[0]
    assert audit.field_name == "size"


def test_bulk_headshot_routes_are_rate_limited(client):
    """Both bulk routes share one per-actor budget, so a loop can't churn the
    whole directory even though the batch is now chunked."""
    rate_limit.reset()
    app.dependency_overrides[get_current_db_user] = lambda: _ctx("full_access")
    statuses = [
        client.post("/alumni/headshots/bulk/upload-urls", json={"filenames": []}).status_code
        for _ in range(120)  # BULK_HEADSHOT_LIMITER allows 100 per 10 min
    ]
    rate_limit.reset()
    assert 429 in statuses


# --- Batch headshot URLs (GET /alumni/headshots/urls) -------------------------
#
# The roster used to mint one signed URL PER ROW per render. These pin the two
# properties that make the batch route cheaper than that loop: one storage
# round-trip per DISTINCT alumnus that actually has a net ID, and none at all for
# anyone who doesn't.


def _sign_stub(calls):
    async def _create(bucket, path, **kwargs):
        calls.append((bucket, path))
        return f"https://storage.test/sign/{bucket}/{path}?token=abc"

    return _create


def test_headshot_urls_requires_auth(client):
    assert client.get("/alumni/headshots/urls?alumni_ids=1").status_code == 401


def test_headshot_urls_signs_once_per_alumnus_with_a_net_id(monkeypatch):
    """One signature per alumnus that HAS a net ID; an alumnus without one (and
    an id that matches nobody) resolves to null with no storage call at all."""
    calls: list = []
    monkeypatch.setattr(
        alumni_routes.supabase_storage, "create_signed_url", _sign_stub(calls)
    )
    session = _BulkHeadshotSession([_alumnus("jdoe12", 5), _alumnus(None, 6)])
    app.dependency_overrides[get_session] = _with_session(session)
    app.dependency_overrides[get_current_db_user] = lambda: _ctx("view_only")
    try:
        with TestClient(app) as c:
            resp = c.get("/alumni/headshots/urls?alumni_ids=5&alumni_ids=6&alumni_ids=7")
    finally:
        app.dependency_overrides.clear()
    assert resp.status_code == 200
    urls = resp.json()["urls"]
    # Every requested id is answered, so the caller never has to guess.
    assert set(urls) == {"5", "6", "7"}
    assert urls["5"].startswith("https://storage.test/sign/headshots/jdoe12")
    assert urls["6"] is None  # no net_id -> no object key
    assert urls["7"] is None  # unknown alumnus -> null, not 404
    assert calls == [("headshots", "jdoe12")]


def test_headshot_urls_deduplicates_repeated_ids(monkeypatch):
    """The same alumnus asked for five times costs ONE signature — the exact
    waste seen in the storage logs."""
    calls: list = []
    monkeypatch.setattr(
        alumni_routes.supabase_storage, "create_signed_url", _sign_stub(calls)
    )
    session = _BulkHeadshotSession([_alumnus("jdoe12", 5)])
    app.dependency_overrides[get_session] = _with_session(session)
    app.dependency_overrides[get_current_db_user] = lambda: _ctx("view_only")
    try:
        with TestClient(app) as c:
            resp = c.get("/alumni/headshots/urls?" + "&".join(["alumni_ids=5"] * 5))
    finally:
        app.dependency_overrides.clear()
    assert resp.status_code == 200
    assert calls == [("headshots", "jdoe12")]


def test_headshot_urls_survives_an_unavailable_storage_service(monkeypatch):
    """A storage outage costs that row its photo (initials fallback), never the
    whole page."""

    async def _boom(bucket, path, **kwargs):
        raise alumni_routes.ServiceError("storage down")

    monkeypatch.setattr(alumni_routes.supabase_storage, "create_signed_url", _boom)
    session = _BulkHeadshotSession([_alumnus("jdoe12", 5)])
    app.dependency_overrides[get_session] = _with_session(session)
    app.dependency_overrides[get_current_db_user] = lambda: _ctx("view_only")
    try:
        with TestClient(app) as c:
            resp = c.get("/alumni/headshots/urls?alumni_ids=5")
    finally:
        app.dependency_overrides.clear()
    assert resp.status_code == 200
    assert resp.json()["urls"] == {"5": None}


def test_headshot_urls_rejects_an_oversized_batch(client):
    app.dependency_overrides[get_current_db_user] = lambda: _ctx("view_only")
    ids = "&".join(
        f"alumni_ids={i}" for i in range(alumni_routes._HEADSHOT_BATCH_MAX + 1)
    )
    resp = client.get(f"/alumni/headshots/urls?{ids}")
    assert resp.status_code == 422


def test_headshot_urls_requires_at_least_one_id(client):
    app.dependency_overrides[get_current_db_user] = lambda: _ctx("view_only")
    assert client.get("/alumni/headshots/urls").status_code == 422
