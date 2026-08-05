"""Tests for the per-alumnus survey campaign reset (#395).

REWRITTEN 2026-08-05. The original suite asserted that a reset DELETED the
alum's responses, send-log rows and staged photos. That was the requirement then;
it is the opposite of the requirement now (Jake: "when you reset the campaign the
responses should not be reset, they should still be in the db"). The coverage is
the same set of questions — only one alumnus is affected, an audit row is
written, the guard is on the route — with the destruction assertions replaced by
preservation ones, plus the new question the redesign raises: does the reset
actually UNBLOCK, given nothing is removed?

Run against a real (in-memory SQLite) database rather than a canned fake session,
because a fake that returns whatever the test handed it cannot answer either of
the questions that matter here:

* is BOTH blocks lifted (either one alone still holds the person out — the exact
  trap that sent people back to psql), and
* is everything still there afterwards?

`survey_responses` is created from hand-written DDL because its `payload` column
is JSONB, which SQLite cannot render; TEXT round-trips through the same JSON
result processor, so the ORM still hands back a dict. The other tables come
straight from the models.
"""

import asyncio
import datetime
import json
import uuid

import pytest
from sqlalchemy import create_engine, func, select, text
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.api.dependencies import auth as auth_deps
from app.core.capabilities import DEFAULT_GRANTS
from app.core.errors import ConflictError, NotFoundError
from app.core.roles import RoleName
from app.core.security import AuthorizationError
from app.main import app
from app.models.alumni import Alumni
from app.models.audit import AuditLog
from app.models.contact import AlumniContactInfo
from app.models.survey_reset import SurveyResetLog
from app.models.survey_response import SurveyResponse
from app.models.survey_schedule import SurveySchedule, SurveySendLog
from app.schemas.auth import UserContext
from app.services import survey_email, survey_reset, survey_schedule

_NOW = datetime.datetime(2026, 8, 5, 12, 0, tzinfo=datetime.UTC)
_YEAR = 2019
_TARGET = 1
_BYSTANDER = 2


