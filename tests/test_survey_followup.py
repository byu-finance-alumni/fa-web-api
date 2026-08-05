"""Tests for the "needs manual follow-up" state (#359).

#151's third step — after the last reminder, flag whoever never replied — had no
implementation: `completed` read the same whether the whole cohort answered or
none of it did. These tests cover the set that fills that gap.

Unlike the rest of the survey suite these run the queries FOR REAL, against an
in-memory SQLite database, rather than asserting on a canned fake result. The
whole risk in this feature is in the SQL — a GROUP BY with a HAVING over the send
log, a cycle join, and a correlated NOT EXISTS — and a fake session that returns
whatever the test handed it cannot tell you whether any of that is right. In
particular it could not catch the one failure mode that matters most: an
unscoped read folding a PREVIOUS campaign's non-responders into the current one,
which is the same shape as the #357 bug. SQLite is close enough for this: the
statements use no Postgres-specific syntax, so the same expression tree is
evaluated end to end.

`survey_responses` is created from hand-written DDL because its `payload` column
is JSONB, which SQLite cannot render; only the columns these queries touch are
needed. Same for `alumni` / `alumni_contact_info`, which are far wider than this.
"""

import asyncio
import datetime

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.core.database import Base
from app.models.survey_schedule import SurveySchedule, SurveySendLog
from app.services import survey_schedule as ss

_NOW = datetime.datetime(2026, 8, 1, 12, 0, tzinfo=datetime.UTC)
_YEAR = 2000


class _Session:
    """The async-session surface these services use, over a synchronous ORM one.

    Only `execute` is needed — every follow-up query is a read. Results are real
    SQLAlchemy `Result`s, so `.all()` / `.scalar()` / `.scalar_one_or_none()`
    behave exactly as they do in production, and an entity select really does
    return ORM objects."""

    def __init__(self, session):
        self._session = session

    async def execute(self, stmt):
        return self._session.execute(stmt)


def _ddl(conn):
    Base.metadata.create_all(
        conn, tables=[SurveySchedule.__table__, SurveySendLog.__table__]
    )
    conn.execute(
        text(
            "CREATE TABLE survey_responses ("
            " survey_response_id INTEGER PRIMARY KEY,"
            " alumni_id INTEGER NOT NULL,"
            " graduation_year INTEGER,"
            " status VARCHAR(20) NOT NULL,"
            " submitted_at TIMESTAMP NOT NULL)"
        )
    )
    # An engineer reset supersedes both a reply and the emails that preceded it
    # (#395), so both halves of the non-responder query now reach this table.
    # Empty here: with no resets recorded, every row counts exactly as before.
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
    conn.execute(
        text(
            "CREATE TABLE alumni ("
            " alumni_id INTEGER PRIMARY KEY,"
            " first_name VARCHAR(100),"
            " preferred_first_name VARCHAR(100),"
            " last_name VARCHAR(100),"
            " archived BOOLEAN NOT NULL DEFAULT 0)"
        )
    )
    conn.execute(
        text(
            "CREATE TABLE alumni_contact_info ("
            " contact_info_id INTEGER PRIMARY KEY,"
            " alumni_id INTEGER NOT NULL,"
            " personal_email VARCHAR(255),"
            # The call sheet now reports the address the survey actually went
            # to, which may be the work email (#392).
            " work_email VARCHAR(255))"
        )
    )


class _Fixture:
    """A tiny survey world to ask questions of."""

    def __init__(self, db):
        self.conn = db
        self.session = _Session(db)
        self._log_id = 0

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

    def sent(self, alumni_id, stages, *, year=_YEAR, cycle=1):
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
                    "sent_at": _NOW,
                }
            )
        self.conn.execute(SurveySendLog.__table__.insert(), rows)

    def alum(self, alumni_id, last_name="Zed", email=None, archived=False):
        self.conn.execute(
            text(
                "INSERT INTO alumni (alumni_id, first_name, preferred_first_name,"
                " last_name, archived) VALUES (:i, :f, NULL, :l, :a)"
            ),
            {"i": alumni_id, "f": f"A{alumni_id}", "l": last_name, "a": archived},
        )
        if email is not None:
            self.conn.execute(
                text(
                    "INSERT INTO alumni_contact_info (contact_info_id, alumni_id,"
                    " personal_email) VALUES (:i, :i, :e)"
                ),
                {"i": alumni_id, "e": email},
            )

    def replied(self, alumni_id, status="applied", when=None):
        self.conn.execute(
            text(
                "INSERT INTO survey_responses (alumni_id, graduation_year, status,"
                " submitted_at) VALUES (:a, :y, :s, :t)"
            ),
            {"a": alumni_id, "y": _YEAR, "s": status, "t": when or _NOW},
        )

    def counts(self):
        return asyncio.run(ss._non_responder_counts(self.session))

    def count(self, year=_YEAR):
        return asyncio.run(ss._non_responder_count(self.session, year))

    def names(self, year=_YEAR):
        return asyncio.run(ss.list_non_responders(self.session, year))


