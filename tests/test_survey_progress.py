"""At-a-glance campaign progress: recipients, replied, awaiting review (#543).

The three per-stage counts say what LEFT. `non_responders` only counts people
who have had all three emails, so for the first fortnight of every campaign it
is legitimately 0 however many have answered. Neither answers "how is it going",
which is the question actually being asked while a campaign runs.

Pure tests: the counts are one SQL statement, so the shape is asserted by
compiling it, and the mapping onto the schedule item is asserted through the
existing fake session.
"""

import asyncio
import datetime

import pytest
from sqlalchemy.dialects import postgresql

from app.services import survey_schedule
from tests.test_survey_scheduler import _COUNTS, QueueSession, _Res, _sched


def _sql() -> str:
    return str(
        survey_schedule._cycle_progress().compile(dialect=postgresql.dialect())
    )


def test_counts_are_scoped_to_the_year_current_cycle():
    # The join on BOTH graduation_year and cycle_seq is the whole of #357. Drop
    # it and a year on its second campaign reports last year's replies as this
    # year's, silently and with a plausible-looking number.
    sql = _sql()
    assert "survey_schedule" in sql
    assert "cycle_seq" in sql


def test_a_reset_send_is_not_counted_as_a_recipient():
    # Someone an engineer reset is owed the campaign again (#395), so their
    # pre-reset emails must not inflate the denominator.
    assert "survey_reset_log" in _sql()


def test_recipients_are_counted_once_each():
    # Three emails to one person is one recipient. Without DISTINCT the rate
    # would fall as the reminders went out, which is exactly backwards.
    sql = _sql().replace("\n", " ")
    assert "count(distinct(survey_send_log.alumni_id)) AS recipients" in sql


def test_replies_are_counted_over_the_send_log_not_the_response_table():
    # Counting responses directly would let a reply from someone outside this
    # cycle push the rate above 100%. Every number shares one denominator.
    sql = _sql().replace("\n", " ")
    assert sql.strip().lower().startswith("select survey_send_log.graduation_year")


def test_a_rejected_reply_does_not_count_as_a_reply():
    # RESPONDED_STATUSES is ('pending', 'applied'). Staff threw a rejected
    # submission away, so the alum still owes an answer — the sender takes that
    # view, and this has to agree with it or the console and the sender describe
    # two different cohorts.
    assert survey_schedule.survey_email.RESPONDED_STATUSES == ("pending", "applied")
    sql = _sql()
    assert "rejected" not in sql


@pytest.mark.parametrize(
    ("recipients", "replied", "awaiting"),
    [(0, 0, 0), (26, 4, 2), (100, 100, 0)],
)
def test_counts_reach_the_schedule_item(recipients, replied, awaiting):
    year = 2001
    session = QueueSession(
        [
            _Res(scalars_all=[_sched(year, datetime.date(2026, 5, 1), status="active")]),
            _Res(rows=[]),  # per-stage counts
            _Res(rows=[]),  # manual-follow-up counts
            _Res(rows=[]),  # all-time sent counts
            _Res(rows=[(year, recipients, replied, awaiting)]),
        ]
    )
    item = asyncio.run(survey_schedule.list_schedules(session))[0]
    assert (item.recipients, item.replied, item.awaiting_review) == (
        recipients,
        replied,
        awaiting,
    )


def test_a_year_with_no_sends_reports_zeroes_rather_than_failing():
    # A campaign created but not yet started has no send-log rows at all, so it
    # is simply absent from the grouped result. It must read as 0/0/0, not blow
    # up and not inherit another year's numbers.
    session = QueueSession(
        [
            _Res(scalars_all=[_sched(2002, datetime.date(2026, 12, 1))]),
        ]
        + [_Res(rows=[])] * _COUNTS
    )
    item = asyncio.run(survey_schedule.list_schedules(session))[0]
    assert (item.recipients, item.replied, item.awaiting_review) == (0, 0, 0)
