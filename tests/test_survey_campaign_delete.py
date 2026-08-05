"""Delete ANY campaign, then run the year again and actually reach people (#398).

Jake, 2026-08-05: *"it still won't let me delete a campaign in the engineer
dashboard."* The morning's delete refused any campaign that had ever emailed
anyone — which is every real one — and offered `cancel` instead; an
already-cancelled campaign got no control at all. Offered the options, he chose:
delete any campaign, KEEP the emails.

WHY THIS FILE EXISTS SEPARATELY FROM THE DELETE'S OWN TESTS
-----------------------------------------------------------
The refusal was protecting something real. `survey_schedule` is the sole holder
of a graduation year's `cycle_seq`, and `survey_send_log` is scoped by it, so
deleting the row naively leaves the old rows reading as the CURRENT cycle's — the
next campaign for that year finds everyone already emailed and sends to NOBODY
(#357, which this codebase has already paid for once). Every symptom of that bug
is silent: no error, no exception, a campaign that completes with `sent=0` and a
console that says it ran.

So a test that only checks the schedule row vanished would pass while the feature
is broken. These run the whole journey against a real (in-memory SQLite)
database — delete a campaign that has sent, create a new one for the same year,
then load the recipients, select the stage and CLAIM them with the very same
`_claim_batch` the sender uses, against the very same UNIQUE constraint.

Both halves are tested because they fail independently and both fail quietly:

* the READ half — `logged_alumni_ids` is cycle-scoped, so a new campaign in the
  retired cycle sees everyone as already emailed and selects no targets;
* the CLAIM half — UNIQUE (graduation_year, alumni_id, stage, cycle_seq,
  reset_seq) refuses a row in the retired cycle, and `_claim_batch`'s
  ON CONFLICT DO NOTHING swallows the refusal, so the recipient is dropped from
  the batch while the console still calls them eligible. That is exactly the trap
  `reset_seq` was added to the key for on the same day (#395), one level up.

`test_claiming_in_the_retired_cycle_is_silently_swallowed` is the negative
control: it shows the constraint really is live in this database and really does
fail without a sound, so the passing assertions above it are not vacuous.

The eligibility SQL uses Postgres string functions (`btrim` / `strpos` /
`split_part`) that SQLite lacks; they are registered as UDFs with Postgres
semantics so the production expression tree is under test end to end, exactly as
in `test_survey_email_reach`.
"""

import asyncio
import datetime

import pytest
from sqlalchemy import BigInteger, create_engine, event, func, select, text
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.core.database import Base
from app.models.alumni import Alumni
from app.models.audit import AuditLog
from app.models.contact import AlumniContactInfo
from app.models.employment import CurrentEmployment
from app.models.survey_retirement import SurveyCampaignRetirement
from app.models.survey_schedule import SurveySchedule, SurveySendLog
from app.models.tags import AlumniStatusLabel, StatusLabel
from app.services import survey_email, survey_schedule

_YEAR = 2019
_START = datetime.date(2026, 7, 1)
# Two alumni the deleted campaign emailed and who never replied — the population
# the whole feature is about.
_ANN = 1
_BEN = 2


# --------------------------------------------------------------- harness -----


@compiles(BigInteger, "sqlite")
def _bigint_is_integer_on_sqlite(type_, compiler, **kw) -> str:
    """Render ``BigInteger`` as SQLite ``INTEGER`` so identity PKs autoincrement.

    SQLite only treats ``INTEGER PRIMARY KEY`` as a rowid alias; a ``BIGINT`` one
    does not autoincrement, so an insert without an explicit id fails. Both the
    schedule create and ``_claim_batch`` insert rows whose ids Postgres
    generates, so this makes the test double behave the way production does.
    SQLite-dialect only. (Same shim as ``test_involvement_tags``.)"""
    return "INTEGER"


def _pg_split_part(value, delim, index):
    if value is None:
        return None
    parts = value.split(delim)
    return parts[index - 1] if 0 < index <= len(parts) else ""


