"""FERPA / privacy tests for the alumni read + export paths (no database).

These cover the disclosure-minimization and audit guarantees added for FERPA:
  * view_only ("Professor") callers get sensitive PII / notes nulled on every
    alumni read, and the profile aggregate strips free-text notes + audit trail;
  * archived records 404 on a direct GET (single + profile);
  * the server-side export endpoint requires full_access, minimizes the payload
    (no embedded audit, no internal user PKs), and writes an audit row;
  * the profile aggregate read writes a ``view_profile`` audit row.

A tiny fake session (no real DATABASE_URL) drives the service so the rules are
verified end to end without Postgres.
"""

import asyncio
import datetime
import uuid

import pytest
from fastapi.testclient import TestClient

from app.api.dependencies.auth import get_current_db_user
from app.core.database import get_session
from app.core.errors import NotFoundError
from app.main import app
from app.models.alumni import Alumni
from app.models.audit import AuditLog
from app.schemas.alumni import (
    VIEW_ONLY_HIDDEN_FIELDS,
    AlumniListItem,
    AlumniRead,
    minimize_alumni_read,
)
from app.schemas.auth import UserContext
from app.services import profile as profile_service


def _ctx(*roles: str) -> UserContext:
    return UserContext(
        user_id=7,
        auth_user_id=uuid.UUID("11111111-1111-1111-1111-111111111111"),
        roles=list(roles),
    )


def _alumni_model(**kw) -> Alumni:
    now = datetime.datetime(2026, 6, 12, tzinfo=datetime.UTC)
    base = dict(
        alumni_id=1,
        source_id=9,
        byu_id="123456789",
        net_id="jdoe12",
        mst_id="MST-1",
        first_name="Jane",
        last_name="Doe",
        gender="Female",
        birth_year=1990,
        birth_date=datetime.date(1990, 5, 1),
        graduation_year=2012,
        spouse_first_name="John",
        spouse_last_name="Doe",
        spouse_birth_date=datetime.date(1989, 1, 1),
        deceased=False,
        notes="private note",
        archived=False,
        manually_edited_at=now,
        last_imported_at=now,
        created_at=now,
        updated_at=now,
    )
    base.update(kw)
    return Alumni(**base)


# --- Finding 4: AlumniRead role-scoping --------------------------------------


def test_minimize_nulls_sensitive_fields_for_view_only():
    read = AlumniRead.model_validate(_alumni_model())
    scoped = minimize_alumni_read(read, can_edit=False)
    for field in VIEW_ONLY_HIDDEN_FIELDS:
        assert getattr(scoped, field) is None, field
    # Non-sensitive directory fields survive.
    assert scoped.first_name == "Jane"
    assert scoped.last_name == "Doe"
    assert scoped.graduation_year == 2012


def test_minimize_leaves_edit_caller_untouched():
    read = AlumniRead.model_validate(_alumni_model())
    scoped = minimize_alumni_read(read, can_edit=True)
    assert scoped.byu_id == "123456789"
    assert scoped.notes == "private note"
    assert scoped.gender == "Female"


def test_minimize_applies_to_list_item():
    item = AlumniListItem.model_validate(_alumni_model())
    item = item.model_copy(update={"current_employer": "Goldman"})
    scoped = minimize_alumni_read(item, can_edit=False)
    assert scoped.byu_id is None
    assert scoped.notes is None
    # Joined list-only columns are not sensitive and remain.
    assert scoped.current_employer == "Goldman"


# --- Fake session for service-level profile/export tests ---------------------


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

    def all(self):
        return list(self._rows)


class _ProfileFakeSession:
    """Returns the configured alumnus from ``get`` and empty collections from
    every aggregate query, so ``get_profile`` assembles a minimal aggregate.
    Captures ``add``/``commit`` so audit writes can be asserted."""

    def __init__(self, alumnus):
        self._alumnus = alumnus
        self.added: list[object] = []
        self.commits = 0

    async def get(self, model, pk):
        if model is Alumni and pk == self._alumnus.alumni_id:
            return self._alumnus
        return None

    async def scalar(self, stmt):
        # interaction_count + any single-row lookups -> nothing.
        return 0

    async def scalars(self, stmt):
        return _Scalars([])

    async def execute(self, stmt):
        return _ExecResult([])

    def add(self, obj):
        self.added.append(obj)

    async def commit(self):
        self.commits += 1


