"""Tests for the survey send scheduler (#542) — service + cron route.

All run against fakes (no real DB / network), mirroring the monkeypatch style in
tests/test_survey_email.py: `_load_recipients` / `_send_batch` are stubbed, and
the scheduler's own DB helpers (`_load_schedules_due`, `_logged_alumni_ids`) are
monkeypatched so `run_due_schedules` can be exercised without a session backend.
"""

import asyncio
import datetime
from types import SimpleNamespace

import pytest

from app.schemas.survey import SurveyScheduleRunSummary, SurveySendConfigItem
from app.services import survey_email, survey_schedule

_TODAY = datetime.date(2026, 7, 29)


class _Settings:
    survey_token_secret = "sched-unit-secret"
    survey_from_email = "test@jakegunnell.com"
    survey_from_name = "BYU Finance Alumni"
    survey_app_base_url = "https://finance.alumni.byu.edu"
    resend_api_key = "re_test_key"


@pytest.fixture
def fake_settings(monkeypatch):
    settings = _Settings()
    monkeypatch.setattr(survey_email, "get_settings", lambda: settings)
    monkeypatch.setattr(survey_schedule, "_today", lambda: _TODAY)
    return settings


def _rcpts(ids):
    return [
        survey_email.Recipient(i, f"A{i}", f"a{i}@example.com", (("Company", "X"),))
        for i in ids
    ]


def _sched(
    year,
    start,
    status="scheduled",
    created_by=None,
    paused_at=None,
    paused_from_status=None,
):
    return SimpleNamespace(
        survey_schedule_id=year,
        graduation_year=year,
        start_date=start,
        status=status,
        last_run_at=None,
        created_at=None,
        created_by_user_id=created_by,
        paused_at=paused_at,
        paused_from_status=paused_from_status,
    )


def _utc(d, hour=12):
    """A UTC timestamp on date ``d`` — what `paused_at` holds."""
    return datetime.datetime(d.year, d.month, d.day, hour, tzinfo=datetime.UTC)


class FakeSession:
    """Records added rows + commits (run_due_schedules never queries it directly —
    its DB helpers are monkeypatched)."""

    def __init__(self):
        self.added = []
        self.commits = 0

    def add(self, obj):
        self.added.append(obj)

    async def commit(self):
        self.commits += 1


def _logs(session):
    return [a for a in session.added if type(a).__name__ == "SurveySendLog"]


def _audits(session):
    return [a for a in session.added if type(a).__name__ == "AuditLog"]


# ------------------------------------------------------------ stage math ------


def test_stage_for_windows():
    assert survey_schedule._stage_for(0) == 0
    assert survey_schedule._stage_for(6) == 0
    assert survey_schedule._stage_for(7) == 1
    assert survey_schedule._stage_for(13) == 1
    assert survey_schedule._stage_for(14) == 2
    assert survey_schedule._stage_for(20) == 2
    assert survey_schedule._stage_for(21) is None  # campaign done


# ---------------------------------------------------------- run: stage 0 ------


def _patch_run(monkeypatch, *, schedules, recipients, logged, batch, allowance=None):
    async def fake_due(session, today):
        return schedules

    async def fake_recipients(session, year):
        return recipients

    async def fake_logged(session, year, stage):
        return set(logged.get((year, stage), set()))

    async def fake_allowance(session):
        # None = cap disabled (unlimited); an int = the shared run budget.
        return allowance

    monkeypatch.setattr(survey_schedule, "_load_schedules_due", fake_due)
    monkeypatch.setattr(survey_email, "_load_recipients", fake_recipients)
    monkeypatch.setattr(survey_schedule, "_logged_alumni_ids", fake_logged)
    monkeypatch.setattr(survey_schedule, "_run_allowance", fake_allowance)
    monkeypatch.setattr(survey_email, "_send_batch", batch)


def test_run_sends_stage0_and_logs_recipients(fake_settings, monkeypatch):
    sent_to = []

    async def batch(emails):
        sent_to.extend(e["to"][0] for e in emails)
        return (None, None)

    _patch_run(
        monkeypatch,
        schedules=[_sched(2000, _TODAY)],  # elapsed 0 -> stage 0
        recipients=_rcpts([1, 2, 3]),
        logged={},
        batch=batch,
    )

    session = FakeSession()
    summary = asyncio.run(survey_schedule.run_due_schedules(session, actor_user_id=9))

    assert isinstance(summary, SurveyScheduleRunSummary)
    item = summary.ran[0]
    assert item.graduation_year == 2000
    assert item.stage == 0
    assert item.sent == 3
    assert item.remaining == 0
    assert sorted(sent_to) == ["a1@example.com", "a2@example.com", "a3@example.com"]
    # A send_log row per recipient, all stage 0.
    logs = _logs(session)
    assert sorted(r.alumni_id for r in logs) == [1, 2, 3]
    assert {r.stage for r in logs} == {0}
    # Audit row carries sent=N so the usage tally counts scheduled sends.
    audit = _audits(session)[0]
    assert audit.action_type == "send_survey"
    assert "sent=3" in audit.new_value and "stage=0" in audit.new_value


def test_second_run_does_not_reemail_logged(fake_settings, monkeypatch):
    calls = []

    async def batch(emails):
        calls.append(emails)
        return (None, None)

    _patch_run(
        monkeypatch,
        schedules=[_sched(2000, _TODAY)],
        recipients=_rcpts([1, 2, 3]),
        logged={(2000, 0): {1, 2, 3}},  # all three already got the initial
        batch=batch,
    )

    session = FakeSession()
    summary = asyncio.run(survey_schedule.run_due_schedules(session))

    assert summary.ran[0].sent == 0
    assert calls == []  # Resend never called
    assert _logs(session) == []  # no new log rows


