"""Tests for the survey response review queue (no real DB / network)."""

import asyncio
import types
import uuid

import pytest

from app.core.errors import NotFoundError
from app.models.survey_response import SurveyResponse
from app.services import supabase_storage
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
    assert resp.json() == {
        "staged": True,
        "change_count": 1,
        "survey_response_id": None,
    }


# --------------------------------------------------------- photo staging -----
#
# A NEW profile photo rides along a "confirm your info" submission as a separate,
# token-gated step (#524). It's staged under a `survey-pending/<id>` key in the
# private headshots bucket; on apply it becomes the alum's real headshot (keyed by
# net_id, or alumni_id when there's no net_id), on reject it's discarded. These
# tests fake the session + storage client so nothing touches a real DB / network.


class _Scalar:
    def __init__(self, obj):
        self._obj = obj

    def scalar_one_or_none(self):
        return self._obj


class _Session:
    """Minimal AsyncSession stand-in: every ``execute`` returns the same canned
    row, ``flush`` assigns an identity to a newly-added SurveyResponse (mirroring
    a real PK assignment), and add/commit are recorded."""

    def __init__(self, obj=None):
        self._obj = obj
        self.added = []
        self.committed = 0
        self.flushed = 0

    async def execute(self, _stmt):
        return _Scalar(self._obj)

    def add(self, obj):
        self.added.append(obj)

    async def flush(self):
        self.flushed += 1
        for obj in self.added:
            if isinstance(obj, SurveyResponse) and obj.survey_response_id is None:
                obj.survey_response_id = 777

    async def commit(self):
        self.committed += 1


def _fake_resp(**kw):
    base = dict(
        survey_response_id=1,
        alumni_id=5,
        payload={},
        status="pending",
        staged_photo_path=None,
        reviewed_by_user_id=None,
        reviewed_at=None,
    )
    base.update(kw)
    return types.SimpleNamespace(**base)


def test_submit_fields_only_stages_with_id(monkeypatch):
    # A fields-only submit (no photo) still stages exactly as before AND now
    # returns the new row id (so the page can attach a photo).
    monkeypatch.setattr(sr, "verify_survey_token", lambda _t: 5)
    alum = types.SimpleNamespace(alumni_id=5, archived=False, graduation_year=2020)
    session = _Session(alum)
    result = asyncio.run(
        sr.submit_response(session, "tok", {"contact.city": "Provo", "bogus": "x"})
    )
    assert result.staged is True
    assert result.change_count == 1  # "bogus" dropped
    assert result.survey_response_id == 777
    staged = [o for o in session.added if isinstance(o, SurveyResponse)]
    assert len(staged) == 1
    assert staged[0].staged_photo_path is None
    assert session.committed == 1


def test_stage_photo_foreign_response_404(monkeypatch):
    # Token resolves to alum 100 but the response belongs to 999 -> 404, and
    # nothing is uploaded.
    monkeypatch.setattr(sr, "verify_survey_token", lambda _t: 100)
    resp = _fake_resp(alumni_id=999)
    uploaded = []

    async def fake_upload(*a, **k):
        uploaded.append(a)

    monkeypatch.setattr(supabase_storage, "upload_object", fake_upload)
    with pytest.raises(NotFoundError):
        asyncio.run(sr.stage_photo(_Session(resp), "tok", 1, b"data", "image/jpeg"))
    assert uploaded == []


def test_stage_photo_non_pending_404(monkeypatch):
    # The alum owns the response but it's already been reviewed -> 404.
    monkeypatch.setattr(sr, "verify_survey_token", lambda _t: 100)
    resp = _fake_resp(alumni_id=100, status="applied")
    with pytest.raises(NotFoundError):
        asyncio.run(sr.stage_photo(_Session(resp), "tok", 1, b"data", "image/jpeg"))


def test_stage_photo_uploads_and_sets_path(monkeypatch):
    monkeypatch.setattr(sr, "verify_survey_token", lambda _t: 100)
    resp = _fake_resp(survey_response_id=7, alumni_id=100)
    session = _Session(resp)
    calls = {}

    async def fake_upload(bucket, path, data, content_type):
        calls["upload"] = (bucket, path, data, content_type)

    monkeypatch.setattr(supabase_storage, "upload_object", fake_upload)
    asyncio.run(sr.stage_photo(session, "tok", 7, b"bytes", "image/png"))
    assert calls["upload"] == ("headshots", "survey-pending/7", b"bytes", "image/png")
    assert resp.staged_photo_path == "survey-pending/7"
    assert session.committed == 1


