"""Tests for the survey response review queue (no real DB / network)."""

import asyncio
import types
import uuid

import pytest

from app.services import survey_responses as sr
from app.services.survey_responses import _after, _coerce, _current, _Field

# ------------------------------------------------------------- helpers -------


def test_coerce_bool_int_text():
    bf = _Field("k", "L", "engagement", "c", "bool")
    assert _coerce(bf, "Yes") is True
    assert _coerce(bf, "no") is False
    intf = _Field("k", "L", "alumni", "c", "int")
    assert _coerce(intf, "2027") == 2027
    assert _coerce(intf, "") is None
    assert _coerce(intf, "abc") is None
    tf = _Field("k", "L", "alumni", "c", "text")
    assert _coerce(tf, "  hi ") == "hi"
    assert _coerce(tf, "") is None
    import datetime

    df = _Field("profile.birth_date", "Birthday", "alumni", "birth_date", "date")
    assert _coerce(df, "2000-01-15") == datetime.date(2000, 1, 15)
    assert _coerce(df, "") is None
    assert _coerce(df, "not-a-date") is None


def test_new_profile_fields_are_whitelisted():
    # #523 — the Personal & family + Company ZIP fields the survey now covers.
    for key in (
        "profile.gender",
        "profile.marital_status",
        "profile.birth_date",
        "profile.citizenship",
        "profile.home_country",
        "employment.current_zip",
    ):
        assert key in sr._FIELD_BY_KEY, key


def test_current_and_after_formatting():
    obj = types.SimpleNamespace(piff_donor=True, employer="Acme")
    bf = _Field("program.piff_donor", "PIFF", "engagement", "piff_donor", "bool")
    assert _current(bf, obj) == "Yes"
    assert _current(bf, None) == ""
    assert _after(bf, "yes") == "Yes"
    assert _after(bf, "No") == "No"
    tf = _Field("k", "Employer", "employment", "employer", "text")
    assert _current(tf, obj) == "Acme"
    assert _after(tf, " Beta ") == "Beta"


def test_submit_invalid_token_raises():
    from app.core.errors import NotFoundError

    # Garbage token -> verify fails before any DB/secret access -> NotFoundError.
    with pytest.raises(NotFoundError):
        asyncio.run(sr.submit_response(object(), "garbage-token", {"contact.city": "X"}))


# ------------------------------------------------------------- routes --------


@pytest.fixture
def client():
    from fastapi.testclient import TestClient

    from app.core.database import get_session
    from app.main import app

    async def _no_db():
        yield None

    app.dependency_overrides[get_session] = _no_db
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


def test_apply_forbidden_for_view_only(client):
    from app.api.dependencies.auth import get_current_db_user
    from app.main import app

    app.dependency_overrides[get_current_db_user] = lambda: _ctx("view_only")
    resp = client.post("/survey/responses/5/apply")
    assert resp.status_code == 403


def test_submit_route_is_public(client, monkeypatch):
    from app.schemas.survey import SurveySubmitResult

    async def fake_submit(session, token, fields):
        return SurveySubmitResult(staged=True, change_count=len(fields))

    monkeypatch.setattr(sr, "submit_response", fake_submit)
    resp = client.post(
        "/survey/respond/sometoken", json={"fields": {"contact.city": "Provo"}}
    )
    assert resp.status_code == 200
    assert resp.json() == {"staged": True, "change_count": 1}