def test_stage_advances_by_date(fake_settings, monkeypatch):
    async def batch(emails):
        return (None, None)

    _patch_run(
        monkeypatch,
        schedules=[_sched(2000, _TODAY - datetime.timedelta(days=8))],  # stage 1
        recipients=_rcpts([1, 2, 3]),
        logged={(2000, 0): {1, 2, 3}},  # they all received the initial
        batch=batch,
    )

    session = FakeSession()
    summary = asyncio.run(survey_schedule.run_due_schedules(session))

    assert summary.ran[0].stage == 1
    assert summary.ran[0].sent == 3
    assert {r.stage for r in _logs(session)} == {1}


def test_reminder_targets_only_initial_nonresponders(fake_settings, monkeypatch):
    sent_to = []

    async def batch(emails):
        sent_to.extend(e["to"][0] for e in emails)
        return (None, None)

    # _load_recipients already dropped repliers, so alum 3 (replied) is absent.
    # Of the remaining, alum 1 already got the 1-week reminder, so only alum 2 is
    # a genuine reminder target.
    _patch_run(
        monkeypatch,
        schedules=[_sched(2000, _TODAY - datetime.timedelta(days=8))],  # stage 1
        recipients=_rcpts([1, 2]),
        logged={(2000, 0): {1, 2, 3}, (2000, 1): {1}},
        batch=batch,
    )

    session = FakeSession()
    summary = asyncio.run(survey_schedule.run_due_schedules(session))

    assert summary.ran[0].sent == 1
    assert sent_to == ["a2@example.com"]
    logs = _logs(session)
    assert [r.alumni_id for r in logs] == [2]
    assert logs[0].stage == 1


def test_completed_when_past_last_window(fake_settings, monkeypatch):
    async def batch(emails):  # pragma: no cover - never called
        raise AssertionError("no send when the campaign is complete")

    schedule = _sched(2000, _TODAY - datetime.timedelta(days=30), status="active")
    _patch_run(
        monkeypatch,
        schedules=[schedule],
        recipients=_rcpts([1]),
        logged={},
        batch=batch,
    )

    session = FakeSession()
    summary = asyncio.run(survey_schedule.run_due_schedules(session))

    assert summary.ran[0].stage is None
    assert schedule.status == survey_schedule.STATUS_COMPLETED
    assert _logs(session) == []


def test_rate_limit_midrun_stops_and_leaves_rest(fake_settings, monkeypatch):
    # 250 recipients -> 3 batches (100/100/50). First delivers, second is
    # throttled: we stop, log only the delivered 100, and report retry_after.
    state = {"n": 0}

    async def batch(emails):
        state["n"] += 1
        if state["n"] == 1:
            return (None, None)
        raise survey_email.ResendRateLimited(retry_after=30)

    _patch_run(
        monkeypatch,
        schedules=[_sched(2000, _TODAY)],
        recipients=_rcpts(list(range(1, 251))),
        logged={},
        batch=batch,
    )

    session = FakeSession()
    summary = asyncio.run(survey_schedule.run_due_schedules(session))

    item = summary.ran[0]
    assert item.sent == 100
    assert item.remaining == 150
    assert item.retry_after_seconds == 30
    # Only the delivered batch produced log rows — the un-sent 150 have none.
    assert len(_logs(session)) == 100


# ------------------------------------------------------------- send cap -------


def test_cap_limits_how_many_go_out_this_run(fake_settings, monkeypatch):
    # Budget of 2 with a 5-person cohort: only 2 go out this run, the other 3
    # are left for the next cron.
    sent_to = []

    async def batch(emails):
        sent_to.extend(e["to"][0] for e in emails)
        return (None, None)

    _patch_run(
        monkeypatch,
        schedules=[_sched(2000, _TODAY)],
        recipients=_rcpts([1, 2, 3, 4, 5]),
        logged={},
        batch=batch,
        allowance=2,
    )

    session = FakeSession()
    summary = asyncio.run(survey_schedule.run_due_schedules(session))

    item = summary.ran[0]
    assert item.stage == 0
    assert item.sent == 2
    assert item.remaining == 3
    assert len(sent_to) == 2
    assert len(_logs(session)) == 2


def test_cap_budget_is_shared_across_years(fake_settings, monkeypatch):
    # Two due schedules, shared budget of 3. The first (earliest-scheduled) year
    # drains the budget; the second gets nothing this run.
    async def batch(emails):
        return (None, None)

    older = _sched(2001, _TODAY - datetime.timedelta(days=1))
    newer = _sched(2000, _TODAY)
    _patch_run(
        monkeypatch,
        schedules=[older, newer],  # _load_schedules_due returns them pre-ordered
        recipients=_rcpts([1, 2, 3, 4, 5]),
        logged={},
        batch=batch,
        allowance=3,
    )

    session = FakeSession()
    summary = asyncio.run(survey_schedule.run_due_schedules(session))

    # Only the first year ran — the budget was spent before the second.
    assert [i.graduation_year for i in summary.ran] == [2001]
    assert summary.ran[0].sent == 3
    assert summary.ran[0].remaining == 2


def test_cap_disabled_sends_everything(fake_settings, monkeypatch):
    # allowance=None (cap off) → the whole cohort goes out in one run.
    async def batch(emails):
        return (None, None)

    _patch_run(
        monkeypatch,
        schedules=[_sched(2000, _TODAY)],
        recipients=_rcpts(list(range(1, 151))),  # 150 > any Free-tier day cap
        logged={},
        batch=batch,
        allowance=None,
    )

    session = FakeSession()
    summary = asyncio.run(survey_schedule.run_due_schedules(session))

    assert summary.ran[0].sent == 150
    assert summary.ran[0].remaining == 0