def _register_pg_functions(dbapi_conn, _record):
    """Postgres string functions, with Postgres semantics, for SQLite."""
    dbapi_conn.create_function("btrim", 1, lambda v: None if v is None else v.strip())
    dbapi_conn.create_function("strpos", 2, lambda h, n: 0 if h is None else h.find(n) + 1)
    dbapi_conn.create_function("split_part", 3, _pg_split_part)


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
        # `audit_log` is not mapped into this SQLite schema; record it for the
        # assertions and keep it out of the flush.
        if not isinstance(obj, AuditLog):
            self._session.add(obj)

    async def delete(self, obj):
        self._session.delete(obj)

    async def commit(self):
        self._session.commit()


def _ddl(conn):
    Base.metadata.create_all(
        conn,
        tables=[
            Alumni.__table__,
            AlumniContactInfo.__table__,
            AlumniStatusLabel.__table__,
            StatusLabel.__table__,
            # `_load_recipients` bulk-loads employment for the email's
            # "here's what we have on file" block.
            CurrentEmployment.__table__,
            SurveySchedule.__table__,
            # Created from the model so the REAL unique constraint — the thing
            # that silently refuses a claim in a retired cycle — is in force.
            SurveySendLog.__table__,
            SurveyCampaignRetirement.__table__,
        ],
    )
    # Hand-written: `payload` is JSONB, which SQLite cannot render, and the
    # services INSERT without supplying a key (only SQLite's INTEGER PRIMARY KEY
    # auto-assigns one).
    conn.execute(
        text(
            "CREATE TABLE survey_responses ("
            " survey_response_id INTEGER PRIMARY KEY,"
            " alumni_id INTEGER NOT NULL,"
            " graduation_year INTEGER,"
            " payload TEXT NOT NULL DEFAULT '{}',"
            " status VARCHAR(20) NOT NULL,"
            " staged_photo_path VARCHAR(255),"
            " submitted_at TIMESTAMP NOT NULL,"
            " reviewed_by_user_id INTEGER,"
            " reviewed_at TIMESTAMP)"
        )
    )
    conn.execute(
        text(
            "CREATE TABLE survey_reset_log ("
            " survey_reset_id INTEGER PRIMARY KEY,"
            " alumni_id INTEGER NOT NULL,"
            " reset_seq INTEGER NOT NULL,"
            " reset_at TIMESTAMP NOT NULL,"
            " reset_by_user_id INTEGER,"
            " sends_superseded INTEGER NOT NULL DEFAULT 0,"
            " responses_superseded INTEGER NOT NULL DEFAULT 0)"
        )
    )


