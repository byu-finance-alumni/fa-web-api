"""Offline tests for the derived profile survey history (#40).

The Surveys tab reads `ProfileRead.surveys`, which used to come only from the
`surveys` table — a table with NO writer anywhere in the codebase, so the tab
was empty for every alumnus. `_derive_survey_history` builds the same
`SurveyRead` rows from what actually happened (responses, send log, the cohort
schedule), so these lock in the mapping without needing Postgres.
"""

import asyncio
import datetime

from app.models.survey_reset import SurveyResetLog
from app.models.survey_response import SurveyResponse
from app.models.survey_schedule import SurveySchedule, SurveySendLog
from app.services import profile as service

UTC = datetime.UTC
START = datetime.date(2026, 3, 1)
# start_date + _CAMPAIGN_WINDOW_DAYS — the end of the 2-week reminder window.
DUE = datetime.date(2026, 3, 22)


def _response(
    rid: int,
    *,
    submitted: datetime.datetime,
    status: str = "applied",
    payload: dict | None = None,
    photo: str | None = None,
) -> SurveyResponse:
    r = SurveyResponse()
    r.survey_response_id = rid
    r.alumni_id = 1
    r.submitted_at = submitted
    r.status = status
    r.payload = payload if payload is not None else {"a": "1", "b": "2"}
    r.staged_photo_path = photo
    return r


def _send(stage: int, sent: datetime.datetime) -> SurveySendLog:
    s = SurveySendLog()
    s.alumni_id = 1
    s.graduation_year = 2020
    s.stage = stage
    s.sent_at = sent
    return s


class _Result:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return self._rows


class _FakeSession:
    """Serves each query by the entity it selects from, so the service's own
    ordering/filtering is exercised without a database."""

    def __init__(self, *, responses=(), sends=(), start_date=None, reset_at=None):
        self._responses = list(responses)
        self._sends = list(sends)
        self._start_date = start_date
        # When the alum was last reset by an engineer (#395). Responses at or
        # before it belong to a superseded cycle and are labelled as such —
        # they are NOT removed, which is the point of the redesign.
        self._reset_at = reset_at

    @staticmethod
    def _entity(stmt):
        return stmt.column_descriptions[0]["entity"]

    async def scalars(self, stmt):
        entity = self._entity(stmt)
        if entity is SurveyResponse:
            # The service asks for newest-first.
            return _Result(sorted(self._responses, key=lambda r: r.submitted_at, reverse=True))
        if entity is SurveySendLog:
            return _Result(sorted(self._sends, key=lambda s: s.sent_at))
        raise AssertionError(f"unexpected scalars() on {entity}")

    async def scalar(self, stmt):
        entity = self._entity(stmt)
        if entity is SurveyResetLog:
            return self._reset_at
        assert entity is SurveySchedule
        return self._start_date


def _derive(**kwargs):
    grad_year = kwargs.pop("graduation_year", 2020)
    session = _FakeSession(**kwargs)
    return asyncio.run(service._derive_survey_history(session, 1, grad_year))


def test_no_activity_returns_nothing():
    # No survey was ever sent and none came back — the tab stays hidden rather
    # than showing an empty table.
    assert _derive() == []


def test_applied_response_is_a_completed_row():
    submitted = datetime.datetime(2026, 3, 10, 12, 0, tzinfo=UTC)
    (row,) = _derive(responses=[_response(7, submitted=submitted)], start_date=START)

    assert row.completed is True
    assert row.completed_at == submitted
    assert row.survey_year == 2026
    assert row.survey_status == "Completed"
    assert row.survey_due_date == DUE
    assert row.survey_notes == "2 fields submitted"
    # Synthetic id, negative so it can never collide with a real surveys.PK.
    assert row.survey_id == -7


def test_pending_and_rejected_stay_completed_but_say_so():
    # The alum answered in every one of these cases; only what STAFF did with it
    # differs. `completed` must not flip, or the badge would blame the alum.
    when = datetime.datetime(2026, 3, 10, tzinfo=UTC)
    (pending,) = _derive(
        responses=[_response(1, submitted=when, status="pending")], start_date=START
    )
    (rejected,) = _derive(
        responses=[_response(2, submitted=when, status="rejected")], start_date=START
    )

    assert pending.completed is True
    assert pending.survey_status == "Completed - awaiting review"
    assert rejected.completed is True
    assert rejected.survey_status == "Completed - not applied"