def test_initial_sent_before_reminder_when_cap_delayed(fake_settings, monkeypatch):
    # Day 8 (the 1-week reminder window) but two recipients never got the initial
    # — the run must FINISH the initial, not jump to the reminder.
    sent_to = []

    async def batch(emails):
        sent_to.extend(e["to"][0] for e in emails)
        return (None, None)

    _patch_run(
        monkeypatch,
        schedules=[_sched(2000, _TODAY - datetime.timedelta(days=8))],
        recipients=_rcpts([1, 2, 3]),
        logged={(2000, 0): {1}},  # only alum 1 has had the initial so far
        batch=batch,
        allowance=None,
    )

    session = FakeSession()
    summary = asyncio.run(survey_schedule.run_due_schedules(session))

    item = summary.ran[0]
    assert item.stage == 0  # INITIAL, not the day-8 reminder
    assert sorted(sent_to) == ["a2@example.com", "a3@example.com"]
    assert {r.stage for r in _logs(session)} == {0}


def test_run_allowance_subtracts_usage_and_takes_tighter_budget(
    fake_settings, monkeypatch
):
    async def fake_usage(session):
        return SimpleNamespace(sent_today=30, sent_this_month=1000)

    async def fake_cfg(session):
        return SurveySendConfigItem(
            enabled=True, daily_limit=100, monthly_limit=3000
        )

    monkeypatch.setattr(survey_email, "get_send_usage", fake_usage)
    monkeypatch.setattr(survey_schedule, "get_send_config", fake_cfg)

    # min(100 - 30, 3000 - 1000) = 70
    assert asyncio.run(survey_schedule._run_allowance(FakeSession())) == 70


def test_run_allowance_none_when_cap_disabled(monkeypatch):
    async def fake_cfg(session):
        return SurveySendConfigItem(
            enabled=False, daily_limit=100, monthly_limit=3000
        )

    monkeypatch.setattr(survey_schedule, "get_send_config", fake_cfg)
    assert asyncio.run(survey_schedule._run_allowance(FakeSession())) is None


def test_get_send_config_defaults_when_row_missing():
    session = QueueSession([_Res(one=None)])
    cfg = asyncio.run(survey_schedule.get_send_config(session))
    assert cfg.enabled is True
    assert cfg.daily_limit == 100
    assert cfg.monthly_limit == 3000


def test_update_send_config_updates_existing_row():
    row = SimpleNamespace(
        enabled=True, daily_limit=100, monthly_limit=3000, updated_by_user_id=None
    )
    session = QueueSession([_Res(one=row), _Res(one=row)])
    cfg = asyncio.run(
        survey_schedule.update_send_config(
            session,
            enabled=False,
            daily_limit=500,
            monthly_limit=12000,
            actor_user_id=7,
        )
    )
    assert row.enabled is False
    assert row.daily_limit == 500
    assert row.monthly_limit == 12000
    assert row.updated_by_user_id == 7
    assert cfg.daily_limit == 500
    assert session.commits == 1


# ------------------------------------------------- create / cancel / list -----


class _Res:
    def __init__(self, *, one="__unset__", scalars_all=None, rows=None):
        self._one = one
        self._scalars_all = scalars_all or []
        self._rows = rows or []

    def scalar_one_or_none(self):
        return None if self._one == "__unset__" else self._one

    def scalars(self):
        return SimpleNamespace(all=lambda: self._scalars_all)

    def all(self):
        return self._rows


class QueueSession:
    def __init__(self, results):
        self._q = list(results)
        self.added = []
        self.commits = 0
        self.executed = 0

    async def execute(self, _stmt):
        self.executed += 1
        return self._q.pop(0)

    def add(self, obj):
        self.added.append(obj)

    async def commit(self):
        self.commits += 1


def test_create_schedule_inserts_new():
    sched = _sched(2000, datetime.date(2026, 8, 1))
    session = QueueSession(
        [
            _Res(one=None),  # existence check -> none
            _Res(one=sched),  # get_schedule re-query
            _Res(rows=[]),  # per-stage counts
        ]
    )
    item = asyncio.run(
        survey_schedule.create_schedule(
            session,
            graduation_year=2000,
            start_date=datetime.date(2026, 8, 1),
            actor_user_id=5,
        )
    )
    assert item.graduation_year == 2000
    assert item.status == "scheduled"
    assert session.commits == 1
    added = [a for a in session.added if type(a).__name__ == "SurveySchedule"]
    assert len(added) == 1
    assert added[0].graduation_year == 2000
    assert added[0].created_by_user_id == 5


def test_create_schedule_replaces_existing():
    existing = _sched(2000, datetime.date(2026, 1, 1), status="completed")
    session = QueueSession(
        [
            _Res(one=existing),  # existence check -> found
            _Res(one=existing),  # get_schedule re-query
            _Res(rows=[]),  # per-stage counts
            # The upsert stamped created_by_user_id, so the creator-name lookup
            # runs (it is skipped entirely when no row has a creator).
            _Res(rows=[]),
        ]
    )
    asyncio.run(
        survey_schedule.create_schedule(
            session,
            graduation_year=2000,
            start_date=datetime.date(2026, 9, 1),
            actor_user_id=7,
        )
    )
    # Replacing resets state + start date on the existing row (no new insert).
    assert existing.status == "scheduled"
    assert existing.start_date == datetime.date(2026, 9, 1)
    assert not [a for a in session.added if type(a).__name__ == "SurveySchedule"]


def test_cancel_schedule_sets_cancelled():
    existing = _sched(2000, datetime.date(2026, 8, 1), status="active")
    session = QueueSession([_Res(one=existing), _Res(one=existing), _Res(rows=[])])
    item = asyncio.run(survey_schedule.cancel_schedule(session, 2000))
    assert existing.status == "cancelled"
    assert item.status == "cancelled"


