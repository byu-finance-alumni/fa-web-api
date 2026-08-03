"""Tests for the survey-email send service + route (no real network / DB)."""

import asyncio
import uuid

import pytest

from app.services import survey_email
from app.services.survey_email import (
    Recipient,
    make_survey_token,
    render_survey_email,
    verify_survey_token,
)


class _FakeSettings:
    survey_token_secret = "unit-test-secret"
    survey_from_email = "test@jakegunnell.com"
    survey_from_name = "BYU Finance Alumni"
    survey_app_base_url = "https://finance.alumni.byu.edu"
    survey_daily_cap = 100
    resend_api_key = "re_test_key"


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


class FakeSession:
    def __init__(self):
        self.added = []
        self.committed = 0

    def add(self, obj):
        self.added.append(obj)

    async def commit(self):
        self.committed += 1


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
    assert session.committed == 1  # audit row still written


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
    # Two executes now: year counts, then distinct-responder counts. Same rows for
    # both here is fine — the shape assertion only checks the year/total columns.
    session = QueueSession([[(2024, 5), (1900, 3)], []])
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
    ])
    result = asyncio.run(survey_email.list_graduation_years(session))
    assert [(g.graduation_year, g.total_alumni, g.responded) for g in result] == [
        (2024, 5, 2),
        (1900, 3, 0),
    ]


# --------------------------------------------------------- respondent --------


def test_get_respondent_invalid_token_is_none(fake_settings):
    # Garbage token -> verify fails before any DB access -> None.
    result = asyncio.run(survey_email.get_respondent(object(), "not-a-real-token"))
    assert result is None


class _RespondentSession:
    """Serves get_respondent's four lookups in order: alum, contact, job,
    engagement."""

    def __init__(self, rows):
        self._rows = list(rows)

    async def execute(self, _stmt):
        row = self._rows.pop(0)

        class _R:
            def scalar_one_or_none(self_inner):
                return row

        return _R()


def _respondent_alum():
    import types

    return types.SimpleNamespace(
        alumni_id=7,
        archived=False,
        first_name="Jordan",
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


def test_get_respondent_prefills_held_designations(fake_settings, monkeypatch):
    # #529 — the confirm page pre-ticks CFA/CFP from the DEDICATED columns (which
    # hold a marker string), sent as the tickbox's "Yes". Not-held is omitted
    # entirely rather than sent as "No", so the box renders untouched.
    import types

    monkeypatch.setattr(survey_email, "verify_survey_token", lambda _t: 7)
    eng = types.SimpleNamespace(cfa_designation="CFA", cfp_designation=None)
    session = _RespondentSession([_respondent_alum(), None, None, eng])
    info = asyncio.run(survey_email.get_respondent(session, "tok"))
    assert info.fields["program.cfa_designation"] == "Yes"
    assert "program.cfp_designation" not in info.fields
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


def test_tally_sent_splits_today_vs_month():
    import datetime

    from app.services.survey_email import _tally_sent

    start_today = datetime.datetime(2026, 7, 28, tzinfo=datetime.UTC)
    rows = [
        # Two sends today -> count toward both today and the month.
        ("grad_year=1900 recipients=60 prepared=50 sent=50 dry_run=False",
         datetime.datetime(2026, 7, 28, 14, 0, tzinfo=datetime.UTC)),
        ("grad_year=2000 recipients=30 prepared=30 sent=30 dry_run=False",
         datetime.datetime(2026, 7, 28, 8, 0, tzinfo=datetime.UTC)),
        # Earlier this month -> month only.
        ("grad_year=1990 recipients=20 prepared=20 sent=20 dry_run=False",
         datetime.datetime(2026, 7, 10, 9, 0, tzinfo=datetime.UTC)),
        # A row with no parseable count contributes 0, not a crash.
        ("malformed audit row with no count",
         datetime.datetime(2026, 7, 28, 10, 0, tzinfo=datetime.UTC)),
    ]
    sent_today, sent_this_month = _tally_sent(rows, start_today)
    assert sent_today == 80  # 50 + 30
    assert sent_this_month == 100  # + 20


def test_get_send_usage_sums_audit_rows():
    import datetime

    from app.schemas.survey import SurveyUsage

    class _Rows:
        def all(self):
            # created "now" -> counts as today (and this month).
            return [("grad_year=1900 sent=7 dry_run=False",
                     datetime.datetime.now(datetime.UTC))]

    class _Session:
        async def execute(self, _stmt):
            return _Rows()

    usage = asyncio.run(survey_email.get_send_usage(_Session()))
    assert isinstance(usage, SurveyUsage)
    assert usage.sent_today == 7
    assert usage.sent_this_month == 7


def test_get_send_usage_applies_baseline(monkeypatch):
    # #544 — a configured baseline is added, and only sends AFTER the anchor are
    # counted on top (the baseline covers everything up to it).
    import datetime

    now = datetime.datetime.now(datetime.UTC)
    anchor = now - datetime.timedelta(minutes=5)  # earlier the same day/month

    class _S:
        survey_usage_baseline_at = anchor
        survey_usage_baseline_today = 14
        survey_usage_baseline_month = 26

    monkeypatch.setattr(survey_email, "get_settings", lambda: _S())

    class _Rows:
        def all(self):
            return [("grad_year=1900 sent=3 dry_run=False", now)]  # after anchor

    class _Session:
        async def execute(self, _stmt):
            return _Rows()

    usage = asyncio.run(survey_email.get_send_usage(_Session()))
    assert usage.sent_today == 17  # 14 baseline + 3 new
    assert usage.sent_this_month == 29  # 26 baseline + 3 new