def test_photo_and_singular_field_count_in_notes():
    (row,) = _derive(
        responses=[
            _response(
                1,
                submitted=datetime.datetime(2026, 3, 10, tzinfo=UTC),
                payload={"only": "one"},
                photo="survey-pending/1",
            )
        ],
    )
    assert row.survey_notes == "1 field submitted + photo"


def test_response_predating_the_campaign_gets_no_due_date():
    # An older response was never measured against this campaign's deadline, so
    # it must not borrow it.
    (row,) = _derive(
        responses=[_response(1, submitted=datetime.datetime(2025, 5, 1, tzinfo=UTC))],
        start_date=START,
    )
    assert row.survey_due_date is None
    assert row.survey_year == 2025


def test_sent_with_no_reply_opens_a_pending_row():
    rows = _derive(
        sends=[
            _send(0, datetime.datetime(2026, 3, 1, tzinfo=UTC)),
            _send(1, datetime.datetime(2026, 3, 8, tzinfo=UTC)),
        ],
        start_date=START,
    )
    (row,) = rows

    assert row.completed is False
    assert row.completed_at is None
    assert row.survey_due_date == DUE
    assert row.survey_year == 2026
    # Left None so the UI derives Pending vs Overdue from the due date, and the
    # badge keeps up as the deadline passes without anything re-deriving here.
    assert row.survey_status is None
    # Reports the LATEST stage sent, not the first.
    assert row.survey_notes == "1-week reminder sent 2026-03-08 - no reply yet"
    assert row.survey_id == service._OPEN_CYCLE_SURVEY_ID


def test_a_reply_closes_the_open_row():
    rows = _derive(
        responses=[_response(4, submitted=datetime.datetime(2026, 3, 9, tzinfo=UTC))],
        sends=[_send(0, datetime.datetime(2026, 3, 1, tzinfo=UTC))],
        start_date=START,
    )
    assert [r.completed for r in rows] == [True]


def test_a_reply_before_this_campaign_does_not_close_it():
    # Last year's response must not suppress the row for a campaign they're
    # currently ignoring.
    rows = _derive(
        responses=[_response(4, submitted=datetime.datetime(2025, 4, 1, tzinfo=UTC))],
        sends=[_send(0, datetime.datetime(2026, 3, 1, tzinfo=UTC))],
        start_date=START,
    )
    assert sorted(r.completed for r in rows) == [False, True]


def test_no_cohort_schedule_means_no_due_date():
    # Nothing scheduled for their graduation year: still show what was sent,
    # just without inventing a deadline.
    (row,) = _derive(
        sends=[_send(0, datetime.datetime(2026, 3, 1, tzinfo=UTC))],
        start_date=None,
        graduation_year=None,
    )
    assert row.survey_due_date is None
    assert row.survey_year == 2026


def test_a_response_from_before_a_reset_is_kept_and_labelled():
    """An engineer reset deletes nothing (#395), so the answer still renders —
    but it belongs to a cycle that has since been re-opened, and an unlabelled
    older answer to "the same" survey reads as a duplicate."""
    submitted = datetime.datetime(2026, 3, 5, tzinfo=UTC)
    rows = _derive(
        responses=[_response(1, submitted=submitted)],
        start_date=START,
        reset_at=datetime.datetime(2026, 4, 1, tzinfo=UTC),
    )
    (row,) = rows
    assert row.completed is True
    assert "previous survey cycle" in row.survey_notes


def test_a_response_after_the_reset_is_not_labelled():
    rows = _derive(
        responses=[_response(1, submitted=datetime.datetime(2026, 5, 5, tzinfo=UTC))],
        start_date=START,
        reset_at=datetime.datetime(2026, 4, 1, tzinfo=UTC),
    )
    (row,) = rows
    assert "previous survey cycle" not in row.survey_notes
