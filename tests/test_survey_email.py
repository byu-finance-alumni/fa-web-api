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
        pass

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


def test_list_graduation_years_shape():
    session = ExecSession([(2024, 5), (1900, 3)])
    result = asyncio.run(survey_email.list_graduation_years(session))
    assert [(g.graduation_year, g.total_alumni) for g in result] == [(2024, 5), (1900, 3)]


# --------------------------------------------------------- respondent --------


def test_get_respondent_invalid_token_is_none(fake_settings):
    # Garbage token -> verify fails before any DB access -> None.
    result = asyncio.run(survey_email.get_respondent(object(), "not-a-real-token"))
    assert result is None


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
