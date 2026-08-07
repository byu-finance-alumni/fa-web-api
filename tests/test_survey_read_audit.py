"""The two engineer-gated survey READS leave an audit trail (#422).

`GET /survey/campaigns/{grad_year}/held-out` names alumni and dates their
replies; `GET /survey/alumni/{alumni_id}/state` returns a named alumnus's whole
survey history. Both were correctly engineer-gated and neither wrote anything, so
a FERPA-relevant disclosure left no trace at all — out of step with `GET /audit`,
which self-logs its own read, and with `services.alumni.log_search` /
`log_preview`, which exist for exactly this reason on the alumni side.

Three questions are pinned here:

* does each read write ONE row, naming the action and the scope of the read
  (year / bucket / paging / alumni_id) and NOT the people it returned, and
* does the read still succeed when the audit write blows up (best effort — an
  audit outage must not become an outage of the engineer's only view of who is
  being held out), and
* WHERE does the row actually land? These routes are engineer-only, and the
  `before_flush` hook in `app/models/audit.py` reroutes an engineer's AuditLog
  into `engineer_action_log`. That is asserted against a REAL SQLAlchemy session,
  because the fake sessions the route tests use never flush, so the hook they
  depend on cannot be observed there.
"""

import asyncio
import datetime
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from app.api.dependencies import auth as auth_deps
from app.api.routes import survey as survey_routes
from app.core.audit_context import reset_audit_actor, set_audit_actor
from app.core.database import get_session
from app.core.errors import NotFoundError
from app.main import app
from app.models.audit import AuditLog  # registers the before_flush guard
from app.models.engineer_action import EngineerActionLog
from app.schemas.auth import UserContext
from app.schemas.survey import (
    SurveyAlumniState,
    SurveyHeldOutAlum,
    SurveyHeldOutPage,
)

_ENGINEER = UserContext(
    user_id=7,
    auth_user_id=uuid.UUID("11111111-1111-1111-1111-111111111111"),
    email="engineer@byu.edu",
    roles=["engineer"],
)

# The PII each read returns, which must never appear in the audit row.
_ALUM_NAME = "Marguerite Vanderhoof"
_REPLY_AT = datetime.datetime(2026, 5, 3, 17, 30, tzinfo=datetime.UTC)


class _AuditSession:
    """Records what the handler adds and commits. Nothing here flushes, so this
    session says what the route WROTE, not where it landed (see the reroute
    tests at the bottom for that)."""

    def __init__(self, *, commit_raises: bool = False):
        self.added: list = []
        self.commits = 0
        self.rollbacks = 0
        self._commit_raises = commit_raises

    def add(self, obj):
        self.added.append(obj)

    async def commit(self):
        self.commits += 1
        if self._commit_raises:
            raise RuntimeError("audit_logs is unavailable")

    async def rollback(self):
        self.rollbacks += 1

    @property
    def audits(self) -> list:
        return [a for a in self.added if isinstance(a, AuditLog)]


def _client(session):
    async def _override():
        yield session

    app.dependency_overrides[get_session] = _override
    # Override the capability guard itself: these tests are about what the
    # handler writes, and the gate has its own coverage in test_survey_held_out
    # / test_survey_reset (including that it is really attached to the route).
    app.dependency_overrides[auth_deps.require_engineer] = lambda: _ENGINEER
    return TestClient(app, raise_server_exceptions=False)


def _held_out_page() -> SurveyHeldOutPage:
    return SurveyHeldOutPage(
        graduation_year=2019,
        reason="already_responded",
        total=1,
        limit=200,
        offset=0,
        items=[
            SurveyHeldOutAlum(
                alumni_id=42,
                name=_ALUM_NAME,
                reason="already_responded",
                reason_label="Already replied within the last year",
                last_reply_at=_REPLY_AT,
            )
        ],
    )


def _alumni_state() -> SurveyAlumniState:
    return SurveyAlumniState(
        alumni_id=42,
        name=_ALUM_NAME,
        graduation_year=2019,
        email="marguerite@example.com",
        sends=[],
        responses=[],
        blocked_reasons=["Replied on 2026-05-03"],
    )


# ------------------------------------------------------- held-out ------------


