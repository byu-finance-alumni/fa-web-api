"""At-a-glance campaign progress: recipients, replied, awaiting review (#543),
plus the applied/rejected review outcome (#497).

The three per-stage counts say what LEFT. `non_responders` only counts people
who have had all three emails, so for the first fortnight of every campaign it
is legitimately 0 however many have answered. Neither answers "how is it going",
which is the question actually being asked while a campaign runs — and none of
them says how much of what came back was USABLE, which is #497.

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


def _column_sql(label: str) -> str:
    """One selected column, with its status list rendered literally.

    Statuses are bound parameters, so the whole-statement SQL never shows which
    ones a column filters on — which is exactly what the reply-definition tests
    below need to see."""
    column = next(
        c for c in survey_schedule._cycle_progress().selected_columns if c.name == label
    )
    return str(
        column.compile(
            dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}
        )
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
    # RESPONDED_STATUSES is ('pending', 'applied', 'confirmed'). Staff threw a
    # rejected submission away, so the alum still owes an answer — the sender
    # takes that view, and this has to agree with it or the console and the
    # sender describe two different cohorts. Asserted on the `replied` COLUMN,
    # not on the whole statement, because #497 added a column legitimately
    # labelled `rejected`.
    assert survey_schedule.survey_email.RESPONDED_STATUSES == (
        "pending",
        "applied",
        "confirmed",
    )
    replied = _column_sql("replied")
    assert "IN ('pending', 'applied', 'confirmed')" in replied
    assert "rejected" not in replied


def test_a_confirmation_counts_as_a_reply():
    # #755 — "yes, everything is correct" recorded nothing at all before, so the
    # FASTEST responders were the only ones missing from the response rate. It is
    # a reply, and `replied` is where it has to land.
    assert "IN ('pending', 'applied', 'confirmed')" in _column_sql("replied")


# ------------------------------------------- applied vs rejected (#497) -------


def test_applied_and_rejected_filter_on_exactly_one_status_each():
    # The status triple, split three ways: `awaiting_review` is pending,
    # `applied` is applied, `rejected` is rejected. A column that filtered on two
    # of them would double-count somebody in the console's outcome columns.
    assert "IN ('pending')" in _column_sql("awaiting_review")
    assert "IN ('applied')" in _column_sql("applied")
    assert "IN ('rejected')" in _column_sql("rejected")
    # #755 — and the fourth status gets its own column for the same reason.
    assert "IN ('confirmed')" in _column_sql("confirmed")


def test_a_confirmation_is_not_review_work_and_is_not_an_applied_change():
    # The whole reason `confirmed` could not be expressed as one of the other
    # three: it is a reply with NOTHING to review and NOTHING written to the
    # record. If it leaked into `awaiting_review` the console's actionable
    # number would fill with rows no reviewer can act on; if it leaked into
    # `applied` the "how much of what came back was usable" column would count
    # changes that were never made.
    assert "confirmed" not in _column_sql("awaiting_review")
    assert "confirmed" not in _column_sql("applied")
    assert "confirmed" not in _column_sql("rejected")


def test_the_new_counts_use_the_shared_reply_definition():
    # Same 365-day window, same reset rule, same correlated EXISTS over the send
    # log as `replied` — only the status list differs. Re-deriving any of that
    # would put the outcome columns on a different denominator from the rate
    # sitting next to them.
    for label in ("applied", "rejected", "confirmed"):
        column = _column_sql(label)
        assert "survey_responses.submitted_at >=" in column
        assert "survey_reset_log" in column
        assert "count(distinct(" in column
        assert "survey_send_log.alumni_id END" in column


def test_the_new_counts_are_scoped_to_the_current_cycle_like_every_other_count():
    # They ride the same grouped statement, so they inherit the #357 cycle join
    # rather than needing (and possibly missing) their own.
    sql = _sql().replace("\n", " ")
    assert "AS applied" in sql and "AS rejected" in sql and "AS confirmed" in sql
    assert (
        "JOIN survey_schedule ON survey_schedule.graduation_year = "
        "survey_send_log.graduation_year AND survey_schedule.cycle_seq = "
        "survey_send_log.cycle_seq" in sql
    )


def test_existing_counts_are_unchanged():
    # #497 is additive. The three #543 columns keep their position and their
    # meaning; the new ones are appended.
    names = [c.name for c in survey_schedule._cycle_progress().selected_columns]
    assert names == [
        "graduation_year",
        "recipients",
        "replied",
        "awaiting_review",
        "applied",
        "rejected",
        # #755, appended for the same reason #497's two were: the columns before
        # it keep their position AND their meaning.
        "confirmed",
    ]


@pytest.mark.parametrize(
    ("recipients", "replied", "awaiting", "applied", "rejected", "confirmed"),
    [
        # A year nobody has answered: emailed, nothing back at all.
        (26, 0, 0, 0, 0, 0),
        # The mixed case — some queued, some accepted, some binned. Note
        # `replied` (4) is pending + applied + confirmed and deliberately
        # EXCLUDES the two rejected, so awaiting + applied + rejected != replied.
        (26, 4, 2, 2, 2, 0),
        # Everything reviewed and accepted: nothing left in the queue.
        (100, 100, 0, 100, 0, 0),
        # Everything that came back was junk. Nobody counts as having replied,
        # which is the point — those alumni still owe an answer.
        (40, 0, 0, 0, 7, 0),
        # #755 — the good case the console could not previously show at all:
        # nine alumni replied and eight of them had nothing to correct, so there
        # is one submission to review and no applied changes. Before the
        # confirmation column that read as "9 replied" with 8 unaccounted for.
        (30, 9, 1, 0, 0, 8),
    ],
)
def test_counts_reach_the_schedule_item(
    recipients, replied, awaiting, applied, rejected, confirmed
):
    year = 2001
    session = QueueSession(
        [
            _Res(scalars_all=[_sched(year, datetime.date(2026, 5, 1), status="active")]),
            _Res(rows=[]),  # per-stage counts
            _Res(rows=[]),  # manual-follow-up counts
            _Res(rows=[]),  # all-time sent counts
            _Res(
                rows=[
                    (year, recipients, replied, awaiting, applied, rejected, confirmed)
                ]
            ),
        ]
    )
    item = asyncio.run(survey_schedule.list_schedules(session))[0]
    assert (
        item.recipients,
        item.replied,
        item.awaiting_review,
        item.applied,
        item.rejected,
        item.confirmed,
    ) == (recipients, replied, awaiting, applied, rejected, confirmed)


def test_a_rejected_submission_never_lands_in_replied():
    # The console must not be able to show an alum as both "replied" and
    # "never responded". `replied` is whatever the query returned for it; the
    # rejected count rides alongside and never feeds it.
    year = 2003
    session = QueueSession(
        [
            _Res(scalars_all=[_sched(year, datetime.date(2026, 5, 1), status="active")]),
            _Res(rows=[]),
            _Res(rows=[]),
            _Res(rows=[]),
            _Res(rows=[(year, 10, 1, 1, 0, 5, 0)]),
        ]
    )
    item = asyncio.run(survey_schedule.list_schedules(session))[0]
    assert item.rejected == 5
    assert item.replied == 1  # not 6


def test_a_year_with_no_sends_reports_zeroes_rather_than_failing():
    # A campaign created but not yet started has no send-log rows at all, so it
    # is simply absent from the grouped result. It must read as all-zero, not
    # blow up and not inherit another year's numbers.
    session = QueueSession(
        [
            _Res(scalars_all=[_sched(2002, datetime.date(2026, 12, 1))]),
        ]
        + [_Res(rows=[])] * _COUNTS
    )
    item = asyncio.run(survey_schedule.list_schedules(session))[0]
    assert (
        item.recipients,
        item.replied,
        item.awaiting_review,
        item.applied,
        item.rejected,
        item.confirmed,
    ) == (0, 0, 0, 0, 0, 0)
