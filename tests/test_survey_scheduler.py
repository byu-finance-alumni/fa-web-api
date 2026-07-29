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

from app.schemas.survey import SurveyScheduleRunSummary
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


def _sched(year, start, status="scheduled"):
    return SimpleNamespace(
        survey_schedule_id=year,
        graduation_year=year,
        start_date=start,
        status=status,
        last_run_at=None,
        created_at=None,
    )


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


def _patch_run(monkeypatch, *, schedules, recipients, logged, batch):
    async def fake_due(session, today):
        return schedules

    async def fake_recipients(session, year):
        return recipients

    async def fake_logged(session, year, stage):
        return set(logged.get((year, stage), set()))

    monkeypatch.setattr(survey_schedule, "_load_schedules_due", fake_due)
    monkeypatch.setattr(survey_email, "_load_recipients", fake_recipients)
    monkeypatch.setattr(survey_schedule, "_logged_alumni_ids", fake_logged)
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

    async def execute(self, _stmt):
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