def test_held_out_read_writes_one_audit_row(monkeypatch):
    async def _list_held_out(session, grad_year, *, reason, limit, offset):
        return _held_out_page()

    monkeypatch.setattr(survey_routes.survey_email, "list_held_out", _list_held_out)
    session = _AuditSession()
    try:
        with _client(session) as c:
            resp = c.get(
                "/survey/campaigns/2019/held-out",
                params={"reason": "already_responded", "limit": 50, "offset": 100},
            )
    finally:
        app.dependency_overrides.clear()

    assert resp.status_code == 200
    assert resp.json()["items"][0]["name"] == _ALUM_NAME  # the read still works
    (row,) = session.audits
    assert row.action_type == "read_survey_held_out"
    assert row.entity_type == "survey_campaign"
    assert row.entity_id == 2019
    assert row.user_id == 7
    assert session.commits == 1


def test_held_out_audit_records_the_scope_and_none_of_the_people(monkeypatch):
    """The row says WHICH slice was disclosed — year, bucket, page — so the read
    is accountable. It must not copy the names or reply dates it returned into a
    second table: that would duplicate the very PII the row exists to account
    for, which is exactly what `/audit`'s own self-log avoids."""

    async def _list_held_out(session, grad_year, *, reason, limit, offset):
        return _held_out_page()

    monkeypatch.setattr(survey_routes.survey_email, "list_held_out", _list_held_out)
    session = _AuditSession()
    try:
        with _client(session) as c:
            c.get(
                "/survey/campaigns/2019/held-out",
                params={"reason": "already_responded", "limit": 50, "offset": 100},
            )
    finally:
        app.dependency_overrides.clear()

    (row,) = session.audits
    assert "graduation_year=2019" in row.new_value
    assert "reason=already_responded" in row.new_value
    assert "limit=50" in row.new_value
    assert "offset=100" in row.new_value
    recorded = " ".join(
        str(v) for v in (row.new_value, row.old_value, row.field_name) if v
    )
    assert _ALUM_NAME not in recorded
    assert "Vanderhoof" not in recorded
    assert "2026-05-03" not in recorded


def test_held_out_audit_notes_when_no_bucket_was_filtered(monkeypatch):
    """An unfiltered read discloses all three buckets, so the row has to say so
    rather than leave `reason` simply absent — "everyone held out" is a wider
    disclosure than one bucket, and the trail should show which it was."""

    async def _list_held_out(session, grad_year, *, reason, limit, offset):
        return _held_out_page()

    monkeypatch.setattr(survey_routes.survey_email, "list_held_out", _list_held_out)
    session = _AuditSession()
    try:
        with _client(session) as c:
            c.get("/survey/campaigns/2019/held-out")
    finally:
        app.dependency_overrides.clear()

    (row,) = session.audits
    assert "reason=all" in row.new_value


def test_held_out_read_survives_a_failed_audit_write(monkeypatch):
    """Best effort, like `log_search`: the disclosure has already happened by the
    time the row is written, and failing the request would only deny the engineer
    the view without un-disclosing anything."""

    async def _list_held_out(session, grad_year, *, reason, limit, offset):
        return _held_out_page()

    monkeypatch.setattr(survey_routes.survey_email, "list_held_out", _list_held_out)
    session = _AuditSession(commit_raises=True)
    try:
        with _client(session) as c:
            resp = c.get("/survey/campaigns/2019/held-out")
    finally:
        app.dependency_overrides.clear()

    assert resp.status_code == 200
    assert resp.json()["total"] == 1
    assert session.rollbacks == 1


# ------------------------------------------------- per-alumnus state ---------


def test_alumni_state_read_writes_one_audit_row(monkeypatch):
    async def _get_state(session, alumni_id):
        return _alumni_state()

    monkeypatch.setattr(survey_routes.survey_reset, "get_state", _get_state)
    session = _AuditSession()
    try:
        with _client(session) as c:
            resp = c.get("/survey/alumni/42/state")
    finally:
        app.dependency_overrides.clear()

    assert resp.status_code == 200
    assert resp.json()["name"] == _ALUM_NAME
    (row,) = session.audits
    assert row.action_type == "read_survey_alumni_state"
    assert row.entity_type == "alumni"
    assert row.entity_id == 42
    assert row.user_id == 7
    # Scope IS the alumni_id here; nothing from the history is copied across.
    recorded = " ".join(
        str(v) for v in (row.new_value, row.old_value, row.field_name) if v
    )
    assert _ALUM_NAME not in recorded
    assert "marguerite@example.com" not in recorded


def test_alumni_state_read_survives_a_failed_audit_write(monkeypatch):
    async def _get_state(session, alumni_id):
        return _alumni_state()

    monkeypatch.setattr(survey_routes.survey_reset, "get_state", _get_state)
    session = _AuditSession(commit_raises=True)
    try:
        with _client(session) as c:
            resp = c.get("/survey/alumni/42/state")
    finally:
        app.dependency_overrides.clear()

    assert resp.status_code == 200
    assert resp.json()["alumni_id"] == 42
    assert session.rollbacks == 1