def _audit_rows(session) -> list[AuditLog]:
    return [a for a in session.added if isinstance(a, AuditLog)]


# --- Finding 3: archived records 404 -----------------------------------------


def test_profile_archived_returns_not_found():
    session = _ProfileFakeSession(_alumni_model(archived=True))
    with pytest.raises(NotFoundError):
        asyncio.run(profile_service.get_profile(session, 1, actor_user_id=7))


def test_profile_archived_allowed_with_include_archived():
    session = _ProfileFakeSession(_alumni_model(archived=True))
    profile = asyncio.run(
        profile_service.get_profile(session, 1, include_archived=True)
    )
    assert profile.alumni.alumni_id == 1


# --- Finding 2: profile read writes a view_profile audit row -----------------


def test_profile_read_writes_view_audit_row():
    session = _ProfileFakeSession(_alumni_model())
    asyncio.run(profile_service.get_profile(session, 1, actor_user_id=7))
    rows = _audit_rows(session)
    assert any(
        r.action_type == "view_profile"
        and r.entity_type == "alumni"
        and r.entity_id == 1
        and r.user_id == 7
        for r in rows
    )


def test_profile_read_unknown_actor_no_audit():
    session = _ProfileFakeSession(_alumni_model())
    asyncio.run(profile_service.get_profile(session, 1, actor_user_id=None))
    assert _audit_rows(session) == []


# --- Finding 4/5: view_only profile minimization -----------------------------


def test_profile_view_only_minimized():
    session = _ProfileFakeSession(_alumni_model())
    profile = asyncio.run(
        profile_service.get_profile(session, 1, can_edit=False, actor_user_id=7)
    )
    assert profile.alumni.byu_id is None
    assert profile.alumni.notes is None
    # Audit trail omitted for view_only.
    assert profile.audit == []


def test_profile_edit_caller_full_fidelity():
    session = _ProfileFakeSession(_alumni_model())
    profile = asyncio.run(
        profile_service.get_profile(session, 1, can_edit=True, actor_user_id=7)
    )
    assert profile.alumni.byu_id == "123456789"
    assert profile.alumni.notes == "private note"


# --- Finding 1: export ---------------------------------------------------------


def test_export_minimizes_and_audits():
    session = _ProfileFakeSession(_alumni_model())
    data = asyncio.run(
        profile_service.export_profile(session, 1, actor_user_id=7)
    )
    # Minimized: no embedded audit trail in the exported body.
    assert "audit" not in data
    # Full-fidelity core for a full_access export (not view_only-scoped).
    assert data["alumni"]["byu_id"] == "123456789"
    # An export_profile audit row was written.
    rows = _audit_rows(session)
    assert any(
        r.action_type == "export_profile" and r.entity_id == 1 and r.user_id == 7
        for r in rows
    )


def test_export_archived_404():
    session = _ProfileFakeSession(_alumni_model(archived=True))
    with pytest.raises(NotFoundError):
        asyncio.run(profile_service.export_profile(session, 1, actor_user_id=7))


# --- Route-level: export requires full_access --------------------------------


async def _no_db_session():
    yield None


@pytest.fixture
def client():
    app.dependency_overrides[get_session] = _no_db_session
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


def test_export_requires_auth(client):
    resp = client.get("/alumni/1/export")
    assert resp.status_code == 401
    assert resp.json()["error"]["code"] == "unauthorized"


def test_export_forbidden_for_view_only(client):
    app.dependency_overrides[get_current_db_user] = lambda: _ctx("view_only")
    resp = client.get("/alumni/1/export")
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "forbidden"


def test_export_forbidden_for_student(client):
    # student can edit but not full_access; export is full_access-gated.
    app.dependency_overrides[get_current_db_user] = lambda: _ctx("student")
    resp = client.get("/alumni/1/export")
    assert resp.status_code == 403
