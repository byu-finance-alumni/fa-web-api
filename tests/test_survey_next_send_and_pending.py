"""The console's "what goes out next" line (#562) and the year picker's pending
count (#856).

Both are run against a real (in-memory SQLite) database, like the campaign
delete suite whose harness this borrows, because both are only worth anything if
they AGREE with another part of the system:

* the picker's "2020 (14)" must equal the Submissions tab's badge, which is the
  length of ``survey_responses.list_pending`` — so the test asks both questions of
  the same rows and compares them, rather than checking either against a
  hand-picked number;
* the next send must be the stage the cron would actually send. It is answered
  by ``survey_email.select_stage_targets`` over the real send log, so the cases
  below are the ones the issue warns about: an unfinished earlier stage, a
  half-sent reminder, a paused campaign, a previous cycle's rows.
"""

import asyncio
import datetime
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, event, text
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.core.database import Base
from app.models.alumni import Alumni
from app.models.contact import AlumniContactInfo
from app.models.employment import CurrentEmployment
from app.models.engagement import AlumniProgramEngagement
from app.models.survey_retirement import SurveyCampaignRetirement
from app.models.survey_schedule import SurveySchedule, SurveySendLog
from app.models.tags import AlumniStatusLabel, StatusLabel
from app.services import survey_email, survey_responses, survey_schedule

# The SQLite harness (BigInteger shim, Postgres string UDFs, async facade) is
# the campaign-delete suite's; importing the module registers the shim.
from tests.test_survey_campaign_delete import _register_pg_functions, _Session

_YEAR = 2019
_OTHER_YEAR = 2020
_START = datetime.date(2026, 7, 1)
_ANN = 1
_BEN = 2


def _at(day: datetime.date, hour: int, minute: int = 0) -> datetime.datetime:
    return datetime.datetime(
        day.year, day.month, day.day, hour, minute, tzinfo=datetime.UTC
    )


def _day(n: int) -> datetime.date:
    """``n`` days after the campaign start."""
    return _START + datetime.timedelta(days=n)