class _World:
    """One graduation year, its campaign, and the people it emails."""

    def __init__(self, conn):
        # `conn` is a plain ORM Session used for seeding and assertions;
        # `self.session` is the async-shaped facade the services are given.
        self.conn = conn
        self.session = _Session(conn)
        self._log_id = 0
        self._resp_id = 0

    # -- seeding -------------------------------------------------------------

    def alum(self, alumni_id, first="Ada", last="Lovelace", email=None):
        self.conn.execute(
            Alumni.__table__.insert(),
            [
                {
                    "alumni_id": alumni_id,
                    "first_name": first,
                    "last_name": last,
                    "graduation_year": _YEAR,
                    "is_alumni": True,
                    "archived": False,
                    "deceased": False,
                }
            ],
        )
        self.conn.execute(
            AlumniContactInfo.__table__.insert(),
            [
                {
                    "contact_info_id": alumni_id,
                    "alumni_id": alumni_id,
                    "personal_email": email or f"alum{alumni_id}@byu.edu",
                }
            ],
        )
        self.conn.commit()

    def cohort(self):
        """The two alumni every test starts from."""
        self.alum(_ANN, first="Ann")
        self.alum(_BEN, first="Ben", last="Hopper")

    def schedule(self, *, cycle=1, status="active"):
        self.conn.execute(
            SurveySchedule.__table__.insert(),
            [
                {
                    "survey_schedule_id": _YEAR * 10 + cycle,
                    "graduation_year": _YEAR,
                    "start_date": _START,
                    "status": status,
                    "cycle_seq": cycle,
                }
            ],
        )
        self.conn.commit()

    def sent(self, alumni_id, stages, *, cycle=1, reset_seq=0):
        """Send-log rows, as the sender would have written them."""
        rows = []
        for stage in stages:
            self._log_id += 1
            rows.append(
                {
                    "survey_send_log_id": self._log_id,
                    "graduation_year": _YEAR,
                    "alumni_id": alumni_id,
                    "stage": stage,
                    "cycle_seq": cycle,
                    "reset_seq": reset_seq,
                    "sent_at": datetime.datetime.now(datetime.UTC),
                }
            )
        self.conn.execute(SurveySendLog.__table__.insert(), rows)
        self.conn.commit()

    def replied(self, alumni_id, *, status="applied", days_ago=1):
        self._resp_id += 1
        self.conn.execute(
            text(
                "INSERT INTO survey_responses (survey_response_id, alumni_id,"
                " graduation_year, payload, status, submitted_at)"
                " VALUES (:i, :a, :y, '{}', :s, :t)"
            ),
            {
                "i": self._resp_id,
                "a": alumni_id,
                "y": _YEAR,
                "s": status,
                "t": datetime.datetime.now(datetime.UTC)
                - datetime.timedelta(days=days_ago),
            },
        )
        self.conn.commit()

    # -- actions -------------------------------------------------------------

    def delete_campaign(self, actor_user_id=99):
        return asyncio.run(
            survey_schedule.delete_schedule(
                self.session, _YEAR, actor_user_id=actor_user_id
            )
        )

    def create_campaign(self, start_date=_START):
        """What the console does next: schedule the year again."""
        return asyncio.run(
            survey_schedule.create_schedule(
                self.session,
                graduation_year=_YEAR,
                start_date=start_date,
                actor_user_id=None,
            )
        )

    def recipients(self):
        """Who the send would load — the real cohort query, deduped."""
        loaded = asyncio.run(
            survey_email._load_recipients(self.session, _YEAR)
        )
        kept, _dropped = survey_email.dedupe_by_email(loaded)
        return kept

    def targets(self, *, cycle, max_stage=0):
        """The stage the sender would send, and to whom."""
        return asyncio.run(
            survey_email.select_stage_targets(
                self.session,
                graduation_year=_YEAR,
                recipients=self.recipients(),
                max_stage=max_stage,
                cycle_seq=cycle,
            )
        )

    def claim(self, *, cycle, stage=0, recipients=None):
        """The irreversible half: reserve the batch in `survey_send_log`.

        Returns the recipients actually claimed — the ones that would really be
        emailed. An empty list where the console promised recipients IS the
        silent failure this whole feature turns on."""
        batch = self.recipients() if recipients is None else recipients
        claimed = asyncio.run(
            survey_email._claim_batch(
                self.session,
                graduation_year=_YEAR,
                stage=stage,
                cycle_seq=cycle,
                batch=batch,
            )
        )
        return sorted(r.alumni_id for r in claimed)

    # -- reads ---------------------------------------------------------------

    def current_cycle(self):
        return asyncio.run(survey_email.current_cycle_seq(self.session, _YEAR))

    def logged(self, *, cycle, stage=0):
        return asyncio.run(
            survey_email.logged_alumni_ids(self.session, _YEAR, stage, cycle)
        )

    def schedules(self):
        return self.conn.scalars(select(SurveySchedule)).all()

    def send_rows(self):
        return self.conn.scalars(
            select(SurveySendLog).order_by(SurveySendLog.survey_send_log_id)
        ).all()

    def send_row_count(self):
        return int(
            self.conn.scalar(select(func.count()).select_from(SurveySendLog)) or 0
        )

    def retirements(self):
        return self.conn.scalars(
            select(SurveyCampaignRetirement).order_by(
                SurveyCampaignRetirement.cycle_seq
            )
        ).all()


