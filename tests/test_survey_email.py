"""Tests for the survey-email send service + route (no real network / DB)."""

import asyncio
import uuid
from types import SimpleNamespace

import pytest

from app.services import survey_email
from app.services.survey_email import (
    Recipient,
    make_survey_token,
    render_survey_email,
    verify_survey_token,
)
from tests.survey_fakes import SendLogSession
from tests.survey_fakes import audits as _audits


class _FakeSettings:
    survey_token_secret = "unit-test-secret"
    survey_from_email = "test@jakegunnell.com"
    survey_from_name = "BYU Finance Alumni"
    survey_app_base_url = "https://finance.alumni.byu.edu"
    resend_api_key = "re_test_key"
    survey_usage_baseline_at = None
    survey_usage_baseline_today = 0
    survey_usage_baseline_month = 0


@pytest.fixture
def fake_settings(monkeypatch):
    settings = _FakeSettings()
    monkeypatch.setattr(survey_email, "get_settings", lambda: settings)
    return settings


def _recipients(n: int) -> list[Recipient]:
    return [
        Recipient(
            i,
            f"Alum{i}",
            f"alum{i}@example.com",
            (("Company", "Emp"), ("Title", "T")),
        )
        for i in range(1, n + 1)
    ]


# --------------------------------------------------------------- tokens ------


def test_token_roundtrip(fake_settings):
    token = make_survey_token(42, 1900)
    assert verify_survey_token(token) == 42


def test_token_tamper_rejected(fake_settings):
    token = make_survey_token(42, 1900)
    tampered = ("Z" if token[0] != "Z" else "Y") + token[1:]
    assert verify_survey_token(tampered) is None


def test_token_garbage_rejected(fake_settings):
    assert verify_survey_token("not-a-token") is None
    assert verify_survey_token("") is None


# --------------------------------------------------------------- render ------


def test_render_email_has_greeting_info_and_link(fake_settings):
    r = Recipient(
        1,
        "Jordan",
        "jordan@example.com",
        (("Company", "Goldman Sachs"), ("Title", "Analyst")),
    )
    subject, html, text = render_survey_email(r, "https://x.test/survey/abc123")
    assert subject
    assert "Hello Jordan," in text
    assert "Goldman Sachs" in text and "Goldman Sachs" in html
    assert "https://x.test/survey/abc123" in html
    assert "Tanya Harmon & Amy Densley" in text


# ------------------------------------------------------------- send flow -----


class FakeSession(SendLogSession):
    """The manual send now reads (the year's schedule, the send log) and WRITES
    claim rows, so its session has to be a real-ish one."""

    @property
    def committed(self):
        return self.commits


def test_dry_run_sends_nothing(fake_settings, monkeypatch):
    calls = []

    async def fake_load(session, year):
        return _recipients(3)

    async def fake_batch(emails):
        calls.append(emails)
        return (None, None)

    monkeypatch.setattr(survey_email, "_load_recipients", fake_load)
    monkeypatch.setattr(survey_email, "_send_batch", fake_batch)

    session = FakeSession()
    result = asyncio.run(
        survey_email.send_campaign(
            session, graduation_year=1900, actor_user_id=1, dry_run=True
        )
    )
    assert result.prepared == 3
    assert result.sent == 0
    assert calls == []  # Resend never called on a dry run
    assert session.committed == 1
    assert len(session.added) == 1  # one audit row


def test_live_send_calls_resend(fake_settings, monkeypatch):
    calls = []

    async def fake_load(session, year):
        return _recipients(2)

    async def fake_batch(emails):
        calls.append(emails)
        return (None, None)

    monkeypatch.setattr(survey_email, "_load_recipients", fake_load)
    monkeypatch.setattr(survey_email, "_send_batch", fake_batch)

    session = FakeSession()
    result = asyncio.run(
        survey_email.send_campaign(
            session, graduation_year=1900, actor_user_id=1, dry_run=False
        )
    )
    assert result.sent == 2
    assert len(calls) == 1 and len(calls[0]) == 2


def test_limit_caps_recipients(fake_settings, monkeypatch):
    async def fake_load(session, year):
        return _recipients(10)

    async def fake_batch(emails):
        return (None, None)

    monkeypatch.setattr(survey_email, "_load_recipients", fake_load)
    monkeypatch.setattr(survey_email, "_send_batch", fake_batch)

    session = FakeSession()
    result = asyncio.run(
        survey_email.send_campaign(
            session, graduation_year=1900, actor_user_id=1, dry_run=True, limit=4
        )
    )
    assert result.prepared == 4
    assert result.remaining == 6


