"""#593 — changing is_alumni / deceased / net_id / byu_id needs ``alumni.archive``.

``alumni.edit`` (student and up) may still SEND those fields unchanged — the
focused edit forms re-submit what they loaded — but a real change without
``alumni.archive`` is a 403 with nothing written. Driven end to end through
``PATCH /alumni/{id}`` against a tiny fake session (no database), with duplicate
detection stubbed since it is not what is under test.
"""

import datetime
import uuid
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app.api.dependencies.auth import get_current_db_user, get_permission_config
from app.core.capabilities import DEFAULT_GRANTS, Capability
from app.core.database import get_session
from app.main import app
from app.schemas.auth import UserContext
from app.services import alumni as alumni_service
from app.services import hygiene


def _ctx(*roles: str) -> UserContext:
    return UserContext(
        user_id=1,
        auth_user_id=uuid.UUID("11111111-1111-1111-1111-111111111111"),
        roles=list(roles),
    )


def _alum(**kw):
    now = datetime.datetime(2026, 6, 12, tzinfo=datetime.UTC)
    base = dict(
        alumni_id=5,
        source_id=None,
        first_name="Jane",
        last_name="Doe",
        graduation_year=2018,
        byu_id="123456789",
        net_id="JDOE12",
        deceased=False,
        is_alumni=True,
        archived=False,
        spouse_alumni_id=None,
        manually_edited_at=None,
        profile_updated_by_user_id=None,
        created_at=now,
        updated_at=now,
    )
    base.update(kw)
    return SimpleNamespace(**base)


class _Session:
    def __init__(self, alumnus):
        self.alumnus = alumnus
        self.added: list = []
        self.commits = 0

    async def get(self, _model, _pk):
        return self.alumnus

    async def scalar(self, _stmt):
        return None

    def add(self, obj):
        self.added.append(obj)

    async def commit(self):
        self.commits += 1

    async def refresh(self, _obj):
        pass


@pytest.fixture(autouse=True)
def _no_duplicate_detection(monkeypatch):
    async def _none(*_a, **_kw):
        return [], []

    monkeypatch.setattr(hygiene, "detect_duplicates", _none)


def _patch(session, role, body, config=None):
    async def _s():
        yield session

    app.dependency_overrides[get_session] = _s
    app.dependency_overrides[get_current_db_user] = lambda: _ctx(role)
    if config is not None:
        app.dependency_overrides[get_permission_config] = lambda: config
    try:
        with TestClient(app, raise_server_exceptions=False) as client:
            return client.patch("/alumni/5", json=body)
    finally:
        app.dependency_overrides.pop(get_session, None)
        app.dependency_overrides.pop(get_current_db_user, None)
        app.dependency_overrides[get_permission_config] = lambda: dict(DEFAULT_GRANTS)


@pytest.mark.parametrize(
    "body",
    [
        {"is_alumni": False},
        {"deceased": True},
        {"net_id": "someoneelse"},
        {"byu_id": "987654321"},
    ],
)
def test_student_cannot_change_identity_fields(body):
    alumnus = _alum()
    session = _Session(alumnus)
    resp = _patch(session, "student", body)
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "forbidden"
    # Nothing was written: stored values intact, no audit rows, no commit.
    assert alumnus.is_alumni is True
    assert alumnus.deceased is False
    assert alumnus.net_id == "JDOE12"
    assert alumnus.byu_id == "123456789"
    assert session.added == []
    assert session.commits == 0


def test_student_can_resend_unchanged_identity_fields():
    """A full-form save re-sends what it loaded. The legacy upper-case net ID and
    a dashed BYU ID normalise to the stored values, so this is not a change."""
    alumnus = _alum()
    session = _Session(alumnus)
    resp = _patch(
        session,
        "student",
        {
            "net_id": "jdoe12",
            "byu_id": "123-45-6789",
            "deceased": False,
            "is_alumni": True,
            "first_name": "Janet",
        },
    )
    assert resp.status_code == 200, resp.text
    assert alumnus.first_name == "Janet"
    assert session.commits == 1
    # Unchanged after normalisation -> not WRITTEN either: the stored net ID
    # (the headshot object key) keeps its original form.
    assert alumnus.net_id == "JDOE12"
    assert alumnus.byu_id == "123456789"


