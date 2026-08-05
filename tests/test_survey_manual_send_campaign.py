"""A manual send leaves a campaign behind, and that campaign does not re-send (#405).

Jake, 2026-08-05: he cleared every campaign, then sent to a graduation year from
the console. Resend confirms the emails went out; the Surveys console showed no
campaign for that year. That was correct as built — the send writes
`survey_send_log` rows and never a `survey_schedule` row — and the consequence
was not: the schedule is what the cron iterates and what the day 0 / +7 / +14
cadence is measured from, so the initial went out and BOTH REMINDERS SILENTLY
NEVER FIRED. Nothing anywhere said so.

WHY THIS FILE RUNS AGAINST A REAL DATABASE
------------------------------------------
The fix is one line of intent — "leave a campaign behind" — sitting on top of the
single most dangerous invariant in this subsystem: the campaign it creates must
read the send-log rows the send just wrote as ITS OWN stage 0. If it does not,
the very next thing that happens is the cron sending the initial email to the
whole cohort a second time. That is unrecallable and lands in real alumni
inboxes.

The argument that it is safe is a chain: a manual send with no schedule resolves
its cycle through `survey_email.current_cycle_seq`, a fresh schedule row resolves
its own through `next_cycle_seq`, and those two agree — so the claimed rows sit in
the created campaign's cycle and the cycle-scoped double-send guard sees them.
Every link is somewhere else in the codebase and none of them raises when it
breaks. So the assertions here do not inspect the chain; they run it, through the
real `_claim_batch` against the real UNIQUE constraint, and ask what would
actually be emailed.

`test_the_created_campaign_still_sends_the_reminder_on_day_seven` is the
non-vacuity control: "nobody was emailed" is the pass condition of the test above
it, and a campaign that never sends anything at all would satisfy it too.

Harness (SQLite + Postgres string UDFs, the `_Session` facade) is the one from
`test_survey_campaign_delete`, for the same reason it exists there.
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
from app.models.survey_send_config import SurveySendConfig
from app.models.tags import AlumniStatusLabel, StatusLabel
from app.services import survey_email, survey_schedule

_YEAR = 2019
_ANN = 1
_BEN = 2


class _Settings:
    survey_token_secret = "manual-send-campaign-secret"
    survey_from_email = "test@jakegunnell.com"
    survey_from_name = "BYU Finance Alumni"
    survey_app_base_url = "https://finance.alumni.byu.edu"
    resend_api_key = "re_test_key"
    survey_usage_baseline_at = None
    survey_usage_baseline_today = 0
    survey_usage_baseline_month = 0
    cron_secret = "cron"


@pytest.fixture(autouse=True)
def fake_settings(monkeypatch):
    monkeypatch.setattr(survey_email, "get_settings", lambda: _Settings())


# --------------------------------------------------------------- harness -----


@compiles(BigInteger, "sqlite")
def _bigint_is_integer_on_sqlite(type_, compiler, **kw) -> str:
    """``BigInteger`` as SQLite ``INTEGER`` so identity PKs autoincrement — both
    `_claim_batch` and the campaign this feature creates insert rows whose ids
    Postgres generates. (Same shim as `test_survey_campaign_delete`.)"""
    return "INTEGER"


def _pg_split_part(value, delim, index):
    if value is None:
        return None
    parts = value.split(delim)
    return parts[index - 1] if 0 < index <= len(parts) else ""


def _register_pg_functions(dbapi_conn, _record):
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
            CurrentEmployment.__table__,
            SurveySchedule.__table__,
            # From the model, so the REAL unique constraint is in force and the
            # claim can be silently refused here exactly as it would be in prod.
            SurveySendLog.__table__,
            SurveyCampaignRetirement.__table__,
            SurveySendConfig.__table__,
        ],
    )
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
    # Only the four columns `_creator_names` reads — the console resolves who
    # started a campaign, and a campaign created by a send has a creator too.
    conn.execute(
        text(
            "CREATE TABLE users ("
            " user_id INTEGER PRIMARY KEY,"
            " first_name VARCHAR(100),"
            " last_name VARCHAR(100),"
            " email VARCHAR(255))"
        )
    )
    conn.execute(
        text(
            "INSERT INTO users (user_id, first_name, last_name, email)"
            " VALUES (7, 'Jake', 'Gunnell', 'gunnjake@byu.edu')"
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
    """One graduation year, the people in it, and the console acting on it."""

    def __init__(self, conn, monkeypatch):
        self.conn = conn
        self.session = _Session(conn)
        self.monkeypatch = monkeypatch
        # Every address Resend was asked to deliver to, in order — the only thing
        # that actually matters about a double-send.
        self.emailed: list[str] = []
        self._log_id = 0
        self._resp_id = 0

        async def _batch(emails):
            self.emailed.extend(e["to"][0] for e in emails)
            return (None, None)

        monkeypatch.setattr(survey_email, "_send_batch", _batch)

    # -- seeding -------------------------------------------------------------

    def alum(self, alumni_id, first="Ada", last="Lovelace"):
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
                    "personal_email": f"alum{alumni_id}@byu.edu",
                }
            ],
        )
        self.conn.commit()

    def cohort(self):
        self.alum(_ANN, first="Ann")
        self.alum(_BEN, first="Ben", last="Hopper")

    def schedule(self, *, cycle=1, status="active", start_date=None):
        self.conn.execute(
            SurveySchedule.__table__.insert(),
            [
                {
                    "survey_schedule_id": _YEAR * 10 + cycle,
                    "graduation_year": _YEAR,
                    "start_date": start_date or _today(),
                    "status": status,
                    "cycle_seq": cycle,
                }
            ],
        )
        self.conn.commit()

    def sent(self, alumni_id, stages, *, cycle=1, days_ago=0):
        """Send-log rows as the sender would have written them, `days_ago` back."""
        when = _now() - datetime.timedelta(days=days_ago)
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
                    "reset_seq": 0,
                    "sent_at": when,
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
                "t": _now() - datetime.timedelta(days=days_ago),
            },
        )
        self.conn.commit()

    # -- actions -------------------------------------------------------------

    def send(self, *, dry_run=False, limit=None, actor_user_id=7):
        """What the console's Send button does — the whole real path."""
        return asyncio.run(
            survey_email.send_campaign(
                self.session,
                graduation_year=_YEAR,
                actor_user_id=actor_user_id,
                dry_run=dry_run,
                limit=limit,
            )
        )

    def run_cron(self):
        """The daily scheduler, over whatever campaigns now exist."""
        return asyncio.run(
            survey_schedule.run_due_schedules(self.session, actor_user_id=None)
        )

    def delete_campaign(self):
        return asyncio.run(
            survey_schedule.delete_schedule(self.session, _YEAR, actor_user_id=9)
        )

    def rewind_start_date(self, days):
        """Age the campaign by `days`, which is what "a week later" looks like to
        every piece of stage arithmetic in this subsystem."""
        campaign = self.campaign()
        campaign.start_date = campaign.start_date - datetime.timedelta(days=days)
        self.conn.commit()

    def claim(self, *, cycle, stage=0):
        """The irreversible half: reserve the whole cohort in `survey_send_log`.

        Returns who was actually claimed — i.e. who would really be emailed. This
        is the question the feature turns on, asked of the real constraint."""
        loaded = asyncio.run(survey_email._load_recipients(self.session, _YEAR))
        kept, _dropped = survey_email.dedupe_by_email(loaded)
        claimed = asyncio.run(
            survey_email._claim_batch(
                self.session,
                graduation_year=_YEAR,
                stage=stage,
                cycle_seq=cycle,
                batch=kept,
            )
        )
        return sorted(r.alumni_id for r in claimed)

    def targets(self, *, cycle, max_stage=0):
        loaded = asyncio.run(survey_email._load_recipients(self.session, _YEAR))
        kept, _dropped = survey_email.dedupe_by_email(loaded)
        stage, targets = asyncio.run(
            survey_email.select_stage_targets(
                self.session,
                graduation_year=_YEAR,
                recipients=kept,
                max_stage=max_stage,
                cycle_seq=cycle,
            )
        )
        return stage, sorted(r.alumni_id for r in targets)

    # -- reads ---------------------------------------------------------------

    def campaigns(self):
        return self.conn.scalars(select(SurveySchedule)).all()

    def campaign(self):
        rows = self.campaigns()
        assert len(rows) == 1, f"expected exactly one campaign, got {len(rows)}"
        return rows[0]

    def send_rows(self):
        return sorted(
            (r.alumni_id, r.stage, r.cycle_seq)
            for r in self.conn.scalars(select(SurveySendLog)).all()
        )

    def send_row_count(self):
        return int(
            self.conn.scalar(select(func.count()).select_from(SurveySendLog)) or 0
        )

    def audits(self):
        return [a.action_type for a in self.session.added if isinstance(a, AuditLog)]