def test_cancel_missing_schedule_returns_none():
    session = QueueSession([_Res(one=None)])
    assert asyncio.run(survey_schedule.cancel_schedule(session, 1999)) is None


def test_create_schedules_bulk_inserts_and_updates_many():
    from app.schemas.survey import SurveyScheduleCreateRequest

    existing = _sched(2001, datetime.date(2026, 1, 1), status="completed")
    session = QueueSession(
        [
            _Res(one=None),  # year 2000 existence -> new
            _Res(one=existing),  # year 2001 existence -> found (update)
            _Res(one=None),  # year 2002 existence -> new
            # list_schedules re-query: all rows + per-stage counts
            _Res(scalars_all=[_sched(2000, datetime.date(2026, 8, 1))]),
            _Res(rows=[]),
        ]
    )
    items = [
        SurveyScheduleCreateRequest(
            graduation_year=2000, start_date=datetime.date(2026, 8, 1)
        ),
        SurveyScheduleCreateRequest(
            graduation_year=2001, start_date=datetime.date(2026, 9, 1)
        ),
        SurveyScheduleCreateRequest(
            graduation_year=2002, start_date=datetime.date(2026, 10, 1)
        ),
    ]
    result = asyncio.run(
        survey_schedule.create_schedules_bulk(
            session, items=items, actor_user_id=5
        )
    )
    assert isinstance(result, list)
    # One commit for the whole batch (not one per year).
    assert session.commits == 1
    # The two brand-new years were inserted; the existing one was updated in place.
    added = [a for a in session.added if type(a).__name__ == "SurveySchedule"]
    assert sorted(a.graduation_year for a in added) == [2000, 2002]
    assert existing.status == "scheduled"
    assert existing.start_date == datetime.date(2026, 9, 1)


def test_create_schedules_bulk_empty_is_noop():
    session = QueueSession(
        [
            _Res(scalars_all=[]),  # list_schedules: no rows
            _Res(rows=[]),  # per-stage counts
        ]
    )
    result = asyncio.run(
        survey_schedule.create_schedules_bulk(
            session, items=[], actor_user_id=5
        )
    )
    assert result == []
    # No schedule rows were touched.
    assert not [a for a in session.added if type(a).__name__ == "SurveySchedule"]


def test_create_schedules_bulk_dedupes_duplicate_year():
    from app.schemas.survey import SurveyScheduleCreateRequest

    session = QueueSession(
        [
            _Res(one=None),  # single existence check for the one deduped year
            _Res(scalars_all=[_sched(2000, datetime.date(2026, 9, 1))]),
            _Res(rows=[]),
        ]
    )
    items = [
        SurveyScheduleCreateRequest(
            graduation_year=2000, start_date=datetime.date(2026, 8, 1)
        ),
        SurveyScheduleCreateRequest(
            graduation_year=2000, start_date=datetime.date(2026, 9, 1)
        ),
    ]
    asyncio.run(
        survey_schedule.create_schedules_bulk(
            session, items=items, actor_user_id=5
        )
    )
    # The duplicate year collapses to ONE inserted row, and last-one-wins picks
    # the later start date.
    added = [a for a in session.added if type(a).__name__ == "SurveySchedule"]
    assert len(added) == 1
    assert added[0].graduation_year == 2000
    assert added[0].start_date == datetime.date(2026, 9, 1)


def test_list_schedules_includes_stage_counts():
    s1 = _sched(2001, datetime.date(2026, 5, 1), status="active")
    session = QueueSession(
        [
            _Res(scalars_all=[s1]),
            _Res(rows=[(2001, 0, 3), (2001, 1, 1)]),
        ]
    )
    items = asyncio.run(survey_schedule.list_schedules(session))
    assert len(items) == 1
    assert items[0].sent_initial == 3
    assert items[0].sent_reminder_1 == 1
    assert items[0].sent_reminder_2 == 0


# ------------------------------------------------------- who started it -------


def test_list_schedules_resolves_creator_names_in_one_query():
    # Two schedules by two different users: the name lookup must be ONE query for
    # the whole list (the engineer console lists every year — a per-row lookup
    # would be an N+1).
    session = QueueSession(
        [
            _Res(
                scalars_all=[
                    _sched(2001, datetime.date(2026, 5, 1), created_by=7),
                    _sched(2000, datetime.date(2026, 5, 1), created_by=8),
                ]
            ),
            _Res(rows=[]),  # per-stage counts
            _Res(
                rows=[
                    (7, "Jake", "Gunnell", "jake@byu.edu"),
                    (8, None, None, "tanya@byu.edu"),  # no name on file
                ]
            ),
        ]
    )
    items = asyncio.run(survey_schedule.list_schedules(session))
    # Full name when present, email as the fallback — never the internal user id.
    assert [i.created_by for i in items] == ["Jake Gunnell", "tanya@byu.edu"]
    assert session.executed == 3  # schedules + counts + one creator lookup


def test_list_schedules_skips_creator_query_when_none_recorded():
    session = QueueSession(
        [
            _Res(scalars_all=[_sched(2000, datetime.date(2026, 5, 1))]),
            _Res(rows=[]),
        ]
    )
    items = asyncio.run(survey_schedule.list_schedules(session))
    assert items[0].created_by is None
    assert session.executed == 2  # no creator lookup at all


# --------------------------------------------------------- pause / resume -----


class _CaptureSession(QueueSession):
    """Keeps the last statement it was handed so it can be compiled and
    inspected — the cheapest way to PROVE what the SQL filters on, rather than
    re-asserting the constant it was built from."""

    def __init__(self, results=None):
        super().__init__(results or [_Res(scalars_all=[])])
        self.stmt = None

    async def execute(self, stmt):
        self.stmt = stmt
        return await super().execute(stmt)


