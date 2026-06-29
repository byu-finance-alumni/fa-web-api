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
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app.api.dependencies.auth import get_current_db_user
from app.core.database import get_session
from app.core.errors import NotFoundError
from app.main import app
from app.models.alumni import Alumni
from app.models.audit import AuditLog
from app.models.crm import Attachment, Interaction
from app.models.user import User
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


def test_minimize_strips_interaction_notes_but_preserves_logged_by():
    from app.schemas.profile import InteractionRead, ProfileRead
    from app.services.profile import _minimize_profile_for_view_only

    # logged_by is set to the first name upstream in get_profile; minimization
    # must strip the free-text notes but leave logged_by untouched.
    interaction = InteractionRead(
        interaction_id=1,
        interaction_type="Phone Call",
        interaction_date_time=datetime.datetime(2026, 6, 1, tzinfo=datetime.UTC),
        interaction_notes="sensitive note",
        logged_by="Tanya",
    )
    profile = ProfileRead.model_construct(
        alumni=AlumniRead.model_validate(_alumni_model()),
        interactions=[interaction],
        surveys=[],
        engagement_notes=[],
        program_engagement=None,
        audit=[],
    )
    scoped = _minimize_profile_for_view_only(profile)
    assert scoped.interactions[0].interaction_notes is None  # notes stripped
    assert scoped.interactions[0].logged_by == "Tanya"  # first name preserved
    assert scoped.interactions[0].interaction_type == "Phone Call"
    assert scoped.interactions[0].interaction_date_time is not None


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


class _ProfileInteractionFakeSession(_ProfileFakeSession):
    """Like ``_ProfileFakeSession`` but returns one interaction (logged by
    ``user``) and resolves that user, so ``logged_by`` name-building is covered."""

    def __init__(self, alumnus, interaction, user):
        super().__init__(alumnus)
        self._interaction = interaction
        self._user = user

    async def get(self, model, pk):
        if model is User and pk == self._user.user_id:
            return self._user
        return await super().get(model, pk)

    async def scalars(self, stmt):
        entity = stmt.column_descriptions[0]["entity"]
        if entity is Interaction:
            return _Scalars([self._interaction])
        if entity is User:
            return _Scalars([self._user])
        return _Scalars([])


def _logger_user():
    return SimpleNamespace(
        user_id=42, first_name="Tanya", last_name="Harmon", email="th@byu.edu"
    )


def _one_interaction():
    return SimpleNamespace(
        interaction_id=1,
        user_id=42,
        interaction_type="Phone Call",
        interaction_date_time=datetime.datetime(2026, 6, 1, tzinfo=datetime.UTC),
        interaction_notes="sensitive note",
    )


def test_profile_view_only_logged_by_is_first_name():
    session = _ProfileInteractionFakeSession(
        _alumni_model(), _one_interaction(), _logger_user()
    )
    profile = asyncio.run(
        profile_service.get_profile(session, 1, can_edit=False, actor_user_id=7)
    )
    # view_only sees WHO made contact by first name, but not the full name or
    # the free-text notes.
    assert profile.interactions[0].logged_by == "Tanya"
    assert profile.interactions[0].interaction_notes is None


def test_profile_editor_logged_by_is_full_name():
    session = _ProfileInteractionFakeSession(
        _alumni_model(), _one_interaction(), _logger_user()
    )
    profile = asyncio.run(
        profile_service.get_profile(session, 1, can_edit=True, actor_user_id=7)
    )
    assert profile.interactions[0].logged_by == "Tanya Harmon"
    assert profile.interactions[0].interaction_notes == "sensitive note"


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


# --- #112b: AuditEntryRead / AttachmentRead hide internal user PKs ------------
#
# Both schemas now resolve the internal user PK to a display name and DROP the
# raw integer PK from the response (matching InteractionRead/TaskRead).


class _ProfileAuditAttachmentFakeSession(_ProfileFakeSession):
    """Returns one audit row and one attachment for the aggregate, plus the
    uploader user so ``uploaded_by`` name-building (a single batched lookup) is
    covered."""

    def __init__(self, alumnus, audit_row, attachment, uploader):
        super().__init__(alumnus)
        self._audit_row = audit_row
        self._attachment = attachment
        self._uploader = uploader

    async def get(self, model, pk):
        if model is User and pk == self._uploader.user_id:
            return self._uploader
        return await super().get(model, pk)

    async def scalars(self, stmt):
        entity = stmt.column_descriptions[0]["entity"]
        if entity is Attachment:
            return _Scalars([self._attachment])
        if entity is AuditLog:
            return _Scalars([self._audit_row])
        if entity is User:
            return _Scalars([self._uploader])
        return _Scalars([])


def _uploader_user():
    return SimpleNamespace(
        user_id=55, first_name="Dana", last_name="Lee", email="dana@byu.edu"
    )


def _attachment():
    return SimpleNamespace(
        attachment_id=3,
        file_name="resume.pdf",
        file_type="application/pdf",
        attachment_notes=None,
        uploaded_at=datetime.datetime(2026, 6, 2, tzinfo=datetime.UTC),
        uploaded_by_user_id=55,
    )


def _audit_row_with_snapshot():
    # actor_name/actor_email are snapshotted at insert by a DB trigger and
    # survive the actor's deletion, so AuditEntryRead resolves performed_by from
    # them without a join.
    return SimpleNamespace(
        audit_log_id=7,
        action_type="update_interaction",
        field_name="interaction_notes",
        old_value="old",
        new_value="new",
        created_at=datetime.datetime(2026, 6, 3, tzinfo=datetime.UTC),
        user_id=99,
        actor_name="Sam Smith",
        actor_email="sam@byu.edu",
    )


def _audit_attachment_profile():
    session = _ProfileAuditAttachmentFakeSession(
        _alumni_model(),
        _audit_row_with_snapshot(),
        _attachment(),
        _uploader_user(),
    )
    return asyncio.run(
        profile_service.get_profile(session, 1, can_edit=True, actor_user_id=7)
    )


def test_audit_entry_resolves_performed_by_from_snapshot():
    profile = _audit_attachment_profile()
    assert len(profile.audit) == 1
    entry = profile.audit[0]
    # Display name resolved from the actor_name snapshot.
    assert entry.performed_by == "Sam Smith"
    # The raw internal user PK is gone from the schema entirely.
    assert not hasattr(entry, "user_id")
    assert "user_id" not in entry.model_dump()


def test_attachment_resolves_uploaded_by_name():
    profile = _audit_attachment_profile()
    assert len(profile.attachments) == 1
    att = profile.attachments[0]
    assert att.uploaded_by == "Dana Lee"
    # The raw uploader PK is gone from the schema entirely.
    assert not hasattr(att, "uploaded_by_user_id")
    assert "uploaded_by_user_id" not in att.model_dump()


def test_audit_attachment_pks_absent_from_serialized_profile():
    profile = _audit_attachment_profile()
    dumped = profile.model_dump(mode="json")
    assert "user_id" not in dumped["audit"][0]
    assert dumped["audit"][0]["performed_by"] == "Sam Smith"
    assert "uploaded_by_user_id" not in dumped["attachments"][0]
    assert dumped["attachments"][0]["uploaded_by"] == "Dana Lee"