def test_send_stops_and_reports_retry_after_on_429(fake_settings, monkeypatch):
    # Resend rate-limits mid-send -> we stop, report what got sent, how many
    # remain, and Resend's retry-after. The limit comes from Resend, not config.
    #
    # The CAP IS SWITCHED OFF here (#417), which is now the only way 250 emails
    # can be attempted in one call: the send budget is enforced inside
    # `send_survey_stage`, and the default 100/day would otherwise clamp this
    # send to 100 and it would never reach the second batch. That clamp is the
    # subject of `test_survey_send_budget.py`; what is under test here is
    # Resend's own throttle, which is a different brake and still the real one.
    from app.services import survey_schedule

    async def no_cap(session):
        return None

    monkeypatch.setattr(survey_schedule, "_run_allowance", no_cap)

    async def fake_load(session, year):
        return _recipients(250)  # 3 batches: 100, 100, 50

    state = {"n": 0}

    async def fake_batch(emails):
        state["n"] += 1
        if state["n"] == 1:
            return (None, None)  # first batch delivers
        raise survey_email.ResendRateLimited(retry_after=42)  # then throttled

    monkeypatch.setattr(survey_email, "_load_recipients", fake_load)
    monkeypatch.setattr(survey_email, "_send_batch", fake_batch)

    session = FakeSession()
    result = asyncio.run(
        survey_email.send_campaign(
            session, graduation_year=1900, actor_user_id=1, dry_run=False
        )
    )
    assert result.sent == 100  # only the first batch went out
    assert result.retry_after_seconds == 42
    assert result.remaining == 150  # 250 - 100
    # Exactly the delivered 100 are recorded: the throttled batch's claim was
    # RELEASED (a 429 means Resend queued nothing), so those 100 are still owed.
    assert len(session.send_log) == 100
    assert {stage for _y, _a, stage, _c in session.send_log} == {0}
    # The audit row is still written — and, unlike before, so is every claim,
    # each committed before its own send rather than all at the end.
    assert session.committed >= 1
    # Two audit rows now: the send, and the campaign this throttled PARTIAL send
    # left behind (#405). 100 alumni really were emailed, so the reminders they
    # are owed need a campaign to fire from — and the 150 the 429 stopped are
    # picked up by the same campaign's stage 0 on the next cron run, because
    # `select_stage_targets` drains the initial before it looks at a reminder.
    assert [a.action_type for a in _audits(session)] == [
        "send_survey",
        "create_survey_schedule",
    ]
    assert result.campaign_created is True


# ------------------------------------------------------ sendable email -------


def test_sendable_email_gate():
    from app.services.survey_email import _is_sendable_email

    assert _is_sendable_email("gunnjake@byu.edu")
    assert _is_sendable_email("jake@jakegunnell.com")
    # Reserved / placeholder / malformed -> not sendable.
    assert not _is_sendable_email("REPLACE_WITH_TANYA_EMAIL@example.com")
    assert not _is_sendable_email("someone@example.org")
    assert not _is_sendable_email("no-at-sign")
    assert not _is_sendable_email("@byu.edu")
    assert not _is_sendable_email("x@localhost")
    assert not _is_sendable_email(None)


# --------------------------------------------------------- grad years --------


class _Result:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return self._rows


class ExecSession:
    def __init__(self, rows):
        self._rows = rows

    async def execute(self, stmt):
        return _Result(self._rows)


class QueueSession:
    """Returns a queued result per ``execute`` call, so a service that runs more
    than one query gets a distinct result set for each."""

    def __init__(self, result_sets):
        self._queue = list(result_sets)

    async def execute(self, stmt):
        return _Result(self._queue.pop(0))


def test_list_graduation_years_shape():
    # Three executes now: year counts, distinct-responder counts, then the
    # per-year unreachable counts (#392). The shape assertion only checks the
    # year/total columns, so empty rows for the latter two are fine.
    session = QueueSession([[(2024, 5), (1900, 3)], [], []])
    result = asyncio.run(survey_email.list_graduation_years(session))
    assert [(g.graduation_year, g.total_alumni) for g in result] == [(2024, 5), (1900, 3)]


