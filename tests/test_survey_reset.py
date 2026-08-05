"""Tests for the per-alumnus survey campaign reset (#395).

This feature replaces DELETE statements someone was typing into psql, so the
tests run the real statements against a real (in-memory SQLite) database rather
than a canned fake session. A fake that returns whatever the test handed it
cannot answer the only questions that matter here:

* did BOTH tables get cleared (clearing one leaves the person just as blocked —
  the exact trap that sent people back to SQL), and
* did the delete stay inside the one alumnus it was aimed at?

`survey_responses` is created from hand-written DDL because its `payload` column
is JSONB, which SQLite cannot render; TEXT round-trips through the same JSON
result processor, so the ORM still hands back a dict. The other four tables come
straight from the models.
"""

import asyncio
import datetime
import json
import uuid

import pytest
from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.api.dependencies import auth as auth_deps
from app.core.capabilities import DEFAULT_GRANTS
from app.core.errors import NotFoundError
from app.core.roles import RoleName
from app.core.security import AuthorizationError
from app.main import app
from app.models.alumni import Alumni
from app.models.audit import AuditLog
from app.models.contact import AlumniContactInfo
from app.models.survey_response import SurveyResponse
from app.models.survey_schedule import SurveySchedule, SurveySendLog
from app.schemas.auth import UserContext
from app.services import survey_reset

_NOW = datetime.datetime(2026, 8, 5, 12, 0, tzinfo=datetime.UTC)
_YEAR = 2019
_TARGET = 1
_BYSTANDER = 2


class _Session:
    """The async-session surface the service uses, over a synchronous ORM one.

    `execute` / `scalars` / `scalar` / `add` / `commit` — the reset needs all
    five, and `execute` must return a real Result so `.rowcount` on a DELETE is
    the genuine count the service reports and audits.
    """

    def __init__(self, session):
        self._session = session
        self.added = []

    async def execute(self, stmt):
        return self._session.execute(stmt)

    async def scalars(self, stmt):
        return self._session.scalars(stmt)

    async def scalar(self, stmt):
        return self._session.scalar(stmt)

    def add(self, obj):
        self.added.append(obj)
        # AuditLog is not mapped in this SQLite schema; keep it out of the flush
        # and just record it, which is what the assertions inspect.
        if not isinstance(obj, AuditLog):
            self._session.add(obj)

    async def commit(self):
        self._session.commit()


def _ddl(conn):
    from app.core.database import Base

    Base.metadata.create_all(
        conn,
        tables=[
            Alumni.__table__,
            AlumniContactInfo.__table__,
            SurveySchedule.__table__,
            SurveySendLog.__table__,
        ],
    )
    conn.execute(
        text(
            "CREATE TABLE survey_responses ("
            " survey_response_id INTEGER PRIMARY KEY,"
            " alumni_id INTEGER NOT NULL,"
            " graduation_year INTEGER,"
            " payload TEXT NOT NULL,"
            " status VARCHAR(20) NOT NULL,"
            " staged_photo_path VARCHAR(255),"
            " submitted_at TIMESTAMP NOT NULL,"
            " reviewed_by_user_id INTEGER,"
            " reviewed_at TIMESTAMP)"
        )
    )