def test_student_can_still_edit_other_fields():
    alumnus = _alum()
    session = _Session(alumnus)
    resp = _patch(session, "student", {"last_name": "Smith"})
    assert resp.status_code == 200, resp.text
    assert alumnus.last_name == "Smith"


@pytest.mark.parametrize("role", ["full_access", "super_admin", "engineer"])
def test_archive_holders_can_change_identity_fields(role):
    alumnus = _alum()
    session = _Session(alumnus)
    resp = _patch(
        session,
        role,
        {"is_alumni": False, "deceased": True, "net_id": "newid1", "byu_id": "111222333"},
    )
    assert resp.status_code == 200, resp.text
    assert alumnus.is_alumni is False
    assert alumnus.deceased is True
    assert alumnus.net_id == "newid1"
    assert alumnus.byu_id == "111222333"


def test_gate_follows_the_capability_not_the_role():
    """An engineer granting ``alumni.archive`` to student lifts the gate."""
    config = dict(DEFAULT_GRANTS)
    config["student"] = frozenset(config["student"]) | {Capability.ALUMNI_ARCHIVE}
    alumnus = _alum()
    session = _Session(alumnus)
    resp = _patch(session, "student", {"is_alumni": False}, config=config)
    assert resp.status_code == 200, resp.text
    assert alumnus.is_alumni is False


def test_identity_fields_changed_helper():
    alumnus = _alum(net_id=None, byu_id=None)
    assert alumni_service.identity_fields_changed(alumnus, {"net_id": None}) == []
    assert alumni_service.identity_fields_changed(alumnus, {"net_id": ""}) == []
    assert alumni_service.identity_fields_changed(alumnus, {"net_id": "x1"}) == [
        "net_id"
    ]
    assert alumni_service.identity_fields_changed(
        alumnus, {"first_name": "Q", "deceased": True}
    ) == ["deceased"]


def test_service_default_fails_closed():
    """A caller that forgets to pass the capability gets the restrictive
    behaviour, not the permissive one."""
    import asyncio

    from app.core.security import AuthorizationError
    from app.schemas.alumni import AlumniUpdateFull

    alumnus = _alum()
    with pytest.raises(AuthorizationError):
        asyncio.run(
            alumni_service.update_alumni(
                _Session(alumnus), 5, AlumniUpdateFull(is_alumni=False), actor_user_id=1
            )
        )
    assert alumnus.is_alumni is True


# --- the bulk-update importer passes the caller's alumni.archive --------------


@pytest.mark.parametrize(
    ("grant_archive", "expected"), [(True, True), (False, False)]
)
def test_import_update_route_passes_the_archive_capability(monkeypatch, grant_archive, expected):
    from app.api.routes import alumni as alumni_routes

    seen: dict = {}

    async def _commit(_session, rows, *, actor_user_id, can_change_identity):
        seen["can_change_identity"] = can_change_identity
        return {
            "updated": 0, "unchanged": 0, "unmatched": 0, "errors": 0,
            "results": [], "updated_ids": [],
        }

    monkeypatch.setattr(
        alumni_routes.import_csv, "parse_and_map_partial", lambda *_a, **_k: ([{}], [], [])
    )
    monkeypatch.setattr(alumni_routes.import_csv, "commit_update", _commit)

    # A student granted alumni.import (assignable) with or without archive.
    config = dict(DEFAULT_GRANTS)
    extra = {Capability.ALUMNI_IMPORT} | ({Capability.ALUMNI_ARCHIVE} if grant_archive else set())
    config["student"] = frozenset(config["student"]) | extra

    async def _s():
        yield None

    app.dependency_overrides[get_session] = _s
    app.dependency_overrides[get_current_db_user] = lambda: _ctx("student")
    app.dependency_overrides[get_permission_config] = lambda: config
    try:
        with TestClient(app) as client:
            resp = client.post(
                "/alumni/import/update",
                files={"file": ("u.csv", b"alumni_id\n1\n", "text/csv")},
            )
    finally:
        app.dependency_overrides.pop(get_session, None)
        app.dependency_overrides.pop(get_current_db_user, None)
        app.dependency_overrides[get_permission_config] = lambda: dict(DEFAULT_GRANTS)
    assert resp.status_code == 200, resp.text
    assert seen["can_change_identity"] is expected