def test_load_schedules_due_excludes_paused():
    # The whole pause mechanism rests on this one filter: a `paused` row must not
    # be selectable by the cron.
    session = _CaptureSession()
    asyncio.run(survey_schedule._load_schedules_due(session, _TODAY))
    sql = str(session.stmt.compile(compile_kwargs={"literal_binds": True}))
    assert "'scheduled'" in sql
    assert "'active'" in sql
    assert "'paused'" not in sql
    assert survey_schedule.STATUS_PAUSED not in survey_schedule._RUNNABLE_STATUSES


def _pause_session(sched):
    """Query queue for one pause/resume: the row lookup, then get_schedule's
    re-query + per-stage counts (+ the creator lookup when a creator is set)."""
    results = [_Res(one=sched), _Res(one=sched), _Res(rows=[])]
    if sched.created_by_user_id is not None:
        results.append(_Res(rows=[]))
    return QueueSession(results)


def test_pause_sets_paused_and_remembers_previous_status():
    sched = _sched(2000, datetime.date(2026, 7, 20), status="active")
    session = _pause_session(sched)
    item = asyncio.run(
        survey_schedule.pause_schedule(session, 2000, actor_user_id=4)
    )
    assert sched.status == "paused"
    assert sched.paused_from_status == "active"  # what resume must restore
    assert sched.paused_at is not None
    assert item.status == "paused"
    assert item.paused_at is not None  # surfaced to the console
    assert session.commits == 1
    audit = _audits(session)[0]
    assert audit.action_type == "pause_survey_schedule"
    assert audit.entity_type == "survey_campaign"
    assert audit.entity_id == 2000
    assert audit.user_id == 4


def test_pause_scheduled_campaign_remembers_scheduled():
    sched = _sched(2000, datetime.date(2026, 9, 1), status="scheduled")
    asyncio.run(
        survey_schedule.pause_schedule(_pause_session(sched), 2000, actor_user_id=1)
    )
    assert sched.paused_from_status == "scheduled"


def test_pause_missing_schedule_returns_none():
    session = QueueSession([_Res(one=None)])
    assert (
        asyncio.run(survey_schedule.pause_schedule(session, 1999, actor_user_id=1))
        is None
    )


def test_pause_already_paused_is_noop():
    # Idempotent: two admins pressing pause together must not fight over
    # `paused_at` — the second press must leave the first stamp alone, or the
    # resume shift would be measured from the wrong moment.
    stamp = _utc(datetime.date(2026, 7, 25))
    sched = _sched(
        2000,
        datetime.date(2026, 7, 20),
        status="paused",
        paused_at=stamp,
        paused_from_status="active",
    )
    session = _pause_session(sched)
    item = asyncio.run(
        survey_schedule.pause_schedule(session, 2000, actor_user_id=1)
    )
    assert item.status == "paused"
    assert sched.paused_at == stamp
    assert sched.paused_from_status == "active"
    assert session.commits == 0
    assert _audits(session) == []


@pytest.mark.parametrize("status", ["completed", "cancelled"])
def test_pause_rejects_non_runnable(status):
    from app.core.errors import ConflictError

    sched = _sched(2000, datetime.date(2026, 5, 1), status=status)
    session = QueueSession([_Res(one=sched)])
    with pytest.raises(ConflictError):
        asyncio.run(survey_schedule.pause_schedule(session, 2000, actor_user_id=1))
    assert sched.status == status
    assert session.commits == 0


# ---- the resume shift (`_resumed_start_date`) -------------------------------


def test_resumed_start_date_shifts_in_flight_campaign():
    # Paused on day 3, resumed 10 days later -> start moves 10 days so `elapsed`
    # is 3 again, exactly where the cadence stopped.
    start = datetime.date(2026, 7, 1)
    shifted = survey_schedule._resumed_start_date(
        start_date=start,
        paused_on=datetime.date(2026, 7, 4),
        resumed_on=datetime.date(2026, 7, 14),
    )
    assert shifted == datetime.date(2026, 7, 11)
    assert (datetime.date(2026, 7, 14) - shifted).days == 3


def test_resumed_start_date_keeps_original_when_never_started():
    # Paused before it began and resumed while the start is STILL in the future:
    # nothing had elapsed, so there is no cadence to shift.
    start = datetime.date(2026, 8, 20)
    assert (
        survey_schedule._resumed_start_date(
            start_date=start,
            paused_on=datetime.date(2026, 8, 1),
            resumed_on=datetime.date(2026, 8, 10),
        )
        == start
    )


def test_resumed_start_date_clamps_to_today_when_start_passed_during_pause():
    # Never started, but the start date went by while it was paused. Honouring it
    # would land at elapsed=10 and fire the 1-week reminder the day after the
    # initial — so it begins today with a full, correct cadence.
    assert survey_schedule._resumed_start_date(
        start_date=datetime.date(2026, 8, 10),
        paused_on=datetime.date(2026, 8, 1),
        resumed_on=datetime.date(2026, 8, 20),
    ) == datetime.date(2026, 8, 20)


def test_resumed_start_date_branches_agree_on_the_boundary():
    # paused_on == start_date: "shift by the pause" and "start today" give the
    # same answer, so which branch takes it is not load-bearing.
    day = datetime.date(2026, 8, 10)
    assert survey_schedule._resumed_start_date(
        start_date=day, paused_on=day, resumed_on=datetime.date(2026, 8, 18)
    ) == datetime.date(2026, 8, 18)


def test_resumed_start_date_without_a_stamp_leaves_it_alone():
    # A hand-edited `paused` row with no `paused_at`: no duration to shift by, so
    # nothing is invented.
    start = datetime.date(2026, 7, 1)
    assert (
        survey_schedule._resumed_start_date(
            start_date=start, paused_on=None, resumed_on=datetime.date(2026, 7, 14)
        )
        == start
    )