def _ddl(conn):
    Base.metadata.create_all(
        conn,
        tables=[
            Alumni.__table__,
            AlumniContactInfo.__table__,
            AlumniStatusLabel.__table__,
            StatusLabel.__table__,
            CurrentEmployment.__table__,
            AlumniProgramEngagement.__table__,
            SurveySchedule.__table__,
            SurveySendLog.__table__,
            SurveyCampaignRetirement.__table__,
        ],
    )
    # Hand-written: `payload` is JSONB, which SQLite cannot render. Every mapped
    # column is present because `list_pending` selects the whole entity.
    conn.execute(
        text(
            "CREATE TABLE survey_responses ("
            " survey_response_id INTEGER PRIMARY KEY,"
            " alumni_id INTEGER NOT NULL,"
            " graduation_year INTEGER,"
            " payload TEXT NOT NULL DEFAULT '{}',"
            " status VARCHAR(20) NOT NULL,"
            " staged_photo_path VARCHAR(255),"
            " cycle_seq INTEGER,"
            " stage SMALLINT,"
            " fill_seconds INTEGER,"
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
    def __init__(self, conn):
        self.conn = conn
        self.session = _Session(conn)
        self._log_id = 0
        self._resp_id = 0

    def alum(self, alumni_id, *, year=_YEAR, first="Ada"):
        self.conn.execute(
            Alumni.__table__.insert(),
            [
                {
                    "alumni_id": alumni_id,
                    "first_name": first,
                    "last_name": "Lovelace",
                    "graduation_year": year,
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
        self.alum(_BEN, first="Ben")

    def sent(self, alumni_id, stages, *, cycle=1):
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
                    "sent_at": datetime.datetime.now(datetime.UTC),
                }
            )
        self.conn.execute(SurveySendLog.__table__.insert(), rows)
        self.conn.commit()

    def response(self, alumni_id, *, status, year=_YEAR):
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
                "y": year,
                "s": status,
                "t": datetime.datetime.now(datetime.UTC)
                - datetime.timedelta(days=1),
            },
        )
        self.conn.commit()

    def schedule_row(self, *, status="active", cycle=1, start=_START):
        self.conn.execute(
            SurveySchedule.__table__.insert(),
            [
                {
                    "survey_schedule_id": _YEAR,
                    "graduation_year": _YEAR,
                    "start_date": start,
                    "status": status,
                    "cycle_seq": cycle,
                }
            ],
        )
        self.conn.commit()


@pytest.fixture
def world():
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


@pytest.fixture
def clock(monkeypatch):
    """Set "now" for the next-send date: ``clock(day_offset, hour)``."""

    def _set(day_offset: int, hour: int = 12, minute: int = 0):
        now = _at(_day(day_offset), hour, minute)
        monkeypatch.setattr(survey_schedule, "_now", lambda: now)
        return now

    return _set


def _sched(*, status="active", start=_START, cycle=1, last_run_at=None):
    return SimpleNamespace(
        graduation_year=_YEAR,
        start_date=start,
        status=status,
        cycle_seq=cycle,
        last_run_at=last_run_at,
    )


def _next(world, sched):
    return asyncio.run(survey_schedule._next_send(world.session, sched))


# ------------------------------------------------------- #856 pending count ---


def test_picker_pending_count_equals_the_review_queue(world):
    """The number in "2019 (N)" is the length of the list the Submissions tab
    renders — over every status and an orphaned row the queue skips."""
    world.cohort()
    world.alum(3, year=_OTHER_YEAR)
    world.alum(4)
    world.alum(5)
    world.response(_ANN, status="pending")
    world.response(_BEN, status="pending")
    # The same alum submitting twice is two submissions — the badge counts rows.
    world.response(_BEN, status="pending")
    world.response(4, status="applied")
    world.response(5, status="rejected")
    world.response(_ANN, status="confirmed")
    world.response(3, status="pending", year=_OTHER_YEAR)
    # A pending row whose alum is gone: the queue drops it, so must the count.
    world.response(999, status="pending")

    counts = asyncio.run(survey_email.pending_review_counts_by_year(world.session))
    for year in (_YEAR, _OTHER_YEAR):
        queue = asyncio.run(survey_responses.list_pending(world.session, year))
        assert counts.get(year, 0) == len(queue)
    assert counts == {_YEAR: 3, _OTHER_YEAR: 1}


def test_a_year_with_nothing_to_review_is_absent_not_zero(world):
    world.cohort()
    world.response(_ANN, status="applied")
    assert asyncio.run(survey_email.pending_review_counts_by_year(world.session)) == {}


# ----------------------------------------------------- #562 next send: when ---


@pytest.mark.parametrize(
    "hour,minute,last_run_hour,expected_offset",
    [
        (9, 0, None, 0),  # morning: today's 18:00 run is still to come
        (18, 30, None, 0),  # inside the cron hour, not fired yet
        (18, 30, 10, 0),  # a run stamped this morning is not today's cron
        (18, 30, 18, 1),  # inside the hour and it HAS fired
        (19, 0, None, 1),  # past the cron hour
        (23, 59, None, 1),
    ],
)
def test_next_cron_date(hour, minute, last_run_hour, expected_offset):
    today = _day(3)
    now = _at(today, hour, minute)
    last = _at(today, last_run_hour, 5) if last_run_hour is not None else None
    assert survey_schedule._next_cron_date(now, last) == today + datetime.timedelta(
        days=expected_offset
    )


# ----------------------------------------------- #562 next send: which stage ---


def test_not_started_yet_is_the_initial_on_the_start_date(world, clock):
    world.cohort()
    clock(-5)
    assert _next(world, _sched(status="scheduled")) == (0, _START, 2)


def test_initial_window_sends_the_initial_at_the_next_run(world, clock):
    world.cohort()
    clock(2, hour=9)
    assert _next(world, _sched()) == (0, _day(2), 2)
    clock(2, hour=20)
    assert _next(world, _sched()) == (0, _day(3), 2)


def test_initial_drained_next_is_the_1_week_reminder_on_day_7(world, clock):
    world.cohort()
    world.sent(_ANN, [0])
    world.sent(_BEN, [0])
    clock(3)
    assert _next(world, _sched()) == (1, _day(7), 2)
    # On the boundary day itself, before the cron: today.
    clock(7, hour=9)
    assert _next(world, _sched()) == (1, _day(7), 2)
    # Day 6 after the cron hour: the next run is day 7, which is the window.
    clock(6, hour=20)
    assert _next(world, _sched()) == (1, _day(7), 2)


def test_a_reply_shrinks_the_count(world, clock):
    world.cohort()
    world.sent(_ANN, [0])
    world.sent(_BEN, [0])
    world.response(_ANN, status="pending")
    clock(3)
    assert _next(world, _sched()) == (1, _day(7), 1)


def test_an_unfinished_initial_is_reported_before_any_reminder(world, clock):
    """Day 10 is the 1-week window, but Ben never got the initial (cap-starved).
    The cron finishes stage 0 first, so that is what "next" must say."""
    world.cohort()
    world.sent(_ANN, [0])
    clock(10)
    assert _next(world, _sched()) == (0, _day(10), 1)


def test_a_half_sent_reminder_is_finished_now(world, clock):
    world.cohort()
    world.sent(_ANN, [0, 1])
    world.sent(_BEN, [0])
    clock(10)
    assert _next(world, _sched()) == (1, _day(10), 1)


def test_reminder_1_drained_next_is_the_2_week_reminder_on_day_14(world, clock):
    world.cohort()
    world.sent(_ANN, [0, 1])
    world.sent(_BEN, [0, 1])
    clock(10)
    assert _next(world, _sched()) == (2, _day(14), 2)
    clock(14, hour=9)
    assert _next(world, _sched()) == (2, _day(14), 2)


def test_a_late_reminder_goes_at_the_next_run_not_its_old_window(world, clock):
    # Day 30: every window has passed, the 2-week reminder never went out.
    world.cohort()
    world.sent(_ANN, [0, 1])
    world.sent(_BEN, [0, 1])
    clock(30, hour=9)
    assert _next(world, _sched()) == (2, _day(30), 2)


def test_every_stage_delivered_has_no_next_send(world, clock):
    world.cohort()
    world.sent(_ANN, [0, 1, 2])
    world.sent(_BEN, [0, 1, 2])
    clock(16)
    assert _next(world, _sched()) == (None, None, None)


def test_the_date_follows_the_rows_start_date_not_the_calendar(world, clock):
    """A resume shifts `start_date`; the date must move with it."""
    world.cohort()
    world.sent(_ANN, [0])
    world.sent(_BEN, [0])
    clock(3)
    resumed = _START + datetime.timedelta(days=5)
    assert _next(world, _sched(start=resumed)) == (
        1,
        resumed + datetime.timedelta(days=7),
        2,
    )


def test_a_previous_cycles_sends_do_not_count(world, clock):
    world.cohort()
    world.sent(_ANN, [0, 1, 2], cycle=1)
    world.sent(_BEN, [0, 1, 2], cycle=1)
    clock(2, hour=9)
    assert _next(world, _sched(cycle=2)) == (0, _day(2), 2)


class _NoQuerySession:
    async def execute(self, _stmt):  # pragma: no cover - must not be reached
        raise AssertionError("a non-runnable campaign must not be queried")


@pytest.mark.parametrize("status", ["paused", "completed", "cancelled"])
def test_a_campaign_that_cannot_send_has_no_next_send_and_costs_nothing(status):
    result = asyncio.run(
        survey_schedule._next_send(_NoQuerySession(), _sched(status=status))
    )
    assert result == (None, None, None)
    sends = asyncio.run(
        survey_schedule._next_sends(_NoQuerySession(), [_sched(status=status)])
    )
    assert sends == {}


# ----------------------------------------------------- #562 list wiring -------


def test_the_list_read_carries_the_next_send(world, clock):
    world.cohort()
    world.schedule_row()
    world.sent(_ANN, [0])
    world.sent(_BEN, [0])
    clock(3)
    item = asyncio.run(
        survey_schedule.list_schedules(world.session, with_next_send=True)
    )[0]
    assert (item.next_stage, item.next_send_date, item.next_send_count) == (
        1,
        _day(7),
        2,
    )
    # The default read (what the write endpoints echo) does not compute it.
    plain = asyncio.run(survey_schedule.list_schedules(world.session))[0]
    assert (plain.next_stage, plain.next_send_date, plain.next_send_count) == (
        None,
        None,
        None,
    )


def test_the_list_read_leaves_a_paused_campaign_blank(world, clock):
    world.cohort()
    world.schedule_row(status="paused")
    clock(3)
    item = asyncio.run(
        survey_schedule.list_schedules(world.session, with_next_send=True)
    )[0]
    assert item.next_stage is None and item.next_send_date is None
    assert item.next_send_count is None