def test_resurvey_cutoff_is_about_a_year_ago():
    # Alumni who replied on/after this cutoff are skipped by _load_recipients and
    # counted as "responded" — the annual re-survey window.
    import datetime

    from app.services.survey_email import _RESURVEY_INTERVAL_DAYS, _resurvey_cutoff

    assert _RESURVEY_INTERVAL_DAYS == 365
    delta = datetime.datetime.now(datetime.UTC) - _resurvey_cutoff()
    assert abs(delta.days - 365) <= 1
    assert _resurvey_cutoff().tzinfo is not None


def test_list_graduation_years_includes_responded():
    # #537 — the second query returns distinct responders per grad year; each is
    # merged onto its year (0 when a year has no responses).
    session = QueueSession([
        [(2024, 5), (1900, 3)],  # year counts
        [(2024, 2)],             # only 2024 has responders
        [(1900, 1)],             # only 1900 has an unreachable alum (#392)
    ])
    result = asyncio.run(survey_email.list_graduation_years(session))
    assert [
        (g.graduation_year, g.total_alumni, g.responded, g.unreachable)
        for g in result
    ] == [
        (2024, 5, 2, 0),
        (1900, 3, 0, 1),
    ]


# --------------------------------------------------------- respondent --------


def test_get_respondent_invalid_token_is_none(fake_settings):
    # Garbage token -> verify fails before any DB access -> None.
    result = asyncio.run(survey_email.get_respondent(object(), "not-a-real-token"))
    assert result is None


class _RespondentSession:
    """Serves get_respondent's lookups in order: alum, contact, job, engagement,
    then the survey support contact (#774, a ``(name, email)`` row).

    Anything past the end of ``rows`` reads as "no row" rather than raising, so a
    test that only cares about the alum's fields does not have to spell out a
    support contact — it just gets ``support_contact: None``, which is the real
    behaviour when none is configured. Serves both ``scalar_one_or_none`` (the
    entity lookups) and ``first`` (the two-column contact select)."""

    def __init__(self, rows):
        self._rows = list(rows)

    async def execute(self, _stmt):
        row = self._rows.pop(0) if self._rows else None

        class _R:
            def scalar_one_or_none(self_inner):
                return row

            def first(self_inner):
                return row

        return _R()


def _respondent_alum():
    import types

    return types.SimpleNamespace(
        alumni_id=7,
        archived=False,
        first_name="Jordan",
        # All four name columns are pre-filled onto the confirm page since #646 —
        # the survey can change them now, and a name box that arrives blank must
        # mean "cleared", not "we never sent one".
        middle_name=None,
        last_name="Avery",
        preferred_first_name=None,
        employment_status="Employed",
        linkedin_url=None,
        graduate_degree=None,
        graduate_school=None,
        graduate_graduation_year=None,
        spouse_first_name=None,
        spouse_last_name=None,
        other_designations="Series 7, Series 63",
        gender=None,
        marital_status=None,
        birth_date=None,
        citizenship=None,
        home_country=None,
    )


def test_get_respondent_prefills_the_whole_name_block(fake_settings, monkeypatch):
    """#646 — the survey can change an alum's name, and the name fields refuse to
    write a blank (`survey_responses._Field.blankable`). That rule only holds
    because the boxes arrive PRE-FILLED: without these four an alum with a perfectly
    good name on file would be shown four empty inputs."""
    monkeypatch.setattr(survey_email, "verify_survey_token", lambda _t: 7)
    alum = _respondent_alum()
    alum.middle_name = "Whitaker"
    alum.preferred_first_name = "Jordy"
    session = _RespondentSession([alum, None, None, None])
    info = asyncio.run(survey_email.get_respondent(session, "tok"))
    assert info.fields["profile.first_name"] == "Jordan"
    assert info.fields["profile.middle_name"] == "Whitaker"
    assert info.fields["profile.last_name"] == "Avery"
    assert info.fields["profile.preferred_first_name"] == "Jordy"


def test_get_respondent_sends_an_off_list_marital_status_verbatim(
    fake_settings, monkeypatch
):
    """#647 — the four options constrain what the survey may WRITE, never what it
    may show. A legacy "Separated" has to reach the alum unaltered; the frontend
    re-adds it to the dropdown the way the staff employment-status dropdown does."""
    monkeypatch.setattr(survey_email, "verify_survey_token", lambda _t: 7)
    alum = _respondent_alum()
    alum.marital_status = "Separated"
    session = _RespondentSession([alum, None, None, None])
    info = asyncio.run(survey_email.get_respondent(session, "tok"))
    assert info.fields["profile.marital_status"] == "Separated"