# ---- resume_schedule --------------------------------------------------------


def test_resume_shifts_start_and_restores_status(fake_settings):
    # _TODAY = 2026-07-29. Paused on 2026-07-04 (day 3 of a 2026-07-01 campaign),
    # resumed 25 days later -> start_date moves to 2026-07-26, elapsed 3 again.
    sched = _sched(
        2000,
        datetime.date(2026, 7, 1),
        status="paused",
        paused_at=_utc(datetime.date(2026, 7, 4)),
        paused_from_status="active",
    )
    session = _pause_session(sched)
    item = asyncio.run(
        survey_schedule.resume_schedule(session, 2000, actor_user_id=4)
    )
    assert sched.start_date == datetime.date(2026, 7, 26)
    assert (_TODAY - sched.start_date).days == 3
    assert sched.status == "active"  # restored, not guessed
    assert sched.paused_at is None
    assert sched.paused_from_status is None
    assert item.status == "active"
    assert item.paused_at is None
    assert session.commits == 1
    audit = _audits(session)[0]
    assert audit.action_type == "resume_survey_schedule"
    assert audit.entity_id == 2000
    assert "start_date=2026-07-26" in audit.new_value


def test_resume_restores_scheduled_for_a_campaign_that_never_started(fake_settings):
    # Paused while still in the future and resumed before its start date: the
    # start date is untouched and it goes back to `scheduled`, not `active`.
    sched = _sched(
        2000,
        datetime.date(2026, 8, 20),
        status="paused",
        paused_at=_utc(datetime.date(2026, 7, 20)),
        paused_from_status="scheduled",
    )
    asyncio.run(
        survey_schedule.resume_schedule(_pause_session(sched), 2000, actor_user_id=1)
    )
    assert sched.start_date == datetime.date(2026, 8, 20)
    assert sched.status == "scheduled"


def test_resume_falls_back_to_scheduled_without_a_recorded_status(fake_settings):
    sched = _sched(
        2000,
        datetime.date(2026, 7, 1),
        status="paused",
        paused_at=_utc(datetime.date(2026, 7, 4)),
        paused_from_status=None,  # never paused through the service
    )
    asyncio.run(
        survey_schedule.resume_schedule(_pause_session(sched), 2000, actor_user_id=1)
    )
    assert sched.status == "scheduled"


def test_resume_already_running_is_noop(fake_settings):
    sched = _sched(2000, datetime.date(2026, 7, 1), status="active")
    session = _pause_session(sched)
    item = asyncio.run(
        survey_schedule.resume_schedule(session, 2000, actor_user_id=1)
    )
    assert item.status == "active"
    assert sched.start_date == datetime.date(2026, 7, 1)  # not shifted
    assert session.commits == 0


@pytest.mark.parametrize("status", ["completed", "cancelled"])
def test_resume_rejects_completed_and_cancelled(status, fake_settings):
    # Cancel is terminal by design — resume must not become a back door round it.
    from app.core.errors import ConflictError

    sched = _sched(2000, datetime.date(2026, 5, 1), status=status)
    session = QueueSession([_Res(one=sched)])
    with pytest.raises(ConflictError):
        asyncio.run(survey_schedule.resume_schedule(session, 2000, actor_user_id=1))
    assert sched.status == status


def test_resume_missing_schedule_returns_none(fake_settings):
    session = QueueSession([_Res(one=None)])
    assert (
        asyncio.run(survey_schedule.resume_schedule(session, 1999, actor_user_id=1))
        is None
    )


# ---- the point of the whole design: the stage survives a pause --------------


def test_pause_does_not_eat_the_rest_of_the_campaign(fake_settings, monkeypatch):
    """Pause on day 3, resume 25 days later — the campaign must still be on the
    INITIAL stage, not silently `completed`.

    Without the start-date shift, `elapsed` at resume would be 28 days, past the
    2-week reminder window, so the very next cron run would mark the campaign
    complete and neither reminder would ever go out."""
    original_start = datetime.date(2026, 7, 1)
    # Sanity check on what the naive behaviour would have been.
    assert survey_schedule._stage_for((_TODAY - original_start).days) is None

    sched = _sched(
        2000,
        original_start,
        status="paused",
        paused_at=_utc(datetime.date(2026, 7, 4)),
        paused_from_status="active",
    )
    asyncio.run(
        survey_schedule.resume_schedule(_pause_session(sched), 2000, actor_user_id=1)
    )
    assert survey_schedule._stage_for((_TODAY - sched.start_date).days) == 0

    # And the cron agrees: it sends the initial rather than completing the run.
    async def batch(emails):
        return (None, None)

    _patch_run(
        monkeypatch,
        schedules=[sched],
        recipients=_rcpts([1, 2]),
        logged={},
        batch=batch,
    )
    summary = asyncio.run(survey_schedule.run_due_schedules(FakeSession()))
    assert summary.ran[0].stage == survey_schedule.STAGE_INITIAL
    assert summary.ran[0].sent == 2
    assert sched.status == survey_schedule.STATUS_ACTIVE

    # The reminder still lands a week after the resumed day 3, not immediately.
    assert survey_schedule._stage_for(
        (_TODAY + datetime.timedelta(days=4) - sched.start_date).days
    ) == survey_schedule.STAGE_REMINDER_1