class _World:
    """A small survey world to reset things in."""

    def __init__(self, conn):
        self.conn = conn
        self.session = _Session(conn)
        self._log_id = 0
        self._resp_id = 0

    # -- seeding -------------------------------------------------------------

    def alum(self, alumni_id, first="Ada", last="Lovelace", year=_YEAR, email=None):
        self.conn.execute(
            Alumni.__table__.insert(),
            [
                {
                    "alumni_id": alumni_id,
                    "first_name": first,
                    "last_name": last,
                    "graduation_year": year,
                    "archived": False,
                }
            ],
        )
        if email:
            self.conn.execute(
                AlumniContactInfo.__table__.insert(),
                [
                    {
                        "contact_info_id": alumni_id,
                        "alumni_id": alumni_id,
                        "personal_email": email,
                    }
                ],
            )

    def schedule(self, *, year=_YEAR, cycle=1, status="active"):
        self.conn.execute(
            SurveySchedule.__table__.insert(),
            [
                {
                    "survey_schedule_id": year,
                    "graduation_year": year,
                    "start_date": datetime.date(2026, 7, 1),
                    "status": status,
                    "cycle_seq": cycle,
                }
            ],
        )

    def sent(self, alumni_id, stages, *, year=_YEAR, cycle=1, when=None):
        rows = []
        for stage in stages:
            self._log_id += 1
            rows.append(
                {
                    "survey_send_log_id": self._log_id,
                    "graduation_year": year,
                    "alumni_id": alumni_id,
                    "stage": stage,
                    "cycle_seq": cycle,
                    "sent_at": when or _NOW,
                }
            )
        self.conn.execute(SurveySendLog.__table__.insert(), rows)

    def replied(
        self, alumni_id, *, status="pending", when=None, fields=1, photo=None
    ):
        self._resp_id += 1
        self.conn.execute(
            text(
                "INSERT INTO survey_responses (survey_response_id, alumni_id,"
                " graduation_year, payload, status, staged_photo_path,"
                " submitted_at) VALUES (:i, :a, :y, :p, :s, :ph, :t)"
            ),
            {
                "i": self._resp_id,
                "a": alumni_id,
                "y": _YEAR,
                "p": json.dumps({f"f{n}": "v" for n in range(fields)}),
                "s": status,
                "ph": photo,
                "t": when or _NOW,
            },
        )
        return self._resp_id

    # -- reads ---------------------------------------------------------------

    def send_rows(self, alumni_id):
        return self.conn.scalars(
            select(SurveySendLog).where(SurveySendLog.alumni_id == alumni_id)
        ).all()

    def response_rows(self, alumni_id):
        return self.conn.scalars(
            select(SurveyResponse).where(SurveyResponse.alumni_id == alumni_id)
        ).all()

    def schedules(self):
        return self.conn.scalars(select(SurveySchedule)).all()

    def audits(self):
        return [a for a in self.session.added if isinstance(a, AuditLog)]

    # -- actions -------------------------------------------------------------

    def state(self, alumni_id=_TARGET):
        return asyncio.run(survey_reset.get_state(self.session, alumni_id))

    def reset(self, alumni_id=_TARGET, actor_user_id=99):
        return asyncio.run(
            survey_reset.reset_alumnus(
                self.session, alumni_id, actor_user_id=actor_user_id
            )
        )


@pytest.fixture
def deleted_objects(monkeypatch):
    """Capture storage deletes — a staged survey photo lives in the headshots
    bucket, not the database, so a reset that only touched rows would orphan it.
    """
    seen = []

    async def _delete(bucket, path):
        seen.append((bucket, path))

    monkeypatch.setattr(survey_reset.supabase_storage, "delete_object", _delete)
    return seen