def test_get_respondent_prefills_held_designations(fake_settings, monkeypatch):
    # #529 — the confirm page pre-ticks CFA/CFP/CPA from the DEDICATED columns
    # (which hold a marker string), sent as the tickbox's "Yes". Not-held is
    # omitted entirely rather than sent as "No", so the box renders untouched.
    import types

    monkeypatch.setattr(survey_email, "verify_survey_token", lambda _t: 7)
    eng = types.SimpleNamespace(
        cfa_designation="CFA", cfp_designation=None, cpa_designation="CPA"
    )
    session = _RespondentSession([_respondent_alum(), None, None, eng])
    info = asyncio.run(survey_email.get_respondent(session, "tok"))
    assert info.fields["program.cfa_designation"] == "Yes"
    assert "program.cfp_designation" not in info.fields
    # CPA joined the tickboxes on 2026-08-03; it pre-ticks like the other two.
    assert info.fields["program.cpa_designation"] == "Yes"
    # The free text is untouched — "CFA Level II Candidate"-style entries stay put.
    assert info.fields["profile.other_designations"] == "Series 7, Series 63"


@pytest.mark.parametrize("stored", ["No", "no", "  NO ", "N/A", "false", "0", "   "])
def test_get_respondent_does_not_prefill_a_negative(fake_settings, monkeypatch, stored):
    # The column is a marker-or-NULL flag, but nothing stops an intake sheet from
    # having put "No" there. Presence is NOT the question: an alum recorded as
    # NOT holding the CFA must see an UNTICKED box, not a pre-ticked one.
    import types

    monkeypatch.setattr(survey_email, "verify_survey_token", lambda _t: 7)
    eng = types.SimpleNamespace(cfa_designation=stored, cfp_designation="CFP")
    session = _RespondentSession([_respondent_alum(), None, None, eng])
    info = asyncio.run(survey_email.get_respondent(session, "tok"))
    assert "program.cfa_designation" not in info.fields
    # The held one is unaffected.
    assert info.fields["program.cfp_designation"] == "Yes"


def test_get_respondent_prefills_in_progress_designation(fake_settings, monkeypatch):
    # "CFP Level 1" is real prod data and is NOT interpreted as a negative — it
    # pre-ticks, same as today. See tests/test_designations.py for why.
    import types

    monkeypatch.setattr(survey_email, "verify_survey_token", lambda _t: 7)
    eng = types.SimpleNamespace(cfa_designation=None, cfp_designation="CFP Level 1")
    session = _RespondentSession([_respondent_alum(), None, None, eng])
    info = asyncio.run(survey_email.get_respondent(session, "tok"))
    assert info.fields["program.cfp_designation"] == "Yes"


def test_get_respondent_without_engagement_row(fake_settings, monkeypatch):
    # No engagement row at all -> neither designation key is sent.
    monkeypatch.setattr(survey_email, "verify_survey_token", lambda _t: 7)
    session = _RespondentSession([_respondent_alum(), None, None, None])
    info = asyncio.run(survey_email.get_respondent(session, "tok"))
    assert "program.cfa_designation" not in info.fields
    assert "program.cfp_designation" not in info.fields


def test_respond_route_404_on_invalid(client, monkeypatch):
    async def none_resp(session, token):
        return None

    monkeypatch.setattr(survey_email, "get_respondent", none_resp)
    resp = client.get("/survey/respond/badtoken")
    assert resp.status_code == 404


def test_respond_route_returns_info(client, monkeypatch):
    from app.schemas.survey import SurveyRespondInfo

    async def ok(session, token):
        return SurveyRespondInfo(
            first_name="Jane",
            full_name="Jane Doe",
            fields={"contact.personal_email": "jane@example-real.com"},
        )

    monkeypatch.setattr(survey_email, "get_respondent", ok)
    resp = client.get("/survey/respond/whatever")
    assert resp.status_code == 200
    body = resp.json()
    assert body["first_name"] == "Jane"
    assert body["fields"]["contact.personal_email"] == "jane@example-real.com"


# ------------------------------------------------------------- route ---------


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