@pytest.fixture
def db():
    # StaticPool: every checkout is the SAME connection, so the schema created
    # here is still there for the session below (a second connection to
    # `sqlite://` is a second, empty in-memory database).
    # check_same_thread: TestClient runs the app on its own thread, and the
    # route tests below share this one connection with it.
    engine = create_engine(
        "sqlite://",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    with engine.begin() as conn:
        _ddl(conn)
    with Session(engine) as session:
        yield _Fixture(session)
    engine.dispose()


# --------------------------------------------------------- who is counted -----


def test_all_three_stages_and_no_reply_counts(db):
    db.schedule()
    db.sent(1, (0, 1, 2))
    assert db.counts() == {_YEAR: 1}
    assert db.count() == 1


def test_a_partly_emailed_alum_is_not_yet_a_follow_up(db):
    # Two of three stages: the campaign still owes them a reminder, so they are
    # not a "we tried everything and heard nothing" case yet. Counting them would
    # hand staff a call sheet of people the system is still emailing.
    db.schedule()
    db.sent(1, (0, 1))
    assert db.counts() == {}
    assert db.count() == 0


def test_a_reply_removes_them(db):
    db.schedule()
    db.sent(1, (0, 1, 2))
    db.sent(2, (0, 1, 2))
    db.replied(2)
    assert db.counts() == {_YEAR: 1}


@pytest.mark.parametrize("status", ["pending", "applied"])
def test_pending_and_applied_both_count_as_replies(db, status):
    # A reply awaiting review is still a reply — the alum answered. Only whether
    # staff have PROCESSED it differs, and that is not their problem to be
    # chased about.
    db.schedule()
    db.sent(1, (0, 1, 2))
    db.replied(1, status=status)
    assert db.counts() == {}


def test_a_rejected_reply_is_not_a_reply(db):
    # Staff threw the submission away (spam, junk, someone else's data) — nothing
    # reached the record, so this alum still has not told us anything and still
    # needs following up. Same rule as the send exclusion
    # (`survey_email.RESPONDED_STATUSES`); if the two ever drift an alum can be
    # both "replied" and "never responded".
    db.schedule()
    db.sent(1, (0, 1, 2))
    db.replied(1, status="rejected")
    assert db.counts() == {_YEAR: 1}


def test_a_reply_older_than_the_annual_window_does_not_clear_them(db):
    # The survey is annual. A reply from two years ago says nothing about this
    # campaign — the same 365-day cutoff that makes them re-surveyable makes them
    # a non-responder again.
    db.schedule()
    db.sent(1, (0, 1, 2))
    db.replied(1, when=_NOW - datetime.timedelta(days=800))
    assert db.counts() == {_YEAR: 1}


def test_counts_are_grouped_per_graduation_year(db):
    db.schedule(year=2000)
    db.schedule(year=2001)
    db.sent(1, (0, 1, 2), year=2000)
    db.sent(2, (0, 1, 2), year=2001)
    db.sent(3, (0, 1, 2), year=2001)
    assert db.counts() == {2000: 1, 2001: 2}
    assert db.count(2001) == 2


# ------------------------------------------------------- THE cycle scoping ----


def test_a_previous_cycles_non_responders_are_not_counted_in_this_one(db):
    """The failure this feature is one line away from.

    `survey_send_log` is append-only and spans every campaign the year has ever
    run. Read unscoped, the cycle-1 alum below — who really did ignore all three
    of last year's emails — is reported as a cycle-2 non-responder, on a campaign
    that has not emailed them once. That is the all-time-vs-this-campaign bug
    #357 existed to fix, reintroduced through a different query."""
    db.schedule(cycle=2)
    db.sent(1, (0, 1, 2), cycle=1)  # ignored LAST year's campaign
    db.sent(2, (0, 1, 2), cycle=2)  # ignored THIS one
    db.alum(1)
    db.alum(2)

    assert db.counts() == {_YEAR: 1}
    assert [n.alumni_id for n in db.names()] == [2]


def test_a_fresh_cycle_starts_with_no_backlog(db):
    # Nobody has been emailed in cycle 2 yet, so nobody can have failed to answer
    # it — even though the whole cohort ignored cycle 1.
    db.schedule(cycle=2)
    db.sent(1, (0, 1, 2), cycle=1)
    db.sent(2, (0, 1, 2), cycle=1)
    assert db.counts() == {}
    assert db.count() == 0


def test_stages_spread_across_cycles_do_not_add_up_to_a_complete_campaign(db):
    # Two stages last cycle and one this cycle is not "all three of this
    # campaign". Without the cycle in the GROUP BY they would combine into a
    # phantom completed campaign.
    db.schedule(cycle=2)
    db.sent(1, (0, 1), cycle=1)
    db.sent(1, (2,), cycle=2)
    assert db.counts() == {}


def test_a_year_with_no_schedule_reports_nothing(db):
    # Log rows with no campaign to belong to (a manual send for an unscheduled
    # year) have no "current cycle" to be measured against — the join drops them,
    # exactly as it does for the per-stage counters.
    db.sent(1, (0, 1, 2))
    assert db.counts() == {}


# ------------------------------------------------------------- the names ------


def test_list_non_responders_returns_a_workable_call_sheet(db):
    db.schedule()
    db.sent(1, (0, 1, 2))
    db.sent(2, (0, 1, 2))
    db.alum(1, last_name="Young", email="young@example.com")
    db.alum(2, last_name="Adams", email="adams@example.com")

    items = db.names()
    # Ordered by name, so the list reads like a call sheet and is stable.
    assert [i.name for i in items] == ["A2 Adams", "A1 Young"]
    assert [i.email for i in items] == ["adams@example.com", "young@example.com"]
    assert all(i.last_sent_at is not None for i in items)


def test_list_non_responders_excludes_archived_alumni(db):
    db.schedule()
    db.sent(1, (0, 1, 2))
    db.alum(1, archived=True)
    assert db.names() == []


def test_list_non_responders_is_none_for_a_year_with_no_campaign(db):
    # Distinct from an empty list: "no such campaign" (the route 404s) is not
    # "this campaign has nobody left to chase".
    db.schedule(year=2001)
    assert db.names(1999) is None
    assert db.names(2001) == []


def test_list_non_responders_survives_a_missing_contact_row(db):
    # The address is how staff reach them, but its absence must not drop the
    # person from the list — an alum we can no longer email is MORE in need of
    # manual follow-up, not less.
    db.schedule()
    db.sent(1, (0, 1, 2))
    db.alum(1, email=None)
    items = db.names()
    assert [i.alumni_id for i in items] == [1]
    assert items[0].email is None


# ------------------------------------------------- surfaced on the schedule ---


def test_schedule_item_carries_the_follow_up_count(db):
    db.schedule()
    db.sent(1, (0, 1, 2))
    db.sent(2, (0, 1, 2))
    db.sent(3, (0, 1))  # still mid-campaign
    db.replied(2)

    items = asyncio.run(ss.list_schedules(db.session))
    assert len(items) == 1
    assert items[0].graduation_year == _YEAR
    assert items[0].non_responders == 1
    # `completed` would say the sending finished; this is what says it worked or
    # didn't. The per-stage counters are unaffected.
    assert items[0].sent_initial == 3


# ------------------------------------------------------------------ route -----


def _ctx(*roles):
    import uuid

    from app.schemas.auth import UserContext

    return UserContext(
        user_id=1,
        auth_user_id=uuid.UUID("11111111-1111-1111-1111-111111111111"),
        roles=list(roles),
    )


def _get(path, session, ctx=None):
    from fastapi.testclient import TestClient

    from app.api.dependencies.auth import get_current_db_user, get_permission_config
    from app.core.capabilities import DEFAULT_GRANTS
    from app.core.database import get_session
    from app.main import app

    async def _session():
        yield session

    app.dependency_overrides[get_session] = _session
    app.dependency_overrides[get_permission_config] = lambda: dict(DEFAULT_GRANTS)
    if ctx is not None:
        app.dependency_overrides[get_current_db_user] = lambda: ctx
    try:
        with TestClient(app) as client:
            return client.get(path)
    finally:
        app.dependency_overrides.pop(get_session, None)
        app.dependency_overrides.pop(get_permission_config, None)
        app.dependency_overrides.pop(get_current_db_user, None)


def _path(year=_YEAR):
    return f"/survey/schedules/{year}/non-responders"


def test_non_responders_route_requires_auth(db):
    assert _get(_path(), db.session).status_code == 401


def test_non_responders_route_forbidden_for_view_only(db):
    # It returns alumni contact details, so it is gated like the rest of the
    # survey console rather than as a plain read.
    assert _get(_path(), db.session, _ctx("view_only")).status_code == 403


def test_non_responders_route_returns_the_list_for_full_access(db):
    db.schedule()
    db.sent(1, (0, 1, 2))
    db.alum(1, last_name="Young", email="young@example.com")

    resp = _get(_path(), db.session, _ctx("full_access"))
    assert resp.status_code == 200
    assert resp.json() == [
        {
            "alumni_id": 1,
            "name": "A1 Young",
            "email": "young@example.com",
            "last_sent_at": resp.json()[0]["last_sent_at"],
        }
    ]


def test_non_responders_route_404s_for_a_year_with_no_campaign(db):
    assert _get(_path(1999), db.session, _ctx("full_access")).status_code == 404