def test_half_finished_initial_resumes_from_the_send_log(fake_settings, monkeypatch):
    """A partially-sent initial is tracked in `survey_send_log`, not in dates, so
    the pause cannot lose it — the un-emailed recipients still get the initial
    after a resume, and the already-emailed ones are not re-emailed."""
    sched = _sched(
        2000,
        datetime.date(2026, 7, 1),
        status="paused",
        paused_at=_utc(datetime.date(2026, 7, 4)),
        paused_from_status="active",
    )
    asyncio.run(
        survey_schedule.resume_schedule(_pause_session(sched), 2000, actor_user_id=1)
    )

    sent_to = []

    async def batch(emails):
        sent_to.extend(e["to"][0] for e in emails)
        return (None, None)

    _patch_run(
        monkeypatch,
        schedules=[sched],
        recipients=_rcpts([1, 2, 3]),
        logged={(2000, 0): {1}},  # alum 1 got the initial before the pause
        batch=batch,
    )
    summary = asyncio.run(survey_schedule.run_due_schedules(FakeSession()))
    assert summary.ran[0].stage == survey_schedule.STAGE_INITIAL
    assert sorted(sent_to) == ["a2@example.com", "a3@example.com"]


# ------------------------------------------------------------- pause all ------


def test_pause_all_pauses_runnable_and_reports_years():
    session = QueueSession([_Res(scalars_all=[1901, 1900])])
    result = asyncio.run(
        survey_schedule.pause_all_schedules(session, actor_user_id=1)
    )
    assert result.paused == 2
    assert result.graduation_years == [1900, 1901]  # sorted for a stable report
    assert session.commits == 1
    audit = _audits(session)[0]
    assert audit.action_type == "pause_all_survey_schedules"
    assert audit.entity_type == "survey_campaign"
    assert audit.entity_id is None
    assert audit.user_id == 1
    assert "paused=2" in audit.new_value
    assert "1900,1901" in audit.new_value


def test_pause_all_is_idempotent_when_nothing_running():
    # A second press finds nothing runnable (the first press's rows are already
    # `paused`, which the UPDATE's WHERE excludes) and reports 0 — leaving the
    # original `paused_at` stamps intact so resume still shifts correctly.
    session = QueueSession([_Res(scalars_all=[])])
    result = asyncio.run(
        survey_schedule.pause_all_schedules(session, actor_user_id=1)
    )
    assert result.paused == 0
    assert result.graduation_years == []
    assert _audits(session)[0].new_value == "paused=0 grad_years=none"


def test_pause_all_statement_only_touches_runnable_and_captures_old_status():
    # The single UPDATE ... RETURNING must (a) filter to scheduled/active and
    # (b) copy the PRE-update status into paused_from_status — Postgres evaluates
    # every SET expression against the old row, so this is safe in one statement.
    session = _CaptureSession()
    asyncio.run(survey_schedule.pause_all_schedules(session, actor_user_id=1))
    sql = str(session.stmt.compile(compile_kwargs={"literal_binds": True}))
    assert "IN ('scheduled', 'active')" in sql.replace('"', "")
    assert "paused_from_status=survey_schedule.status" in sql.replace(" =", "=")


# ------------------------------------------------------------- cancel all -----


def test_cancel_all_cancels_runnable_and_reports_years():
    session = QueueSession([_Res(scalars_all=[1901, 1900])])
    result = asyncio.run(
        survey_schedule.cancel_all_schedules(session, actor_user_id=1)
    )
    assert result.cancelled == 2
    assert result.graduation_years == [1900, 1901]  # sorted for a stable report
    assert session.commits == 1
    audit = _audits(session)[0]
    assert audit.action_type == "cancel_all_survey_schedules"
    assert audit.entity_type == "survey_campaign"
    assert audit.user_id == 1
    assert "cancelled=2" in audit.new_value
    assert "1900,1901" in audit.new_value


def test_cancel_all_is_idempotent_when_nothing_active():
    session = QueueSession([_Res(scalars_all=[])])
    result = asyncio.run(
        survey_schedule.cancel_all_schedules(session, actor_user_id=1)
    )
    assert result.cancelled == 0
    assert result.graduation_years == []
    # A no-op press is still audited — reaching for the kill switch is itself an
    # intervention worth tracing.
    audit = _audits(session)[0]
    assert audit.new_value == "cancelled=0 grad_years=none"


# ------------------------------------------------- cancel-all route (gating) ---


def _engineer_ctx(*roles, user_id=1):
    import uuid

    from app.schemas.auth import UserContext

    return UserContext(
        user_id=user_id,
        auth_user_id=uuid.UUID("11111111-1111-1111-1111-111111111111"),
        roles=list(roles),
    )


def _post(path, session, ctx=None):
    """POST ``path`` against ``session``, as ``ctx`` (or unauthenticated when ctx
    is None). Returns the response."""
    from fastapi.testclient import TestClient

    from app.api.dependencies.auth import get_current_db_user, get_permission_config
    from app.core.capabilities import DEFAULT_GRANTS
    from app.core.database import get_session
    from app.main import app

    async def _session():
        yield session

    app.dependency_overrides[get_session] = _session
    # Pin the role→capability map to the shipped defaults so the fake session's
    # query queue only has to serve the route's own reads, not the auth layer's.
    app.dependency_overrides[get_permission_config] = lambda: dict(DEFAULT_GRANTS)
    if ctx is not None:
        app.dependency_overrides[get_current_db_user] = lambda: ctx
    try:
        with TestClient(app) as test_client:
            return test_client.post(path)
    finally:
        app.dependency_overrides.pop(get_session, None)
        app.dependency_overrides.pop(get_permission_config, None)
        app.dependency_overrides.pop(get_current_db_user, None)


def _cancel_all(session, ctx=None):
    return _post("/survey/schedules/cancel-all", session, ctx)


def test_cancel_all_route_requires_auth():
    resp = _cancel_all(None)
    assert resp.status_code == 401


def test_cancel_all_route_forbidden_below_engineer():
    # super_admin is the highest NON-engineer role and still can't blanket-stop
    # every cohort — the per-year cancel stays their tool.
    resp = _cancel_all(None, _engineer_ctx("super_admin"))
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "forbidden"