@pytest.fixture
def world():
    # StaticPool: every checkout is the SAME connection, so the schema created
    # here is still there for the session below.
    engine = create_engine(
        "sqlite://",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    event.listen(engine, "connect", _register_pg_functions)
    with engine.begin() as conn:
        _ddl(conn)
    with Session(engine) as session:
        yield _World(session)
    engine.dispose()


# ------------------------------------------------------------- THE test -------


def test_the_next_campaign_reaches_everyone_the_deleted_one_emailed(world):
    """The whole point, end to end.

    A campaign that emailed both alumni is deleted; a new campaign for the same
    year is created; and the two of them are loaded, targeted and CLAIMED — with
    the real send-log constraint in force. Any one of those four steps failing is
    the feature being broken, and none of them raises when it does."""
    world.cohort()
    world.schedule()
    world.sent(_ANN, (0,))
    world.sent(_BEN, (0,))
    # Before: the campaign's own guard holds them both out, correctly.
    assert world.logged(cycle=1) == {_ANN, _BEN}

    result = world.delete_campaign()
    assert world.schedules() == []
    assert (result.retired_cycle, result.next_cycle) == (1, 2)
    assert result.emails_retired == 2

    item = world.create_campaign()

    # 1. The new campaign is a cycle ABOVE the retired sends...
    assert item.cycle_seq == 2
    assert world.current_cycle() == 2
    # 2. ...so the double-send guard no longer sees them...
    assert world.logged(cycle=2) == set()
    # 3. ...the sender selects both for the initial email...
    stage, targets = world.targets(cycle=2)
    assert stage == survey_email.STAGE_INITIAL
    assert sorted(r.alumni_id for r in targets) == [_ANN, _BEN]
    # 4. ...and the claim actually reserves them, rather than being swallowed by
    #    ON CONFLICT DO NOTHING against the retired rows. THIS is the assertion
    #    that separates a working feature from a silently broken one.
    assert world.claim(cycle=2) == [_ANN, _BEN]

    # And the retired rows are still there, untouched, beside the new ones.
    assert world.send_row_count() == 4
    assert sorted((r.cycle_seq, r.alumni_id) for r in world.send_rows()) == [
        (1, _ANN),
        (1, _BEN),
        (2, _ANN),
        (2, _BEN),
    ]


def test_claiming_in_the_retired_cycle_is_silently_swallowed(world):
    """The negative control, and the reason the assertion above is worth making.

    Claim the same people in the cycle the delete retired and the unique
    constraint refuses every row — but `_claim_batch` uses ON CONFLICT DO
    NOTHING, so nothing raises, nobody is returned, and a campaign wired this way
    would report a clean run having emailed no one. That is the #357 failure
    mode, reproduced here on purpose so the passing test cannot be vacuous."""
    world.cohort()
    world.schedule()
    world.sent(_ANN, (0,))
    world.sent(_BEN, (0,))
    world.delete_campaign()

    # No exception, no claim, no email — exactly what makes this bug class so
    # expensive to find in production.
    assert world.claim(cycle=1) == []
    assert world.send_row_count() == 2


def test_the_console_and_the_sender_agree_after_a_delete(world):
    """`get_schedule`'s counters read the campaign that exists NOW.

    The retired cycle's sends belonged to a campaign that is gone, so the new
    one must not open showing two emails already sent — the disagreement between
    what the console reports and what the send actually did is the standing bug
    class in this area."""
    world.cohort()
    world.schedule()
    world.sent(_ANN, (0,))
    world.sent(_BEN, (0,))
    world.delete_campaign()

    item = world.create_campaign()

    assert item.sent_initial == 0
    assert item.non_responders == 0
    # ...while the all-time figure still counts them, because those emails were
    # really sent and really cost Resend budget.
    assert item.emails_sent_all_time == 2


def test_a_manual_send_after_a_delete_lands_in_the_fresh_cycle(world):
    """The console's manual send for a year with NO schedule row resolves its own
    cycle. Left at 1 it would land on top of the retired rows and claim nobody —
    the same silent failure by a different door, since a deleted campaign is
    precisely a year with no schedule."""
    world.cohort()
    world.schedule()
    world.sent(_ANN, (0,))
    world.delete_campaign()

    assert world.schedules() == []
    # What `send_survey_stage` resolves when the caller passes no cycle.
    assert world.current_cycle() == 2
    assert world.claim(cycle=world.current_cycle()) == [_ANN, _BEN]


def test_repeated_manual_sends_for_that_year_stay_in_one_cycle(world):
    """The other side of it: the fresh cycle must be STABLE. If it moved every
    time it was resolved, a second manual send would re-email the people the
    first one just reached."""
    world.cohort()
    world.schedule()
    world.delete_campaign()

    first = world.current_cycle()
    assert world.claim(cycle=first) == [_ANN, _BEN]
    assert world.current_cycle() == first
    # The second send finds them already logged for this stage, as it should.
    assert world.claim(cycle=world.current_cycle()) == []
    assert world.send_row_count() == 2


def test_deleting_the_replacement_campaign_climbs_again(world):
    """Delete is not a one-shot. Each deletion retires its own cycle, so the
    year keeps climbing and no campaign ever lands on a retired one."""
    world.cohort()
    world.schedule()
    world.sent(_ANN, (0,))
    world.delete_campaign()

    second = world.create_campaign()
    assert second.cycle_seq == 2
    world.sent(_ANN, (0,), cycle=2)

    result = world.delete_campaign()
    assert (result.retired_cycle, result.next_cycle) == (2, 3)

    third = world.create_campaign()
    assert third.cycle_seq == 3
    assert [(r.graduation_year, r.cycle_seq) for r in world.retirements()] == [
        (_YEAR, 1),
        (_YEAR, 2),
    ]
    # Both earlier campaigns' emails are still on file.
    assert world.send_row_count() == 2
    assert world.claim(cycle=3) == [_ANN, _BEN]


def test_an_alum_who_replied_is_still_held_by_the_annual_window(world):
    """The deliberate limit, stated as a test so nobody "fixes" it later.

    Deleting a campaign retires its EMAILS. It does not retire an alum's ANSWER:
    someone who replied inside the 365-day re-survey window stays out of the next
    campaign, exactly as they would after `start_new_cycle`. Re-asking one person
    who has already answered is the per-alumnus reset (#395), which is a separate,
    deliberate act."""
    world.cohort()
    world.schedule()
    world.sent(_ANN, (0,))
    world.sent(_BEN, (0,))
    world.replied(_BEN, status="applied", days_ago=3)

    world.delete_campaign()
    world.create_campaign()

    # Ann is reachable again; Ben is not, and his answer is why.
    assert sorted(r.alumni_id for r in world.recipients()) == [_ANN]
    assert world.claim(cycle=2) == [_ANN]


def test_a_year_that_never_had_a_campaign_deleted_still_starts_at_cycle_one(world):
    """Entirely additive: with an empty retirement table nothing about cycle
    numbering changes, for any year."""
    world.cohort()

    item = world.create_campaign()

    assert item.cycle_seq == 1
    assert world.current_cycle() == 1
    assert world.retirements() == []


def test_a_campaign_deleted_before_it_sent_anything_still_retires_its_cycle(world):
    """Cheap insurance. A campaign that emailed nobody could safely be forgotten,
    but recording it anyway keeps ONE rule — "a deleted cycle is never reused" —
    instead of a rule with an exception that has to be got right at both the
    delete and the create."""
    world.cohort()
    world.schedule()

    result = world.delete_campaign()

    assert result.emails_retired == 0
    assert [r.cycle_seq for r in world.retirements()] == [1]
    assert world.create_campaign().cycle_seq == 2