def _ctx(*roles: str):
    from app.schemas.auth import UserContext

    return UserContext(
        user_id=1,
        auth_user_id=uuid.UUID("11111111-1111-1111-1111-111111111111"),
        roles=list(roles),
    )


def test_route_forbidden_for_view_only(client):
    from app.api.dependencies.auth import get_current_db_user
    from app.main import app

    app.dependency_overrides[get_current_db_user] = lambda: _ctx("view_only")
    response = client.post("/survey/campaigns/1900/send")
    assert response.status_code == 403


def test_route_defaults_to_dry_run(client, monkeypatch):
    from app.api.dependencies.auth import get_current_db_user
    from app.main import app
    from app.schemas.survey import SurveySendResult

    captured = {}

    async def fake_send(session, *, graduation_year, actor_user_id, dry_run, limit):
        captured.update(graduation_year=graduation_year, dry_run=dry_run, limit=limit)
        return SurveySendResult(
            graduation_year=graduation_year,
            total_recipients=0,
            prepared=0,
            sent=0,
            remaining=0,
            dry_run=dry_run,
            sample=[],
        )

    monkeypatch.setattr(survey_email, "send_campaign", fake_send)
    app.dependency_overrides[get_current_db_user] = lambda: _ctx("full_access")

    response = client.post("/survey/campaigns/1900/send")
    assert response.status_code == 200
    assert captured["graduation_year"] == 1900
    assert captured["dry_run"] is True  # must NOT send unless explicitly asked


# --- Real send-usage tally (#534) -------------------------------------------


class _CountSession:
    """Serves get_send_usage's single (month, today) aggregate."""

    def __init__(self, month, today):
        self._row = (month, today)
        self.stmt = None

    async def execute(self, stmt):
        self.stmt = stmt
        return SimpleNamespace(first=lambda: self._row)


def test_get_send_usage_counts_send_log_rows(fake_settings):
    from app.schemas.survey import SurveyUsage

    session = _CountSession(month=31, today=7)
    usage = asyncio.run(survey_email.get_send_usage(session))
    assert isinstance(usage, SurveyUsage)
    assert usage.sent_today == 7
    assert usage.sent_this_month == 31


def test_get_send_usage_reads_the_send_log_not_the_audit_trail(fake_settings):
    """The ledger MUST be `survey_send_log`, not `audit_logs`.

    Usage used to be regex-scraped out of `sent=N` in the audit text, which had
    two holes: an ENGINEER actor's AuditLog is rerouted into
    `engineer_action_log` by the audit hook (#199) and was never counted at all —
    so an engineer's manual send left the meter on zero and the scheduler handed
    out a budget that was already spent — and any aborted run lost the whole
    run's count even for batches that had been delivered."""
    session = _CountSession(month=0, today=0)
    asyncio.run(survey_email.get_send_usage(session))
    sql = str(session.stmt.compile(compile_kwargs={"literal_binds": True}))
    assert "survey_send_log" in sql
    assert "audit_logs" not in sql


def test_get_send_usage_applies_baseline(monkeypatch):
    # #544 — a configured baseline is added, and only sends AFTER the anchor are
    # counted on top (the baseline covers everything up to it).
    import datetime

    now = datetime.datetime.now(datetime.UTC)
    # ⚠️ MIDNIGHT TODAY, not "now minus five minutes". The baseline is only added
    # while we are still inside the anchor's UTC day (get_send_usage compares
    # now.date() == anchor.date()), and five minutes before 00:03 UTC is
    # YESTERDAY — so the old anchor silently moved the test out of the case it
    # was written to cover and asserted 17 == 3. It failed a real merge at
    # 00:01 UTC on 2026-08-25, and would have failed for the same five minutes
    # every night. Deriving the anchor from now.date() keeps it in today's
    # window at every hour, including the ones nobody runs CI in on purpose.
    anchor = datetime.datetime.combine(now.date(), datetime.time.min, tzinfo=datetime.UTC)

    class _S:
        survey_usage_baseline_at = anchor
        survey_usage_baseline_today = 14
        survey_usage_baseline_month = 26

    monkeypatch.setattr(survey_email, "get_settings", lambda: _S())

    session = _CountSession(month=3, today=3)  # rows after the anchor
    usage = asyncio.run(survey_email.get_send_usage(session))
    assert usage.sent_today == 17  # 14 baseline + 3 new
    assert usage.sent_this_month == 29  # 26 baseline + 3 new