class _Session:
    """The async-session surface the services use, over a synchronous ORM one."""

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

    async def delete(self, obj):
        self._session.delete(obj)

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
    # Hand-written for the same reason as `survey_responses` below: the service
    # INSERTs here without supplying a key, and only SQLite's `INTEGER PRIMARY
    # KEY` is a rowid alias that auto-assigns one (a mapped `BigInteger` renders
    # as BIGINT and would fail NOT NULL).
    conn.execute(
        text(
            "CREATE TABLE survey_reset_log ("
            " survey_reset_id INTEGER PRIMARY KEY,"
            " alumni_id INTEGER NOT NULL,"
            " reset_seq INTEGER NOT NULL,"
            " reset_at TIMESTAMP NOT NULL,"
            " reset_by_user_id INTEGER,"
            " sends_superseded INTEGER NOT NULL DEFAULT 0,"
            " responses_superseded INTEGER NOT NULL DEFAULT 0,"
            " UNIQUE (alumni_id, reset_seq))"
        )
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

    def sent(self, alumni_id, stages, *, year=_YEAR, cycle=1, when=None, reset_seq=0):
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
                    "reset_seq": reset_seq,
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

    def reset_rows(self, alumni_id=None):
        stmt = select(SurveyResetLog)
        if alumni_id is not None:
            stmt = stmt.where(SurveyResetLog.alumni_id == alumni_id)
        return self.conn.scalars(stmt.order_by(SurveyResetLog.reset_seq)).all()

    def schedules(self):
        return self.conn.scalars(select(SurveySchedule)).all()

    def audits(self):
        return [a for a in self.session.added if isinstance(a, AuditLog)]

    def blocked_by_reply(self, alumni_id):
        """Does the SEND EXCLUSION itself still consider them a recent replier?

        The real predicate (`_replied_recently_exists`), not a re-derivation of
        it — the console and the sender disagreeing is the bug class this whole
        area keeps hitting."""
        return bool(
            self.conn.scalar(
                select(func.count())
                .select_from(Alumni)
                .where(
                    Alumni.alumni_id == alumni_id,
                    survey_email._replied_recently_exists(),
                )
            )
        )

    def logged_for(self, stage, *, year=_YEAR, cycle=1):
        """The double-send guard's answer: who is already emailed at this stage."""
        return asyncio.run(
            survey_email.logged_alumni_ids(self.session, year, stage, cycle)
        )

    def usage(self):
        """What the console's "Sent this month" tile reads — the REAL
        `get_send_usage` query run against these send-log rows.

        Deliberately not a canned fake session (which is all the send-service
        tests use): the question here is whether a reset changes the number of
        ROWS that query counts, which a stubbed result cannot answer."""
        return asyncio.run(survey_email.get_send_usage(self.session))

    # -- actions -------------------------------------------------------------

    def state(self, alumni_id=_TARGET):
        return asyncio.run(survey_reset.get_state(self.session, alumni_id))

    def reset(self, alumni_id=_TARGET, actor_user_id=99):
        return asyncio.run(
            survey_reset.reset_alumnus(
                self.session, alumni_id, actor_user_id=actor_user_id
            )
        )

    def delete_campaign(self, year=_YEAR, actor_user_id=99):
        return asyncio.run(
            survey_schedule.delete_schedule(
                self.session, year, actor_user_id=actor_user_id
            )
        )


@pytest.fixture
def no_usage_baseline(monkeypatch):
    """`get_send_usage` with no manual baseline configured (#544), so the meter
    is purely a count of send-log rows and the assertions are about those rows
    and nothing else."""

    class _S:
        survey_usage_baseline_at = None
        survey_usage_baseline_today = 0
        survey_usage_baseline_month = 0

    monkeypatch.setattr(survey_email, "get_settings", lambda: _S())


@pytest.fixture
def deleted_objects(monkeypatch):
    """Capture storage deletes. The reset must make NONE — a staged survey photo
    belongs to a response that is being kept, so removing the object would leave
    a preserved row pointing at nothing and a pending answer unreviewable."""
    seen = []

    async def _delete(bucket, path):
        seen.append((bucket, path))

    monkeypatch.setattr(
        "app.services.supabase_storage.delete_object", _delete
    )
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


# ------------------------------------------------ the reset destroys nothing ---


def test_reset_deletes_NOTHING(world, deleted_objects):
    """THE test, and the inverse of the one it replaces.

    Jake, 2026-08-05: "when you reset the campaign the responses should not be
    reset, they should still be in the db." Every row and every stored object
    survives — the reset's only output is one `survey_reset_log` row."""
    world.alum(_TARGET)
    world.schedule()
    world.sent(_TARGET, (0, 1, 2))
    world.replied(_TARGET, status="pending", photo="survey-pending/7")

    result = world.reset()

    assert len(world.send_rows(_TARGET)) == 3
    assert len(world.response_rows(_TARGET)) == 1
    assert deleted_objects == []
    assert world.response_rows(_TARGET)[0].staged_photo_path == "survey-pending/7"
    (event,) = world.reset_rows(_TARGET)
    assert (event.reset_seq, event.sends_superseded, event.responses_superseded) == (
        1,
        3,
        1,
    )
    assert (result.sends_superseded, result.responses_superseded) == (3, 1)
    assert (result.responses_preserved, result.pending_preserved) == (1, 1)


def test_reset_lifts_BOTH_blocks(world):
    """Preserving the rows is only half the job: either block alone still holds
    the person out, which is what sent someone back to psql a second time. Both
    are asserted through the REAL predicates the sender uses."""
    world.alum(_TARGET)
    world.schedule()
    world.sent(_TARGET, (0,))
    world.replied(_TARGET, status="applied")

    assert world.blocked_by_reply(_TARGET) is True
    assert world.logged_for(0) == {_TARGET}

    world.reset()

    # The reply is still in the table, but it no longer counts as a reply...
    assert len(world.response_rows(_TARGET)) == 1
    assert world.blocked_by_reply(_TARGET) is False
    # ...and the stage-0 email no longer claims them.
    assert world.logged_for(0) == set()


def test_a_reset_alumnus_can_be_claimed_again_for_the_same_stage(world):
    """The constraint half of the design. Ignoring the old send-log row in the
    READS is not enough — UNIQUE (year, alumni, stage, cycle) would refuse the
    new row, the claim's ON CONFLICT DO NOTHING would swallow it, and the alum
    would never actually be emailed however eligible the console called them."""
    world.alum(_TARGET)
    world.schedule()
    world.sent(_TARGET, (0,))

    world.reset()

    # The next send writes reset_seq=1 alongside the kept reset_seq=0 row.
    world.sent(_TARGET, (0,), reset_seq=1)

    rows = sorted(world.send_rows(_TARGET), key=lambda r: r.reset_seq)
    assert [r.reset_seq for r in rows] == [0, 1]
    # And now the NEW row is the one that blocks; the old one stays inert.
    assert world.logged_for(0) == {_TARGET}


# ------------------------------------ the reset must not move the meter -------
#
# Jake, 2026-08-05: "our 'sent this month' number is wrong, we should be at three
# not 2."
#
# The first version of #395 made an alumnus surveyable again by DELETING their
# `survey_send_log` rows -- every year, every cycle, unscoped. It was live on
# prod between the 14:03 and 15:12 deploys that day. `survey_email.get_send_usage`
# counts exactly those rows, so resetting ONE alumnus who had already been
# emailed this month silently dropped the console's "Sent this month" by one, and
# with it `survey_schedule._run_allowance`, which subtracts the same figure from
# the daily/monthly cap -- handing the scheduler a budget that had already been
# spent. That is the precise failure `get_send_usage`'s docstring says the send
# log exists to prevent, reintroduced from the other side: not an unrecorded
# send, an UNRECORDED-AFTERWARDS one.
#
# The rewrite deletes nothing, so the meter cannot move. These two tests hold the
# send log and the budget meter together, because nothing else did: every
# existing usage test runs against a canned session that returns whatever number
# it was handed, and so would have passed throughout the incident.


def test_a_reset_never_reduces_the_send_usage_meter(world, no_usage_baseline):
    """A reset changes who may be emailed NEXT. It cannot change what was
    already sent, because those emails were really sent and really cost budget."""
    now = datetime.datetime.now(datetime.UTC)
    for alumni_id in (_TARGET, _BYSTANDER, 3):
        world.alum(alumni_id)
        world.sent(alumni_id, (0,), when=now)
    world.schedule()

    before = world.usage()
    assert (before.sent_this_month, before.sent_today) == (3, 3)

    world.reset(_TARGET)

    after = world.usage()
    assert (after.sent_this_month, after.sent_today) == (3, 3)
    # The rows the meter counts are all still there — the reset added a row to
    # `survey_reset_log` and took nothing away.
    assert len(world.send_rows(_TARGET)) == 1
    assert len(world.reset_rows(_TARGET)) == 1


def test_a_reset_and_resend_costs_the_budget_two_emails(world, no_usage_baseline):
    """The other direction, and why the meter is deliberately NOT filtered by
    `send_not_superseded`: an alum who was reset and emailed again received two
    emails. Both were paid for, so both must show — hiding the pre-reset one
    would under-report the account's real Resend spend."""
    now = datetime.datetime.now(datetime.UTC)
    world.alum(_TARGET)
    world.schedule()
    world.sent(_TARGET, (0,), when=now)

    world.reset(_TARGET)
    # What `_claim_batch` writes for a reset alumnus on the next send.
    world.sent(_TARGET, (0,), when=now, reset_seq=1)

    usage = world.usage()
    assert usage.sent_this_month == 2
    assert usage.sent_today == 2


def test_reset_keeps_a_rejected_response_too(world):
    """`rejected` never blocked a send, and it is still that person's survey
    history (the profile Surveys tab derives from these rows). The old reset
    deleted it anyway; this one keeps every status."""
    world.alum(_TARGET)
    world.replied(_TARGET, status="rejected")
    world.replied(_TARGET, status="applied")

    result = world.reset()

    assert len(world.response_rows(_TARGET)) == 2
    assert result.responses_preserved == 2


def test_a_pending_response_stays_pending_and_reviewable(world):
    """The case the old behaviour got most wrong: an answer nobody has looked at
    yet was deleted unread. It now survives the reset untouched — same status,
    same photo — so it is still in the review queue and can still be applied."""
    world.alum(_TARGET)
    resp_id = world.replied(_TARGET, status="pending", photo="survey-pending/9")

    result = world.reset()

    (row,) = world.response_rows(_TARGET)
    assert (row.survey_response_id, row.status) == (resp_id, "pending")
    assert row.staged_photo_path == "survey-pending/9"
    assert row.reviewed_at is None
    assert result.pending_preserved == 1


def test_reset_touches_only_the_target_alumnus(world):
    """Scoped to exactly one person — never a cohort, never everyone in the
    year. A reset that swept the graduation year would silently re-open the
    whole cohort."""
    world.alum(_TARGET)
    world.alum(_BYSTANDER, first="Grace", last="Hopper")
    world.schedule()
    world.sent(_TARGET, (0, 1))
    world.sent(_BYSTANDER, (0, 1))
    world.replied(_TARGET)
    world.replied(_BYSTANDER)

    world.reset(_TARGET)

    # The bystander keeps every row AND stays blocked, which is the part that
    # matters: an over-broad reset would quietly re-email them.
    assert len(world.send_rows(_BYSTANDER)) == 2
    assert len(world.response_rows(_BYSTANDER)) == 1
    assert world.blocked_by_reply(_BYSTANDER) is True
    assert world.logged_for(0) == {_BYSTANDER}
    assert world.reset_rows() == list(world.reset_rows(_TARGET))
    # The cohort's campaign is per-YEAR state and must survive untouched: the
    # other 200 people in it are still mid-campaign.
    assert len(world.schedules()) == 1


def test_reset_covers_every_year_and_cycle_for_that_alumnus(world):
    """Not cycle-scoped. A leftover row from an older campaign can block a later
    one for exactly the reason the SQL was being run by hand."""
    world.alum(_TARGET)
    world.schedule(cycle=2)
    world.sent(_TARGET, (0, 1, 2), cycle=1)
    world.sent(_TARGET, (0,), cycle=2)

    result = world.reset()

    assert len(world.send_rows(_TARGET)) == 4
    assert result.sends_superseded == 4
    assert world.logged_for(0, cycle=1) == set()
    assert world.logged_for(0, cycle=2) == set()


def test_a_second_reset_supersedes_only_what_the_first_did_not(world):
    """Resets accumulate rather than repeat: the counts report what THIS action
    moved, so a second reset of an untouched alumnus reports zeros instead of
    re-claiming the first one's work."""
    world.alum(_TARGET)
    world.sent(_TARGET, (0,))
    world.replied(_TARGET)

    first = world.reset()
    second = world.reset()

    assert (first.reset_seq, second.reset_seq) == (1, 2)
    assert (first.sends_superseded, first.responses_superseded) == (1, 1)
    assert (second.sends_superseded, second.responses_superseded) == (0, 0)
    # Still nothing gone.
    assert len(world.send_rows(_TARGET)) == 1
    assert len(world.response_rows(_TARGET)) == 1


def test_reset_writes_an_audit_row_naming_who_what_and_how_much(world):
    """Audited: who reset whom, and what moved. The actor is an engineer, so the
    audit layer reroutes this row into `engineer_action_log` (#199) — it is
    written as an AuditLog either way, which is what this asserts."""
    world.alum(_TARGET)
    world.sent(_TARGET, (0, 1))
    world.replied(_TARGET)

    world.reset(actor_user_id=42)

    (entry,) = world.audits()
    assert entry.action_type == "reset_survey_campaign"
    assert entry.entity_type == "alumni"
    assert entry.entity_id == _TARGET
    assert entry.user_id == 42
    assert "sends=2" in entry.old_value
    assert "responses=1" in entry.old_value
    # The trail must not read as destruction, or the next person to read it will
    # believe answers were thrown away.
    assert "cleared" not in entry.new_value
    assert "kept 1 response" in entry.new_value


def test_reset_on_a_clean_alumnus_succeeds_and_reports_zeros(world):
    world.alum(_TARGET)
    result = world.reset()
    assert (result.sends_superseded, result.responses_superseded) == (0, 0)
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
    assert reply.superseded is False
    assert any("365-day" in r for r in state.blocked_reasons)


def test_state_after_a_reset_shows_the_history_but_nothing_blocking(world):
    """The screen has to agree with the sender. Everything is still listed —
    the rows are still there — but nothing is current and nothing blocks."""
    world.alum(_TARGET)
    world.schedule()
    world.sent(_TARGET, (0,))
    world.replied(_TARGET, status="applied")

    world.reset()
    state = world.state()

    assert state.reset_count == 1
    assert state.last_reset_at is not None
    (send,) = state.sends
    assert (send.superseded, send.current_cycle) == (True, False)
    (reply,) = state.responses
    assert (reply.superseded, reply.blocks_resend) == (True, False)
    assert state.blocked_reasons == []


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
    assert state.reset_count == 0


def test_state_of_an_unknown_alumnus_is_a_404(world):
    with pytest.raises(NotFoundError):
        world.state(4242)


# -------------------------------------------- deleting a campaign (#398) ------


def test_a_campaign_that_never_sent_anything_can_be_deleted(world):
    """The case the issue is about: a campaign scheduled against the wrong year.
    Nothing was emailed, so the row is the only thing that exists."""
    world.alum(_TARGET)
    world.schedule()

    result = world.delete_campaign()

    assert world.schedules() == []
    assert (result.graduation_year, result.previous_status) == (_YEAR, "active")
    assert result.responses_kept == 0


def test_deleting_a_campaign_keeps_the_responses_for_that_year(world):
    """"Delete campaign" will be read as "delete the answers too". It is not."""
    world.alum(_TARGET)
    world.schedule()
    world.replied(_TARGET, status="pending")

    result = world.delete_campaign()

    assert len(world.response_rows(_TARGET)) == 1
    assert result.responses_kept == 1


def test_a_campaign_that_has_emailed_anyone_cannot_be_deleted(world):
    """Cancel is the honest verb there, and the refusal is load-bearing, not
    squeamish: `survey_schedule` is the only holder of the year's `cycle_seq`, so
    dropping it would leave the send-log rows reading as the CURRENT cycle's and
    the next campaign for the year would skip everybody (#357)."""
    world.alum(_TARGET)
    world.schedule()
    world.sent(_TARGET, (0,))

    with pytest.raises(ConflictError) as exc:
        world.delete_campaign()

    assert "Cancel it instead" in str(exc.value)
    assert len(world.schedules()) == 1
    assert len(world.send_rows(_TARGET)) == 1


def test_deleting_an_unknown_campaign_is_a_404(world):
    assert world.delete_campaign(1999) is None


def test_deleting_a_campaign_is_audited_as_keeping_the_answers(world):
    world.alum(_TARGET)
    world.schedule()
    world.replied(_TARGET)

    world.delete_campaign(actor_user_id=42)

    (entry,) = world.audits()
    assert entry.action_type == "delete_survey_schedule"
    assert (entry.entity_type, entry.entity_id) == ("survey_campaign", _YEAR)
    assert entry.user_id == 42
    assert "responses_kept=1" in entry.new_value


def test_deleting_a_campaign_does_not_resurrect_a_superseded_response(world):
    """The two same-day features meeting. Supersession is decided from
    `survey_reset_log` alone — never from the campaign row — so removing the
    campaign must not hand the alum's old reply its blocking power back."""
    world.alum(_TARGET)
    world.schedule()
    world.replied(_TARGET, status="applied")

    world.reset()
    assert world.blocked_by_reply(_TARGET) is False

    world.delete_campaign()

    assert world.blocked_by_reply(_TARGET) is False
    assert len(world.response_rows(_TARGET)) == 1
    assert len(world.reset_rows(_TARGET)) == 1


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
    grant to another role. Not `surveys.manage`: that one IS assignable, and this
    button decides who receives a real email. Super admin is refused too."""
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
        # The campaign delete is a maintenance control too (#398).
        ("DELETE", "/survey/schedules/{grad_year}"),
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