def _now():
    return datetime.datetime.now(datetime.UTC)


def _today():
    return _now().date()


@pytest.fixture
def world(monkeypatch):
    engine = create_engine(
        "sqlite://",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    event.listen(engine, "connect", _register_pg_functions)
    with engine.begin() as conn:
        _ddl(conn)
    with Session(engine) as session:
        yield _World(session, monkeypatch)
    engine.dispose()


# ====================================================== the campaign appears ==


def test_a_manual_send_to_a_year_with_no_campaign_leaves_one_behind(world):
    """The ask, literally. Emails go out AND a campaign exists afterwards."""
    world.cohort()
    assert world.campaigns() == []

    result = world.send()

    assert sorted(world.emailed) == ["alum1@byu.edu", "alum2@byu.edu"]
    assert result.sent == 2
    assert result.campaign_created is True

    campaign = world.campaign()
    assert campaign.graduation_year == _YEAR
    # `active`, not `scheduled`: the initial has already gone out. Both are
    # runnable, so this is about not lying to the operator.
    assert campaign.status == survey_schedule.STATUS_ACTIVE
    # Anchored to the day the initial actually went out — which is today, because
    # the send that created it is the send that emailed them.
    assert campaign.start_date == _today()


def test_the_campaign_is_on_the_cycle_the_send_claimed_under(world):
    """The invariant the safety of the whole thing rests on, stated directly."""
    world.cohort()
    world.send()

    campaign = world.campaign()
    assert campaign.cycle_seq == survey_email.FIRST_CYCLE
    assert {cycle for _a, _s, cycle in world.send_rows()} == {campaign.cycle_seq}


def test_the_send_is_audited_as_having_created_the_campaign(world):
    world.cohort()
    world.send()

    assert world.audits() == ["send_survey", "create_survey_schedule"]


# ============================================ THE test: no second initial =====


def test_the_created_campaign_does_not_re_send_the_initial(world):
    """The one that matters. Getting this wrong emails a whole cohort twice.

    A manual send to a year with no campaign, then the campaign it created is
    RUN — through the daily cron, and then through the raw claim, which is the
    only thing that can actually put an email on the wire. Neither may reach
    anyone who was already emailed.

    Both halves are asserted because they fail independently and both fail
    silently: the READ half (`logged_alumni_ids` is cycle-scoped, so a campaign
    on the wrong cycle sees nobody as emailed and selects the whole cohort) and
    the CLAIM half (the UNIQUE key, which is what would actually stop the second
    email if the read had already gone wrong)."""
    world.cohort()
    world.send()
    assert sorted(world.emailed) == ["alum1@byu.edu", "alum2@byu.edu"]
    after_the_send = list(world.emailed)
    cycle = world.campaign().cycle_seq

    # 1. The campaign's own guard sees the send's rows as its stage 0...
    assert asyncio.run(
        survey_email.logged_alumni_ids(
            world.session, _YEAR, survey_email.STAGE_INITIAL, cycle
        )
    ) == {_ANN, _BEN}
    # 2. ...so nothing is owed at any stage the calendar permits today...
    assert world.targets(cycle=cycle) == (None, [])
    # 3. ...the cron therefore sends nothing, however many times it runs...
    for _ in range(3):
        world.run_cron()
    assert world.emailed == after_the_send
    assert world.send_row_count() == 2
    # 4. ...and even reaching past the cron straight to the claim, the unique key
    #    refuses every row. Nobody could be emailed a second time even if some
    #    future caller asked for it.
    assert world.claim(cycle=cycle) == []
    assert world.send_rows() == [(_ANN, 0, cycle), (_BEN, 0, cycle)]
    # The campaign is still there, still running, having sent nothing extra.
    assert world.campaign().status in survey_schedule._RUNNABLE_STATUSES


def test_the_created_campaign_still_sends_the_reminder_on_day_seven(world):
    """The non-vacuity control, and the point of the whole issue.

    "Nobody was emailed again" is also true of a campaign that is broken and
    never sends anything, so it is not on its own evidence of a fix. A week on,
    the 1-week reminder must actually go out — that reminder is exactly what the
    manual send was silently losing."""
    world.cohort()
    world.send()
    assert len(world.emailed) == 2

    world.rewind_start_date(7)
    world.run_cron()

    # Both alumni again — but at stage 1, the reminder, not a second initial.
    assert sorted(world.emailed) == [
        "alum1@byu.edu",
        "alum1@byu.edu",
        "alum2@byu.edu",
        "alum2@byu.edu",
    ]
    cycle = world.campaign().cycle_seq
    assert world.send_rows() == [
        (_ANN, 0, cycle),
        (_ANN, 1, cycle),
        (_BEN, 0, cycle),
        (_BEN, 1, cycle),
    ]


def test_the_reminders_are_timed_from_the_send_not_from_the_next_cron_run(world):
    """Day 6 is still inside the initial's window: no reminder yet.

    Pins that the cadence is measured from `start_date` — i.e. from the email
    that actually went out — rather than starting over whenever the cron next
    notices the campaign."""
    world.cohort()
    world.send()

    world.rewind_start_date(6)
    world.run_cron()

    assert len(world.emailed) == 2  # nothing new
    assert world.campaign().status == survey_schedule.STATUS_ACTIVE


# ================================================== dry run creates nothing ===


def test_a_dry_run_creates_no_campaign(world):
    """A preview must not have side effects — least of all one that schedules
    real email to a real cohort."""
    world.cohort()

    result = world.send(dry_run=True)

    assert result.prepared == 2
    assert result.sent == 0
    assert result.campaign_created is False
    assert world.emailed == []
    assert world.campaigns() == []
    assert world.send_row_count() == 0
    assert world.audits() == ["send_survey_dry_run"]


def test_repeated_dry_runs_never_accumulate_anything(world):
    """Staff preview cohorts freely; nothing may build up behind that."""
    world.cohort()
    for _ in range(3):
        world.send(dry_run=True)

    assert world.campaigns() == []
    assert world.send_row_count() == 0


# ======================================================== the partial send ====


def test_a_partial_send_creates_a_campaign_that_claims_only_what_it_sent(world):
    """`limit=1` against a cohort of two.

    The campaign must not imply the cohort was emailed. It cannot: every count
    the console shows is read from `survey_send_log`, and the stage-0-first rule
    means the alum who was NOT reached is still owed the initial — which the next
    cron run delivers, before any reminder."""
    world.cohort()

    result = world.send(limit=1)

    assert result.sent == 1
    assert len(world.emailed) == 1
    assert result.campaign_created is True
    cycle = world.campaign().cycle_seq

    # The console's own numbers say one email, not two.
    item = asyncio.run(survey_schedule.get_schedule(world.session, _YEAR))
    assert item.sent_initial == 1
    assert item.sent_reminder_1 == 0

    # The unreached alum is still a stage-0 target...
    stage, targets = world.targets(cycle=cycle)
    assert stage == survey_email.STAGE_INITIAL
    assert targets == [_BEN]
    # ...and the next cron run finishes the initial for them.
    world.run_cron()
    assert sorted(world.emailed) == ["alum1@byu.edu", "alum2@byu.edu"]
    assert world.send_rows() == [(_ANN, 0, cycle), (_BEN, 0, cycle)]


def test_a_partial_send_still_finishes_the_initial_after_the_reminder_is_due(world):
    """The nastier half of a partial send: the leftover must not be skipped just
    because the calendar has moved on to the reminder window. Stage 0 drains
    first at every ceiling, so the straggler gets their INITIAL — not a reminder
    to an email they never received."""
    world.cohort()
    world.send(limit=1)
    world.rewind_start_date(7)

    world.run_cron()

    cycle = world.campaign().cycle_seq
    # Ben's first-ever email is the initial, on day 7 — not stage 1.
    assert (_BEN, 0, cycle) in world.send_rows()
    assert (_BEN, 1, cycle) not in world.send_rows()


# ===================================================== the repair path ========


def test_a_send_to_a_year_already_emailed_backdates_the_new_campaign(world):
    """Jake's cohort, as it stands right now: send-log rows, no campaign.

    Pressing Send again is how it gets one. Nothing is re-emailed — every
    recipient is already claimed — and the campaign is anchored to the day those
    emails REALLY went out, not to today. Using today would push both reminders
    three days late, which is the failure the start date exists to avoid."""
    world.cohort()
    world.sent(_ANN, (0,), days_ago=3)
    world.sent(_BEN, (0,), days_ago=3)

    result = world.send()

    assert world.emailed == []  # not one more email
    assert result.sent == 0
    assert result.stage_complete is True
    assert result.campaign_created is True
    assert world.campaign().start_date == _today() - datetime.timedelta(days=3)
    assert world.send_row_count() == 2


def test_the_backdated_campaign_sends_its_reminder_on_schedule(world):
    """The consequence of backdating, made concrete: a campaign created 3 days
    into its cadence has 4 days left before the reminder, not 7."""
    world.cohort()
    world.sent(_ANN, (0,), days_ago=7)
    world.sent(_BEN, (0,), days_ago=7)

    world.send()
    # No rewinding: the campaign is already a week old the moment it is created.
    world.run_cron()

    assert sorted(world.emailed) == ["alum1@byu.edu", "alum2@byu.edu"]
    cycle = world.campaign().cycle_seq
    assert (_ANN, 1, cycle) in world.send_rows()  # the reminder, immediately due


# ============================================ when NOT to create a campaign ===


def test_a_send_that_emails_nobody_creates_no_campaign(world):
    """Everyone already replied inside the 365-day window, so there is no send —
    and therefore no campaign to leave behind. An empty campaign would be noise
    on the console and would complete itself in 21 days having done nothing."""
    world.cohort()
    world.replied(_ANN, days_ago=2)
    world.replied(_BEN, days_ago=2)

    result = world.send()

    assert result.sent == 0
    assert result.campaign_created is False
    assert world.campaigns() == []
    assert world.emailed == []


def test_a_year_with_no_alumni_at_all_creates_no_campaign(world):
    result = world.send()

    assert result.campaign_created is False
    assert world.campaigns() == []


def test_an_existing_campaign_is_never_replaced_by_a_send(world):
    """A send is not a re-schedule. `create_schedule` and `start_new_cycle` are
    the two deliberate ways a campaign's start date or cycle changes, and both
    are separate buttons for reasons that cost this codebase a cohort (#357)."""
    world.cohort()
    started = _today() - datetime.timedelta(days=2)
    world.schedule(status=survey_schedule.STATUS_ACTIVE, start_date=started)

    result = world.send()

    assert result.campaign_created is False
    campaign = world.campaign()
    assert campaign.start_date == started
    assert campaign.cycle_seq == 1
    assert world.audits() == ["send_survey"]


def test_a_paused_campaign_is_not_quietly_replaced_by_a_send(world):
    """The worst version of replacing one: a paused campaign silently resurrected
    as `active` with today's start date would restart the cadence and lose the
    pause's whole purpose."""
    world.cohort()
    world.schedule(status=survey_schedule.STATUS_PAUSED, start_date=_today())

    world.send()

    assert world.campaign().status == survey_schedule.STATUS_PAUSED


# ================================== interaction with a DELETED campaign (#398) =


def test_a_send_after_a_delete_creates_a_campaign_above_the_retired_cycle(world):
    """The two features that shipped hours apart, together.

    Deleting a campaign retires its cycle; a manual send for that year resolves
    to the cycle above it. The campaign this send creates has to land on that
    SAME cycle — if it dropped back to the retired one, the retired rows would
    read as its stage 0, it would find everyone already emailed and complete
    having sent nothing (#357), while the console said it ran."""
    world.cohort()
    world.schedule(cycle=1)
    world.sent(_ANN, (0,), cycle=1)
    world.sent(_BEN, (0,), cycle=1)
    world.delete_campaign()
    assert world.campaigns() == []

    result = world.send()

    assert sorted(world.emailed) == ["alum1@byu.edu", "alum2@byu.edu"]
    assert result.campaign_created is True
    campaign = world.campaign()
    assert campaign.cycle_seq == 2
    # The new emails are in the new cycle, beside the retired ones.
    assert world.send_rows() == [(_ANN, 0, 1), (_ANN, 0, 2), (_BEN, 0, 1), (_BEN, 0, 2)]
    # And that campaign does not re-send either.
    assert world.claim(cycle=2) == []


def test_the_created_campaign_can_itself_be_deleted(world):
    """A campaign the system created on the operator's behalf must not be one
    they are stuck with — that was the complaint behind #398."""
    world.cohort()
    world.send()

    result = world.delete_campaign()

    assert result is not None
    assert result.emails_retired == 2
    assert world.campaigns() == []
    # Nothing was destroyed: the emails are still on file.
    assert world.send_row_count() == 2