def test_apply_with_photo_promotes_headshot(monkeypatch):
    resp = _fake_resp(staged_photo_path="survey-pending/1")
    alum = types.SimpleNamespace(alumni_id=5, net_id="jdoe5")

    async def fake_get_pending(_s, _rid):
        return resp

    async def fake_side(_s, _ids):
        return ({}, {}, {})

    monkeypatch.setattr(sr, "_get_pending", fake_get_pending)
    monkeypatch.setattr(sr, "_load_side_rows", fake_side)
    calls = {}

    async def fake_download(bucket, path):
        calls["download"] = (bucket, path)
        return b"\x89PNG\r\n\x1a\n rest"  # PNG magic -> content type image/png

    async def fake_upload(bucket, path, _data, content_type):
        calls["upload"] = (bucket, path, content_type)

    async def fake_delete(bucket, path):
        calls["delete"] = (bucket, path)

    monkeypatch.setattr(supabase_storage, "download_object", fake_download)
    monkeypatch.setattr(supabase_storage, "upload_object", fake_upload)
    monkeypatch.setattr(supabase_storage, "delete_object", fake_delete)
    asyncio.run(sr.apply_response(_Session(alum), 1, actor_user_id=9))
    assert calls["download"] == ("headshots", "survey-pending/1")
    assert calls["upload"] == ("headshots", "jdoe5", "image/png")
    assert calls["delete"] == ("headshots", "survey-pending/1")
    assert resp.status == "applied"


def test_apply_with_photo_falls_back_to_alumni_id(monkeypatch):
    # No net_id -> the headshot is keyed by the alumni_id (never hard-fails).
    resp = _fake_resp(alumni_id=42, staged_photo_path="survey-pending/1")
    alum = types.SimpleNamespace(alumni_id=42, net_id="")

    async def fake_get_pending(_s, _rid):
        return resp

    async def fake_side(_s, _ids):
        return ({}, {}, {})

    monkeypatch.setattr(sr, "_get_pending", fake_get_pending)
    monkeypatch.setattr(sr, "_load_side_rows", fake_side)
    key = {}

    async def fake_download(_b, _p):
        return b"\xff\xd8\xff rest"

    async def fake_upload(_bucket, path, _data, _ct):
        key["path"] = path

    async def fake_delete(_b, _p):
        pass

    monkeypatch.setattr(supabase_storage, "download_object", fake_download)
    monkeypatch.setattr(supabase_storage, "upload_object", fake_upload)
    monkeypatch.setattr(supabase_storage, "delete_object", fake_delete)
    asyncio.run(sr.apply_response(_Session(alum), 1, actor_user_id=9))
    assert key["path"] == "42"


def test_apply_without_photo_writes_fields_and_skips_storage(monkeypatch):
    resp = _fake_resp(payload={"profile.linkedin_url": "https://x"})
    alum = types.SimpleNamespace(alumni_id=5, net_id="jdoe5", linkedin_url=None)

    async def fake_get_pending(_s, _rid):
        return resp

    async def fake_side(_s, _ids):
        return ({}, {}, {})

    monkeypatch.setattr(sr, "_get_pending", fake_get_pending)
    monkeypatch.setattr(sr, "_load_side_rows", fake_side)
    touched = []

    async def boom(*a, **k):
        touched.append(a)

    monkeypatch.setattr(supabase_storage, "download_object", boom)
    monkeypatch.setattr(supabase_storage, "upload_object", boom)
    monkeypatch.setattr(supabase_storage, "delete_object", boom)
    asyncio.run(sr.apply_response(_Session(alum), 1, actor_user_id=9))
    assert alum.linkedin_url == "https://x"
    assert touched == []  # storage never touched when no photo staged
    assert resp.status == "applied"


def test_reject_with_photo_removes_staged(monkeypatch):
    resp = _fake_resp(staged_photo_path="survey-pending/1")

    async def fake_get_pending(_s, _rid):
        return resp

    monkeypatch.setattr(sr, "_get_pending", fake_get_pending)
    calls = {}

    async def fake_delete(bucket, path):
        calls["delete"] = (bucket, path)

    monkeypatch.setattr(supabase_storage, "delete_object", fake_delete)
    asyncio.run(sr.reject_response(_Session(None), 1, actor_user_id=9))
    assert calls["delete"] == ("headshots", "survey-pending/1")
    assert resp.status == "rejected"