@pytest.fixture
def world(deleted_objects):
    # StaticPool: every checkout is the SAME connection, so the schema created
    # here is still there for the session below.
    engine = create_engine(
        "sqlite://",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    with engine.begin() as conn:
        _ddl(conn)
    with Session(engine) as session:
        yield _World(session)
    engine.dispose()


# ----------------------------------------------------- the reset itself -------


def test_reset_clears_BOTH_tables(world):
    """THE test. Either table alone still blocks the person.

    `survey_send_log` stops a repeat send inside the cycle; `survey_responses`
    holds the 365-day window open. Clearing one and not the other is the failure
    that had someone back in psql a second time.
    """
    world.alum(_TARGET)
    world.schedule()
    world.sent(_TARGET, (0, 1, 2))
    world.replied(_TARGET, status="pending")

    result = world.reset()

    assert world.send_rows(_TARGET) == []
    assert world.response_rows(_TARGET) == []
    assert (result.sends_deleted, result.responses_deleted) == (3, 1)


def test_reset_clears_a_rejected_response_too(world):
    """`rejected` does not block a send — but it is still that person's survey
    history (the profile Surveys tab derives from these rows), so a reset that
    left it behind would not be a reset."""
    world.alum(_TARGET)
    world.replied(_TARGET, status="rejected")
    world.replied(_TARGET, status="applied")

    result = world.reset()

    assert world.response_rows(_TARGET) == []
    assert result.responses_deleted == 2


def test_reset_touches_only_the_target_alumnus(world):
    """Scoped to exactly one person — never a cohort, never everyone in the
    year. A reset that swept the graduation year would silently re-open the
    whole cohort and destroy every reply in it."""
    world.alum(_TARGET)
    world.alum(_BYSTANDER, first="Grace", last="Hopper")
    world.schedule()
    world.sent(_TARGET, (0, 1))
    world.sent(_BYSTANDER, (0, 1))
    world.replied(_TARGET)
    world.replied(_BYSTANDER)

    world.reset(_TARGET)

    assert len(world.send_rows(_BYSTANDER)) == 2
    assert len(world.response_rows(_BYSTANDER)) == 1
    # The cohort's campaign is per-YEAR state and must survive untouched: the
    # other 200 people in it are still mid-campaign.
    assert len(world.schedules()) == 1


def test_reset_clears_every_year_and_cycle_for_that_alumnus(world):
    """Not cycle-scoped. A leftover row from an older campaign can block a later
    one for exactly the reason the SQL was being run by hand."""
    world.alum(_TARGET)
    world.schedule(cycle=2)
    world.sent(_TARGET, (0, 1, 2), cycle=1)
    world.sent(_TARGET, (0,), cycle=2)

    result = world.reset()

    assert world.send_rows(_TARGET) == []
    assert result.sends_deleted == 4


def test_reset_writes_an_audit_row_naming_who_what_and_how_much(world):
    """Audited: who reset whom, and what was removed. The actor is an engineer,
    so the audit layer reroutes this row into `engineer_action_log` (#199) — it
    is written as an AuditLog either way, which is what this asserts."""
    world.alum(_TARGET)
    world.sent(_TARGET, (0, 1))
    world.replied(_TARGET)

    world.reset(actor_user_id=42)

    (entry,) = world.audits()
    assert entry.action_type == "reset_survey_campaign"
    assert entry.entity_type == "alumni"
    assert entry.entity_id == _TARGET
    assert entry.user_id == 42
    # Counts, not a bare "reset" — the trail has to answer "what did that do?".
    assert "sends=2" in entry.old_value
    assert "responses=1" in entry.old_value


def test_reset_removes_a_staged_photo_from_storage(world, deleted_objects):
    """A pending response can carry an uploaded photo in the headshots bucket.
    Deleting the row without the object leaves an image nothing points at."""
    world.alum(_TARGET)
    world.replied(_TARGET, photo="survey-pending/7")
    world.replied(_TARGET, photo=None)

    result = world.reset()

    assert deleted_objects == [("headshots", "survey-pending/7")]
    assert result.staged_photos_deleted == 1


def test_reset_on_a_clean_alumnus_succeeds_and_reports_zeros(world):
    world.alum(_TARGET)
    result = world.reset()
    assert (result.sends_deleted, result.responses_deleted) == (0, 0)
    assert result.name == "Ada Lovelace"


def test_reset_of_an_unknown_alumnus_is_a_404(world):
    with pytest.raises(NotFoundError):
        world.reset(4242)


# ------------------------------------------- the state shown BEFORE resetting --


def test_state_reports_a_recent_reply_as_the_thing_blocking_them(world):
    """The operator's real question: is a reset even the right move? Usually it
    is not — someone looks blocked because they legitimately answered."""
    world.alum(_TARGET, email="ada@example.com")
    world.schedule()
    recent = _NOW - datetime.timedelta(days=90)
    world.replied(_TARGET, status="applied", when=recent, fields=4)

    state = world.state()

    assert state.name == "Ada Lovelace"
    assert state.email == "ada@example.com"
    assert state.graduation_year == _YEAR
    assert state.schedule_status == "active"
    (reply,) = state.responses
    assert (reply.status, reply.field_count, reply.blocks_resend) == ("applied", 4, True)
    assert any("365-day" in r for r in state.blocked_reasons)


def test_state_does_not_call_a_rejected_reply_blocking(world):
    """Matches the send exclusion exactly (`RESPONDED_STATUSES`): staff threw
    that submission away, so the alum is already surveyable and a reset would
    unblock nothing."""
    world.alum(_TARGET)
    world.replied(_TARGET, status="rejected")

    state = world.state()

    assert state.responses[0].blocks_resend is False
    assert state.blocked_reasons == []


def test_state_does_not_call_an_out_of_window_reply_blocking(world):
    world.alum(_TARGET)
    world.replied(_TARGET, status="applied", when=_NOW - datetime.timedelta(days=800))

    state = world.state()

    assert state.responses[0].blocks_resend is False
    assert state.blocked_reasons == []


def test_state_separates_a_previous_cycles_sends_from_the_current_one(world):
    """A long-standing alumnus has send-log rows from every campaign they have
    ever been in. Only the CURRENT cycle's rows can block a send — reporting all
    of them as blocking would make everyone look stuck."""
    world.alum(_TARGET)
    world.schedule(cycle=2)
    world.sent(_TARGET, (0, 1, 2), cycle=1, when=_NOW - datetime.timedelta(days=400))
    world.sent(_TARGET, (0,), cycle=2)

    state = world.state()

    assert [s.current_cycle for s in state.sends] == [False, False, False, True]
    assert [s.stage_label for s in state.sends][-1] == "Initial email"
    (reason,) = state.blocked_reasons
    assert "current campaign" in reason and "initial email" in reason


def test_state_of_an_untouched_alumnus_has_nothing_blocking(world):
    world.alum(_TARGET)
    state = world.state()
    assert (state.sends, state.responses, state.blocked_reasons) == ([], [], [])


def test_state_of_an_unknown_alumnus_is_a_404(world):
    with pytest.raises(NotFoundError):
        world.state(4242)


# ------------------------------------------------------------ the guard -------


def _ctx(*roles: str) -> UserContext:
    return UserContext(
        user_id=1,
        auth_user_id=uuid.UUID("11111111-1111-1111-1111-111111111111"),
        email="worker@byu.edu",
        roles=list(roles),
    )


@pytest.mark.parametrize(
    "role",
    [
        RoleName.SUPER_ADMIN.value,
        RoleName.FULL_ACCESS.value,
        RoleName.STUDENT.value,
        RoleName.VIEW_ONLY.value,
    ],
)
def test_only_an_engineer_passes_the_reset_guard(role):
    """Gated on the `engineer` capability, which the permission editor cannot
    grant to another role. Not `surveys.manage`: that one IS assignable, so
    gating on it would let permanent destruction of alumni submissions be handed
    to whoever needs to review responses. Super admin is refused too."""
    with pytest.raises(AuthorizationError):
        asyncio.run(auth_deps.require_engineer(_ctx(role), dict(DEFAULT_GRANTS)))
    engineer = _ctx(RoleName.ENGINEER.value)
    assert (
        asyncio.run(auth_deps.require_engineer(engineer, dict(DEFAULT_GRANTS)))
        is engineer
    )


def _all_routes(router):
    """Every real route, flattened.

    FastAPI wraps each `include_router` call in an `_IncludedRouter` container
    whose endpoints hang off `original_router`, so `app.routes` is not a flat
    list and a naive scan finds none of the survey routes at all."""
    for route in getattr(router, "routes", []):
        inner = getattr(route, "original_router", None)
        if inner is not None:
            yield from _all_routes(inner)
        elif hasattr(route, "routes"):
            yield from _all_routes(route)
        else:
            yield route


@pytest.mark.parametrize(
    "method,path",
    [
        ("POST", "/survey/alumni/{alumni_id}/reset"),
        ("GET", "/survey/alumni/{alumni_id}/state"),
    ],
)
def test_the_routes_are_actually_wired_to_that_guard(method, path):
    """A guard that isn't attached to the route protects nothing, so pin the
    wiring rather than only the function."""
    route = next(
        r
        for r in _all_routes(app)
        if getattr(r, "path", None) == path and method in getattr(r, "methods", set())
    )
    guards = {d.call for d in route.dependant.dependencies}
    # `require_engineer` is reached through the sub-dependency tree of the
    # RequireEngineer annotation, so walk it.
    stack = list(route.dependant.dependencies)
    while stack:
        dep = stack.pop()
        guards.add(dep.call)
        stack.extend(dep.dependencies)
    assert auth_deps.require_engineer in guards