def test_cancel_all_route_engineer_cancels_and_reports():
    session = QueueSession([_Res(scalars_all=[1900, 1901])])
    resp = _cancel_all(session, _engineer_ctx("engineer"))
    assert resp.status_code == 200
    assert resp.json() == {"cancelled": 2, "graduation_years": [1900, 1901]}
    assert _audits(session)[0].action_type == "cancel_all_survey_schedules"


# ------------------------------------------- pause routes (gating + status) ---


def test_pause_all_route_requires_auth():
    assert _post("/survey/schedules/pause-all", None).status_code == 401


def test_pause_all_route_forbidden_below_engineer():
    # Same gate as cancel-all: a blanket stop of every cohort is a maintenance
    # action whatever its reversibility, so super_admin still can't do it.
    resp = _post("/survey/schedules/pause-all", None, _engineer_ctx("super_admin"))
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "forbidden"


def test_pause_all_route_engineer_pauses_and_reports():
    session = QueueSession([_Res(scalars_all=[1900, 1901])])
    resp = _post(
        "/survey/schedules/pause-all", session, _engineer_ctx("engineer")
    )
    assert resp.status_code == 200
    assert resp.json() == {"paused": 2, "graduation_years": [1900, 1901]}
    assert _audits(session)[0].action_type == "pause_all_survey_schedules"


def test_per_year_pause_route_requires_auth():
    assert _post("/survey/schedules/2000/pause", None).status_code == 401


def test_per_year_pause_route_forbidden_for_view_only():
    # Per-year pause is full-access, matching the per-year cancel next to it.
    resp = _post("/survey/schedules/2000/pause", None, _engineer_ctx("view_only"))
    assert resp.status_code == 403


def test_per_year_pause_route_pauses_for_full_access():
    sched = _sched(2000, datetime.date(2026, 7, 20), status="active")
    resp = _post(
        "/survey/schedules/2000/pause", _pause_session(sched), _engineer_ctx("full_access")
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "paused"
    assert resp.json()["paused_at"] is not None


def test_per_year_pause_route_404s_for_unknown_year():
    session = QueueSession([_Res(one=None)])
    resp = _post("/survey/schedules/2000/pause", session, _engineer_ctx("full_access"))
    assert resp.status_code == 404


def test_per_year_pause_route_409s_on_cancelled():
    session = QueueSession(
        [_Res(one=_sched(2000, datetime.date(2026, 5, 1), status="cancelled"))]
    )
    resp = _post("/survey/schedules/2000/pause", session, _engineer_ctx("full_access"))
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "conflict"


def test_per_year_resume_route_resumes_for_full_access():
    sched = _sched(
        2000,
        datetime.date(2026, 7, 1),
        status="paused",
        paused_at=_utc(datetime.date(2026, 7, 4)),
        paused_from_status="active",
    )
    resp = _post(
        "/survey/schedules/2000/resume", _pause_session(sched), _engineer_ctx("full_access")
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "active"
    assert resp.json()["paused_at"] is None


def test_per_year_resume_route_forbidden_for_view_only():
    resp = _post("/survey/schedules/2000/resume", None, _engineer_ctx("view_only"))
    assert resp.status_code == 403


# --------------------------------------------------------------- cron route ---


@pytest.fixture
def client():
    from fastapi.testclient import TestClient

    from app.core.database import get_session
    from app.main import app

    async def _no_db_session():
        yield None

    app.dependency_overrides[get_session] = _no_db_session
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


def _set_cron_secret(monkeypatch, value):
    import app.api.routes.survey as survey_routes

    monkeypatch.setattr(
        survey_routes, "get_settings", lambda: SimpleNamespace(cron_secret=value)
    )


def _stub_run(monkeypatch, sink):
    async def fake_run(session, actor_user_id=None):
        sink.append(True)
        return SurveyScheduleRunSummary(ran=[])

    monkeypatch.setattr(survey_schedule, "run_due_schedules", fake_run)


def test_cron_rejects_missing_secret(client, monkeypatch):
    ran = []
    _set_cron_secret(monkeypatch, "topsecret")
    _stub_run(monkeypatch, ran)
    resp = client.post("/survey/cron/run")  # no Authorization header
    assert resp.status_code == 401
    assert ran == []


def test_cron_rejects_wrong_secret(client, monkeypatch):
    ran = []
    _set_cron_secret(monkeypatch, "topsecret")
    _stub_run(monkeypatch, ran)
    resp = client.post(
        "/survey/cron/run", headers={"Authorization": "Bearer nope"}
    )
    assert resp.status_code == 401
    assert ran == []


def test_cron_rejects_when_secret_unset(client, monkeypatch):
    ran = []
    _set_cron_secret(monkeypatch, None)  # CRON_SECRET not configured
    _stub_run(monkeypatch, ran)
    resp = client.post(
        "/survey/cron/run", headers={"Authorization": "Bearer anything"}
    )
    assert resp.status_code == 401
    assert ran == []


def test_cron_runs_with_correct_secret(client, monkeypatch):
    ran = []
    _set_cron_secret(monkeypatch, "topsecret")
    _stub_run(monkeypatch, ran)
    resp = client.post(
        "/survey/cron/run", headers={"Authorization": "Bearer topsecret"}
    )
    assert resp.status_code == 200
    assert resp.json() == {"ran": []}
    assert ran == [True]


def test_cron_accepts_get_from_vercel(client, monkeypatch):
    # Vercel Cron invokes the path with a GET — it must work too.
    ran = []
    _set_cron_secret(monkeypatch, "topsecret")
    _stub_run(monkeypatch, ran)
    resp = client.get(
        "/survey/cron/run", headers={"Authorization": "Bearer topsecret"}
    )
    assert resp.status_code == 200
    assert ran == [True]