def test_a_read_that_failed_is_not_recorded_as_a_disclosure(monkeypatch):
    """The row is written AFTER the read, so a 404 for an alumnus who does not
    exist leaves nothing in the trail — there was no disclosure to account for."""

    async def _get_state(session, alumni_id):
        raise NotFoundError("Alumni 999 not found.")

    monkeypatch.setattr(survey_routes.survey_reset, "get_state", _get_state)
    session = _AuditSession()
    try:
        with _client(session) as c:
            resp = c.get("/survey/alumni/999/state")
    finally:
        app.dependency_overrides.clear()

    assert resp.status_code == 404
    assert session.audits == []


# ------------------------------------- where the row actually lands ----------


class _AsyncSessionShim:
    """The slice of AsyncSession `_log_survey_read` touches, backed by a REAL
    SQLAlchemy Session.

    The route tests above use a fake session that never flushes, so they can only
    show what was WRITTEN. The reroute in `app/models/audit.py` is a `before_flush`
    listener, so observing where an engineer's row ends up needs a session that
    actually flushes — this forwards to a synchronous one rather than pulling in
    an async SQLite driver the project does not otherwise depend on."""

    def __init__(self, session: Session):
        self._session = session

    def add(self, obj):
        self._session.add(obj)

    async def commit(self):
        self._session.commit()

    async def rollback(self):
        self._session.rollback()


@pytest.fixture(autouse=True)
def _reset_actor():
    reset_audit_actor()
    yield
    reset_audit_actor()


@pytest.fixture
def db():
    engine = create_engine("sqlite://")
    AuditLog.__table__.create(engine)
    EngineerActionLog.__table__.create(engine)
    with Session(engine) as s:
        yield s
    engine.dispose()


def _write_one(db, roles):
    set_audit_actor(roles)
    asyncio.run(
        survey_routes._log_survey_read(
            _AsyncSessionShim(db),
            actor_user_id=7,
            action="read_survey_held_out",
            entity_type="survey_campaign",
            entity_id=2019,
            scope="graduation_year=2019; reason=all; limit=200; offset=0",
        )
    )


def test_an_engineers_read_lands_in_the_engineer_action_log(db):
    """These routes are engineer-only, so this is where the row goes in practice:
    mirrored into the append-only `engineer_action_log` (no purge route,
    super_admin-only read) and dropped from `audit_logs`, keeping the FERPA
    record-change trail uncluttered without losing the disclosure."""
    _write_one(db, ["engineer"])

    assert db.scalar(select(func.count()).select_from(AuditLog)) == 0
    row = db.scalar(select(EngineerActionLog))
    assert row is not None
    assert row.actor_user_id == 7
    assert row.action_type == "read_survey_held_out"
    assert row.entity_type == "survey_campaign"
    assert row.entity_id == 2019
    assert "graduation_year=2019" in row.new_value


def test_a_non_engineer_actor_would_land_in_audit_logs(db):
    """Unreachable today (the `engineer` capability is non-assignable), but pinned
    so the row is never silently lost if that ever changes: without the reroute it
    is an ordinary audit_logs disclosure row, not nothing."""
    # The audit_logs PK is DB-generated in Postgres; SQLite will not autoincrement
    # a BigInteger PK, so the insert needs one supplied.
    set_audit_actor(["super_admin"])
    db.add(
        AuditLog(
            audit_log_id=1,
            user_id=7,
            action_type="read_survey_alumni_state",
            entity_type="alumni",
            entity_id=42,
        )
    )
    db.commit()

    assert db.scalar(select(func.count()).select_from(EngineerActionLog)) == 0
    assert db.scalar(select(func.count()).select_from(AuditLog)) == 1


def test_a_read_by_an_unknown_actor_writes_nothing(db):
    """No actor id, nothing to attribute — matches `log_search` / `log_preview`,
    which no-op rather than write an unattributable disclosure row."""
    set_audit_actor(["engineer"])
    asyncio.run(
        survey_routes._log_survey_read(
            _AsyncSessionShim(db),
            actor_user_id=None,
            action="read_survey_held_out",
            entity_type="survey_campaign",
            entity_id=2019,
        )
    )

    assert db.scalar(select(func.count()).select_from(AuditLog)) == 0
    assert db.scalar(select(func.count()).select_from(EngineerActionLog)) == 0
