"""Tests for the survey response review queue (no real DB / network)."""

import asyncio
import datetime
import types
import uuid

import pytest

from app.core.dropdowns import INDUSTRIES, SURVEY_EMPLOYMENT_STATUSES
from app.core.errors import NotFoundError
from app.models.audit import AuditLog
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


def test_coerce_designation_writes_the_server_side_marker():
    # #529 — the submitted TEXT is ignored: ticked writes the marker from the
    # field definition, unticked writes NULL. The submit route is public
    # (token-gated), so a client must not be able to choose what lands in a
    # column the designation filter reads as "holds the CFA".
    cfa = sr._FIELD_BY_KEY["program.cfa_designation"]
    assert _coerce(cfa, "Yes") == "CFA"
    assert _coerce(cfa, "true") == "CFA"
    assert _coerce(cfa, "1") == "CFA"
    assert _coerce(cfa, "No") is None
    assert _coerce(cfa, "") is None
    # Arbitrary text is NOT stored — truthiness only, then the canonical marker.
    assert _coerce(cfa, "CFA Level II Candidate") is None
    assert _coerce(cfa, "x" * 200) is None
    assert _coerce(sr._FIELD_BY_KEY["program.cfp_designation"], "yes") == "CFP"


def test_designation_fields_are_whitelisted_to_their_own_columns():
    # #529 — CFA/CFP/CPA write to the dedicated alumni_program_engagement columns
    # the filter/exports key off, NOT into the free-text `other_designations`.
    # Each writes its own marker, so a ticked box is findable by the filter.
    for key, column, marker in (
        ("program.cfa_designation", "cfa_designation", "CFA"),
        ("program.cfp_designation", "cfp_designation", "CFP"),
        # CPA was added after CFA/CFP (Jake, 2026-08-03). It previously had a
        # column and a filter but no way for an alum to ever populate it.
        ("program.cpa_designation", "cpa_designation", "CPA"),
    ):
        field = sr._FIELD_BY_KEY[key]
        assert (field.group, field.column, field.kind, field.marker) == (
            "engagement",
            column,
            "designation",
            marker,
        )
    # The three "Other" blanks still merge into the alumni free-text column.
    other = sr._FIELD_BY_KEY["profile.other_designations"]
    assert (other.group, other.column, other.kind) == ("alumni", "other_designations", "text")


def test_apply_writes_designation_markers(monkeypatch):
    # End to end through apply: a ticked CFA lands as the marker on the engagement
    # row, an unticked CFP clears it, and free text goes to the alumni column.
    resp = _fake_resp(
        payload={
            "program.cfa_designation": "Yes",
            "program.cfp_designation": "No",
            "profile.other_designations": "Series 7, Series 63",
        }
    )
    alum = types.SimpleNamespace(alumni_id=5, net_id="jdoe5", other_designations=None)
    eng = types.SimpleNamespace(alumni_id=5, cfa_designation=None, cfp_designation="CFP")

    async def fake_get_pending(_s, _rid):
        return resp

    async def fake_side(_s, _ids):
        return ({}, {}, {5: eng})

    monkeypatch.setattr(sr, "_get_pending", fake_get_pending)
    monkeypatch.setattr(sr, "_load_side_rows", fake_side)
    asyncio.run(sr.apply_response(_Session(alum), 1, actor_user_id=9))
    assert eng.cfa_designation == "CFA"
    assert eng.cfp_designation is None
    assert alum.other_designations == "Series 7, Series 63"


def test_designation_diff_reads_as_yes_no(monkeypatch):
    # The reviewer sees "No -> Yes", not "None -> CFA".
    cfa = sr._FIELD_BY_KEY["program.cfa_designation"]
    assert _current(cfa, types.SimpleNamespace(cfa_designation="CFA")) == "Yes"
    assert _current(cfa, types.SimpleNamespace(cfa_designation=None)) == "No"
    assert _after(cfa, "Yes") == "Yes"
    assert _after(cfa, "No") == "No"


def test_designation_diff_treats_a_stored_negative_as_not_held():
    # A column imported as the literal "No" is a truthy stored value, but it
    # means NOT held — the reviewer's "before" must say "No" or the diff would
    # claim the alum already had the designation. Same predicate as the filter.
    cfa = sr._FIELD_BY_KEY["program.cfa_designation"]
    assert _current(cfa, types.SimpleNamespace(cfa_designation="No")) == "No"
    assert _current(cfa, types.SimpleNamespace(cfa_designation="  n/a ")) == "No"
    # In-progress text still reads as held (open product question — see
    # tests/test_designations.py).
    assert _current(cfa, types.SimpleNamespace(cfa_designation="CFA Level II")) == "Yes"


def test_current_and_after_formatting():
    obj = types.SimpleNamespace(piff_donor=True, employer="Acme")
    bf = _Field("program.piff_donor", "PIFF", "engagement", "piff_donor", "bool")
    assert _current(bf, obj) == "Yes"
    assert _after(bf, "yes") == "Yes"
    assert _after(bf, "No") == "No"
    tf = _Field("k", "Employer", "employment", "employer", "text")
    assert _current(tf, obj) == "Acme"
    assert _current(tf, None) == ""
    assert _after(tf, " Beta ") == "Beta"


def test_missing_side_row_reads_as_no_not_blank():
    # An alum with NO alumni_program_engagement row holds none of the yes/no
    # flags — the columns are NOT NULL / default-false, so "no row" means "No",
    # not "unknown". Reading it as "" made an honest "No" answer show up in the
    # review queue as a bogus change ("" -> "No"). Text stays blank-for-blank.
    bf = sr._FIELD_BY_KEY["program.mentor_willing"]
    assert _current(bf, None) == "No"
    assert _after(bf, "No") == "No"
    cfa = sr._FIELD_BY_KEY["program.cfa_designation"]
    assert _current(cfa, None) == "No"
    assert _current(sr._FIELD_BY_KEY["profile.linkedin_url"], None) == ""


def test_every_engagement_flag_the_survey_asks_is_whitelisted():
    # The survey's "Ways to get involved" screen. Each must map to its own bool
    # column on alumni_program_engagement — a key the apply whitelist doesn't
    # know is dropped, so a YES the alum submitted would never reach the record.
    for column in (
        "mentor_willing",
        "women_in_finance_mentor_willing",
        "guest_speaker_willing",
        "help_at_event_willing",
        "nettrek_host_willing",
        "finance_conference_willing",
        "company_event_sponsor_willing",
        "case_competition_host_willing",
        "piff_donor",
    ):
        field = sr._FIELD_BY_KEY[f"program.{column}"]
        assert (field.group, field.column, field.kind) == ("engagement", column, "bool")


def test_coerce_bool_round_trips_json_true_and_false():
    # The survey posts the strings "Yes"/"No", but the payload column is JSON and
    # a client (or a future form) may put a real JSON boolean there. BOTH must
    # survive: a `true` that landed as False would silently look like a correct
    # "not willing", with nothing anywhere saying an answer was lost.
    bf = sr._FIELD_BY_KEY["program.mentor_willing"]
    for truthy in (True, "Yes", "yes", "YES", "true", "True", "1"):
        assert _coerce(bf, truthy) is True, truthy
    for falsy in (False, "No", "no", "false", "False", "0", "", None):
        assert _coerce(bf, falsy) is False, falsy
    assert _after(bf, True) == "Yes"
    assert _after(bf, False) == "No"


def test_apply_writes_engagement_flags_and_creates_a_missing_row(monkeypatch):
    # End to end through apply for the bug Jake hit: YES to "willing to mentor"
    # must land as True on the engagement row, a NO must land as False, and an
    # alum with no engagement row at all must get one created rather than have
    # the answer dropped.
    resp = _fake_resp(
        payload={
            "program.mentor_willing": "Yes",
            "program.guest_speaker_willing": "No",
            "program.piff_donor": "Yes",
        }
    )
    alum = types.SimpleNamespace(alumni_id=5, net_id="jdoe5")

    async def fake_get_pending(_s, _rid):
        return resp

    async def fake_side(_s, _ids):
        return ({}, {}, {})  # no engagement row on file

    monkeypatch.setattr(sr, "_get_pending", fake_get_pending)
    monkeypatch.setattr(sr, "_load_side_rows", fake_side)
    session = _Session(alum)
    asyncio.run(sr.apply_response(session, 1, actor_user_id=9))
    eng = next(
        o for o in session.added if isinstance(o, sr.AlumniProgramEngagement)
    )
    assert eng.alumni_id == 5
    assert eng.mentor_willing is True
    assert eng.guest_speaker_willing is False
    assert eng.piff_donor is True
    assert resp.status == "applied"


def test_apply_reports_keys_it_could_not_write(monkeypatch, caplog):
    # A payload key missing from the whitelist writes NOTHING, yet the response
    # still flips to "applied". That used to happen in total silence, so a rename
    # on either side of the wire would lose alumni answers with no trace. It must
    # now WARN with the offending keys and record the counts on the audit row.
    resp = _fake_resp(
        payload={"program.mentor_willing": "Yes", "program.mentorWilling": "Yes"}
    )
    alum = types.SimpleNamespace(alumni_id=5, net_id="jdoe5")
    eng = types.SimpleNamespace(alumni_id=5, mentor_willing=False)

    async def fake_get_pending(_s, _rid):
        return resp

    async def fake_side(_s, _ids):
        return ({}, {}, {5: eng})

    monkeypatch.setattr(sr, "_get_pending", fake_get_pending)
    monkeypatch.setattr(sr, "_load_side_rows", fake_side)
    session = _Session(alum)
    with caplog.at_level("WARNING"):
        asyncio.run(sr.apply_response(session, 1, actor_user_id=9))
    assert eng.mentor_willing is True
    assert "program.mentorWilling" in caplog.text
    audit = next(o for o in session.added if isinstance(o, AuditLog))
    assert "written=1" in audit.new_value
    assert "dropped=1" in audit.new_value


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

    async def fake_submit(session, token, fields, has_photo=False):
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


def test_submit_photo_only_creates_row_and_returns_id(monkeypatch):
    # #537 — an alum who ONLY changed their photo sends an empty `fields` with
    # has_photo=True. That must still stage a pending row (change_count=0) and
    # return its id, so the page can attach the photo.
    monkeypatch.setattr(sr, "verify_survey_token", lambda _t: 5)
    alum = types.SimpleNamespace(alumni_id=5, archived=False, graduation_year=2020)
    session = _Session(alum)
    result = asyncio.run(sr.submit_response(session, "tok", {}, has_photo=True))
    assert result.staged is True
    assert result.change_count == 0
    assert result.survey_response_id == 777
    staged = [o for o in session.added if isinstance(o, SurveyResponse)]
    assert len(staged) == 1
    assert staged[0].payload == {}
    assert session.committed == 1


def test_submit_empty_no_photo_is_noop(monkeypatch):
    # #537 — a true no-op (no recognized fields AND no photo) stages nothing and
    # returns a null id, exactly as before.
    monkeypatch.setattr(sr, "verify_survey_token", lambda _t: 5)
    alum = types.SimpleNamespace(alumni_id=5, archived=False, graduation_year=2020)
    session = _Session(alum)
    result = asyncio.run(sr.submit_response(session, "tok", {"bogus": "x"}, has_photo=False))
    assert result.staged is False
    assert result.change_count == 0
    assert result.survey_response_id is None
    assert [o for o in session.added if isinstance(o, SurveyResponse)] == []
    assert session.committed == 0


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


def _real_image(fmt="PNG", size=(60, 40)):
    """Actual encoded image bytes.

    Since promotion re-encodes, a stand-in like ``b"\\x89PNG... rest"`` is no
    longer a photo as far as `apply_response` is concerned — it is an undecodable
    object, which is a different test.
    """
    import io

    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", size, (10, 200, 60)).save(buf, format=fmt)
    return buf.getvalue()


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
        return _real_image("PNG")

    async def fake_upload(bucket, path, data, content_type):
        calls["upload"] = (bucket, path, content_type)
        calls["bytes"] = data

    async def fake_delete(bucket, path):
        calls["delete"] = (bucket, path)

    monkeypatch.setattr(supabase_storage, "download_object", fake_download)
    monkeypatch.setattr(supabase_storage, "upload_object", fake_upload)
    monkeypatch.setattr(supabase_storage, "delete_object", fake_delete)
    outcome = asyncio.run(sr.apply_response(_Session(alum), 1, actor_user_id=9))
    assert calls["download"] == ("headshots", "survey-pending/1")
    # A PNG went in and a JPEG comes out, because promotion re-encodes. The
    # recorded content type has to be the type actually stored, not the type
    # staged — labelling this "image/png" is what the old sniff did.
    assert calls["upload"] == ("headshots", "jdoe5", "image/jpeg")
    assert calls["bytes"].startswith(b"\xff\xd8\xff")
    assert calls["delete"] == ("headshots", "survey-pending/1")
    assert resp.status == "applied"
    assert outcome.photo_dropped is False


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
        return _real_image("JPEG")

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
    # NOTE the URL: since #418 the linkedin_url field carries the staff LinkedIn
    # rule, so a placeholder like "https://x" is now (correctly) refused and would
    # make this test assert nothing about the write path.
    resp = _fake_resp(payload={"profile.linkedin_url": "https://www.linkedin.com/in/jdoe"})
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
    assert alum.linkedin_url == "https://www.linkedin.com/in/jdoe"
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


# ------------------------------------------- hostile public input (#418) ------
#
# `apply_response` writes with a raw `setattr`, so no Pydantic validator runs and
# every rule in app/schemas/alumni.py is bypassed on this path. The survey made
# these columns a PUBLIC write surface, so the fields whose values are trusted
# downstream (rendered as an href, handed to the email sender) carry their rule
# on the _Field itself and are refused exactly like an off-list `choice`.


def test_linkedin_url_refuses_hostile_values():
    field = sr._FIELD_BY_KEY["profile.linkedin_url"]
    # The stored value becomes an href on staff pages, so a non-http(s) scheme is
    # a script a signed-in reviewer runs by clicking. The before/after diff would
    # have shown them a plausible-looking URL.
    assert _coerce(field, "javascript:alert(document.cookie)") is sr._IGNORE
    assert _coerce(field, "data:text/html;base64,PHNjcmlwdD4=") is sr._IGNORE
    # Lookalike hosts: each of these READS as LinkedIn in the review queue and
    # resolves somewhere else. The last two are the ones a host check written as
    # a substring/`startswith` test would wave through.
    for hostile in (
        "https://linkedin.com.evil.example/in/jdoe",
        "https://evil.example/linkedin.com/in/jdoe",
        "https://linkedin.com@evil.example/in/jdoe",
        "https://notlinkedin.com/in/jdoe",
    ):
        assert _coerce(field, hostile) is sr._IGNORE, hostile
    # A real profile URL still goes through, trimmed — refusing these would just
    # move the bug from "accepts anything" to "accepts nothing".
    assert (
        _coerce(field, "  https://www.linkedin.com/in/jane-doe-123  ")
        == "https://www.linkedin.com/in/jane-doe-123"
    )
    assert _coerce(field, "https://linkedin.com/in/jdoe") == "https://linkedin.com/in/jdoe"


def test_linkedin_url_refuses_the_backslash_parser_differential():
    r"""A backslash makes Python and the BROWSER disagree about the host, and the
    browser is the one that decides where the staff member actually goes.

    Verified against both parsers on 2026-08-07:

        https://evil.example\@linkedin.com/in/x
          Python urlsplit() -> host "linkedin.com"   (the first fix PASSED this)
          browser new URL() -> host "evil.example"   (where the click lands)

    So the value read as a LinkedIn profile everywhere staff could see it, and
    resolved to the attacker. The render-side guard cannot catch it either — by
    every measure that guard checks it is a valid https URL.

    No real linkedin.com URL contains a backslash, encoded or not.
    """
    field = sr._FIELD_BY_KEY["profile.linkedin_url"]
    for hostile in (
        "https://evil.example\\@linkedin.com/in/jdoe",
        "https://evil.example%5C@linkedin.com/in/jdoe",
        "https://evil.example%5c@linkedin.com/in/jdoe",
        "https:\\\\linkedin.com/in/jdoe",
    ):
        assert _coerce(field, hostile) is sr._IGNORE, hostile
    # A blank is still the ordinary "clear this column" instruction, NOT a
    # rejection: the rule guards hostile values, it must not swallow a legitimate
    # clear (linkedin_url is blankable).
    assert _coerce(field, "") is None


def test_linkedin_rule_is_the_staff_rule_not_a_second_copy():
    # Guards against the rule silently disappearing (e.g. if the shared validator
    # stopped being reachable) — the point of #418 is ONE definition, so if the
    # staff schema accepts a URL this path must too, and vice versa.
    from app.schemas.alumni import AlumniBase

    for value in (
        "https://www.linkedin.com/in/jdoe",
        "javascript:alert(1)",
        "https://linkedin.com.evil.example/in/jdoe",
    ):
        try:
            AlumniBase._validate_linkedin_url(value)
            staff_ok = True
        except ValueError:
            staff_ok = False
        survey_ok = _coerce(sr._FIELD_BY_KEY["profile.linkedin_url"], value) is not sr._IGNORE
        assert staff_ok == survey_ok, value


def test_email_refuses_a_smuggled_second_recipient():
    # The stored address is what `email_reach.resolve_email` hands the sender, so
    # a separator character in the column silently copies or redirects every
    # future mail to that alumnus — and the console still shows one address.
    for field_key in ("contact.personal_email", "contact.work_email"):
        field = sr._FIELD_BY_KEY[field_key]
        for hostile in (
            "alum@byu.edu, attacker@evil.example",
            "alum@byu.edu;attacker@evil.example",
            "alum@byu.edu attacker@evil.example",
            "alum@byu.edu\nattacker@evil.example",
            "alum@byu.edu\r\nBcc: attacker@evil.example",
            '"Finance Alumni" <attacker@evil.example>',
            "alum@byu.edu@evil.example",
            "@byu.edu",
            "alum@byu",
            "a" * 250 + "@byu.edu",  # longer than the varchar(255) column
            # mailto: QUERY INJECTION (found re-reviewing the fix, 2026-08-07).
            # The address is rendered as `href={`mailto:${email}`}`, and in a
            # mailto: URL `?` and `&` start and separate query parameters — so
            # these pre-fill the compose window a staff member opens by clicking
            # "Send" with attacker-authored subject and body text, in a message
            # they believe is their own.
            "victim@byu.edu?subject=Urgent&body=Click%20here",
            "victim@byu.edu?cc=attacker@evil.example",
            "victim@byu.edu&bcc=attacker",
        ):
            assert _coerce(field, hostile) is sr._IGNORE, (field_key, hostile)
        # Ordinary addresses — including the plus-addressed and hyphenated shapes
        # real people use — still pass. This gate is about "is this ONE address",
        # not "is this address real"; a misspelling is a data problem staff can
        # see and fix, and rejecting it here would silently drop real alumni.
        for ok in (
            "jane.doe@byu.edu",
            "jane+alumni@gmail.com",
            "j.doe-smith@sub.domain.co.uk",
            "  jdoe@byu.edu  ",
        ):
            assert _coerce(field, ok) == ok.strip(), (field_key, ok)
        assert _coerce(field, "") is None  # clearing an address is still allowed


def test_hostile_values_are_never_even_staged(monkeypatch):
    # `submit_response` already drops anything `_coerce` would refuse, so a
    # hostile value never reaches the review queue at all — a reviewer is never
    # shown a change that approving would (or worse, would not) make.
    monkeypatch.setattr(sr, "verify_survey_token", lambda _t: 5)
    alum = types.SimpleNamespace(alumni_id=5, archived=False, graduation_year=2020)
    session = _Session(alum)
    result = asyncio.run(
        sr.submit_response(
            session,
            "tok",
            {
                "profile.linkedin_url": "javascript:alert(1)",
                "contact.personal_email": "alum@byu.edu, attacker@evil.example",
                "contact.work_email": "jdoe@byu.edu",
            },
        )
    )
    assert result.change_count == 1
    staged = next(o for o in session.added if isinstance(o, SurveyResponse))
    assert staged.payload == {"contact.work_email": "jdoe@byu.edu"}


def test_apply_leaves_the_column_alone_on_a_refused_value(monkeypatch):
    # The refusal must NOT be "write NULL": an alum who already has a good
    # LinkedIn URL keeps it. Counted in `ignored` (a known field whose answer we
    # declined), never in `dropped` (a key we don't recognize), and only the KEY
    # is logged — never the submitted value.
    resp = _fake_resp(
        payload={
            "profile.linkedin_url": "javascript:alert(1)",
            "contact.personal_email": "good@byu.edu",
        }
    )
    alum = types.SimpleNamespace(
        alumni_id=5, net_id="jdoe5", linkedin_url="https://www.linkedin.com/in/real"
    )
    contact = types.SimpleNamespace(alumni_id=5, personal_email=None)

    async def fake_get_pending(_s, _rid):
        return resp

    async def fake_side(_s, _ids):
        return ({5: contact}, {}, {})

    monkeypatch.setattr(sr, "_get_pending", fake_get_pending)
    monkeypatch.setattr(sr, "_load_side_rows", fake_side)
    session = _Session(alum)
    asyncio.run(sr.apply_response(session, 1, actor_user_id=9))
    assert alum.linkedin_url == "https://www.linkedin.com/in/real"
    assert contact.personal_email == "good@byu.edu"
    audit = next(o for o in session.added if isinstance(o, AuditLog))
    assert "written=1" in audit.new_value
    assert "ignored=1" in audit.new_value
    assert "dropped=0" in audit.new_value


def test_offlist_choice_behaviour_is_not_regressed():
    # #647 regression guard. Adding the `validate` hook must not disturb the
    # existing `choice` contract: an off-list answer is refused (not stored, not
    # NULLed), and a value already ON FILE that is off-list still READS back
    # verbatim. The option list constrains what may be WRITTEN, never what may be
    # displayed.
    field = sr._FIELD_BY_KEY["profile.marital_status"]
    assert _coerce(field, "Separated") is sr._IGNORE
    assert _coerce(field, "<script>alert(1)</script>") is sr._IGNORE
    assert _coerce(field, "married") == "Married"
    assert _current(field, types.SimpleNamespace(marital_status="Separated")) == "Separated"
    # A blank on this non-blankable field is still ignored, not a wipe.
    assert _coerce(field, "") is sr._IGNORE


# ---------------------------------------------- concurrent review (#421) ------


def test_get_pending_locks_the_row():
    # Without FOR UPDATE the `status == "pending"` check below is advisory only:
    # two reviewers (or one double-click) both read "pending", both pass, and one
    # applies the changes + promotes the photo while the other marks it rejected.
    # The record changed and the audit says it was refused. The lock makes the
    # second transaction block, re-read, and get the ordinary "already reviewed"
    # error instead. Same pattern as survey_reset._load_alum.
    from sqlalchemy.dialects import postgresql

    class _Recording(_Session):
        def __init__(self, obj):
            super().__init__(obj)
            self.stmts = []

        async def execute(self, stmt):
            self.stmts.append(stmt)
            return await super().execute(stmt)

    resp = _fake_resp()
    session = _Recording(resp)
    assert asyncio.run(sr._get_pending(session, 1)) is resp
    sql = str(session.stmts[0].compile(dialect=postgresql.dialect()))
    assert "FOR UPDATE" in sql


def test_get_pending_still_rejects_an_already_reviewed_response():
    from app.core.errors import InvalidRequestError

    with pytest.raises(InvalidRequestError):
        asyncio.run(sr._get_pending(_Session(_fake_resp(status="applied")), 1))
    with pytest.raises(NotFoundError):
        asyncio.run(sr._get_pending(_Session(None), 1))


# ============================ the rest of the writable-field audit (#426) =====
#
# Same root cause as #418 above, worked through the whole of `_FIELDS` rather
# than the two fields that were already known: `apply_response` writes with a raw
# `setattr`, so nothing in app/schemas/alumni.py runs on the one path the public
# can reach. Every rule written when "only staff can reach this" was true had to
# be restated here.


def _apply(session, resp, monkeypatch, side_rows=({}, {}, {})):
    """Run `apply_response` with `_get_pending` / `_load_side_rows` stubbed."""

    async def fake_get_pending(_s, _rid):
        return resp

    async def fake_side(_s, _ids):
        return side_rows

    monkeypatch.setattr(sr, "_get_pending", fake_get_pending)
    monkeypatch.setattr(sr, "_load_side_rows", fake_side)
    return asyncio.run(sr.apply_response(session, 1, actor_user_id=9))


# --------------------------------------------- 1. queue poisoning (the year) --


def test_an_absurd_year_is_ignored_rather_than_staged_as_a_time_bomb():
    """THE bug this issue was opened for. `_coerce` did a bare `int()`, so a
    20-digit year became a 20-digit Python integer: fine at submit, fine in the
    reviewer's diff, and then Postgres 22003 at APPLY on an int4 column. The
    transaction rolls back, the response never leaves `pending`, and every future
    Approve on it 500s - a review queue anyone with a survey link can jam."""
    field = sr._FIELD_BY_KEY["profile.graduate_graduation_year"]
    for poison in ("9" * 20, "-" + "9" * 20, str(2**63), str(2**31), "99999"):
        assert _coerce(field, poison) is sr._IGNORE, poison
    # Ordinary answers are untouched - the fix must not stop alumni answering.
    assert _coerce(field, "2027") == 2027
    assert _coerce(field, " 1998 ") == 1998
    # Long-standing behaviour for a blank / non-numeric value is deliberately
    # unchanged: a blank is still "clear the column", and "abc" still parses to
    # nothing. Neither can wedge anything.
    assert _coerce(field, "") is None
    assert _coerce(field, "abc") is None


def test_the_year_range_is_the_staff_range_not_a_second_copy():
    """One definition of "a plausible year". If the staff schema accepts a year
    this path must too, and vice versa - the #418 lesson applied to #426."""
    from app.schemas.alumni import AlumniBase

    field = sr._FIELD_BY_KEY["profile.graduate_graduation_year"]
    this_year = datetime.date.today().year
    for year in (1899, 1900, 1901, 2020, this_year + 10, this_year + 11, 10**20):
        try:
            AlumniBase._validate_year(year)
            staff_ok = True
        except ValueError:
            staff_ok = False
        survey_ok = _coerce(field, str(year)) is not sr._IGNORE
        assert staff_ok == survey_ok, year


def test_the_year_is_the_only_int_field_that_could_overflow():
    """Nothing else on the whitelist is an `int`, so the range on the kind covers
    every field that can reach an integer column. A new `int` field would show up
    here and needs the same thought (see the note in `_coerce`)."""
    int_fields = [f.key for f in sr._FIELDS if f.kind == "int"]
    assert int_fields == ["profile.graduate_graduation_year"]


def test_a_poisoned_year_never_reaches_the_review_queue(monkeypatch):
    monkeypatch.setattr(sr, "verify_survey_token", lambda _t: 5)
    alum = types.SimpleNamespace(alumni_id=5, archived=False, graduation_year=2020)
    session = _Session(alum)
    result = asyncio.run(
        sr.submit_response(
            session,
            "tok",
            {
                "profile.graduate_graduation_year": "9" * 20,
                "profile.graduate_school": "BYU",
            },
        )
    )
    assert result.change_count == 1
    staged = next(o for o in session.added if isinstance(o, SurveyResponse))
    assert staged.payload == {"profile.graduate_school": "BYU"}


def test_a_response_already_wedged_by_a_poisoned_year_can_now_be_applied(monkeypatch):
    """The half of the fix that matters for anything already in the queue.
    Blocking new poison still leaves a jammed response jammed, and `_coerce` runs
    off the STORED payload at apply time - so a row staged before this change is
    now skipped like any other refused value and the response completes instead of
    500-ing on every attempt. The good year on file is kept, not NULLed."""
    resp = _fake_resp(
        payload={
            "profile.graduate_graduation_year": "9" * 20,
            "profile.graduate_school": "BYU Marriott",
        }
    )
    alum = types.SimpleNamespace(
        alumni_id=5,
        net_id="jdoe5",
        graduate_graduation_year=2026,
        graduate_school=None,
    )
    session = _Session(alum)
    _apply(session, resp, monkeypatch)

    assert alum.graduate_graduation_year == 2026  # untouched, not overwritten
    assert alum.graduate_school == "BYU Marriott"  # the rest of the answer lands
    assert resp.status == "applied"  # no longer stuck pending
    audit = next(o for o in session.added if isinstance(o, AuditLog))
    assert "written=1" in audit.new_value
    assert "ignored=1" in audit.new_value


def test_the_queue_drops_a_poisoned_year_from_the_diff(monkeypatch):
    """A reviewer must not be shown a year change that approving would not make."""

    class _Rows:
        def __init__(self, rows):
            self._rows = rows

        def scalars(self):
            return self

        def all(self):
            return list(self._rows)

    resp = types.SimpleNamespace(
        survey_response_id=1,
        alumni_id=5,
        payload={
            "profile.graduate_graduation_year": "9" * 20,
            "profile.graduate_school": "BYU Marriott",
        },
        status="pending",
        staged_photo_path=None,
        submitted_at=datetime.datetime(2026, 8, 8),
    )
    alum = types.SimpleNamespace(
        alumni_id=5,
        first_name="Jane",
        last_name="Doe",
        preferred_first_name=None,
        graduate_graduation_year=2026,
        graduate_school=None,
    )

    class _ListSession:
        queue = [[resp], [alum]]

        async def execute(self, _stmt):
            return _Rows(_ListSession.queue.pop(0))

    async def fake_side(_s, _ids):
        return ({}, {}, {})

    monkeypatch.setattr(sr, "_load_side_rows", fake_side)
    items = asyncio.run(sr.list_pending(_ListSession(), 2020))
    keys = [c.field_key for c in items[0].changes]
    assert "profile.graduate_graduation_year" not in keys
    assert "profile.graduate_school" in keys


# ------------------------------------------- 2. controlled vocabularies -------
#
# The frontend ALREADY renders both of these as dropdowns. The two tuples below
# are the exact strings its controls offer, copied from
# fa-web-app/src/components/survey/survey-screens.tsx and
# fa-web-app/src/constants/dropdowns.ts and verified character-for-character on
# 2026-08-08. They are pinned HERE rather than derived because a mismatch is the
# one failure mode that would be worse than the free text this replaces: every
# legitimate answer silently ignored, with nothing anywhere saying so. If either
# side moves, this fails.

# `SURVEY_EMPLOYMENT_STATUS_OPTIONS` - EMPLOYMENT_STATUS_OPTIONS minus the
# "Unknown" placeholder.
_FRONTEND_EMPLOYMENT_STATUS_OPTIONS = (
    "Full-time",
    "Part-time",
    "Self-Employed",
    "Graduate Student",
    "Military",
    "Not in the Labor Force",
    "Unemployed",
)

# `INDUSTRY_CHOICES` - PRIMARY_INDUSTRY_OPTIONS (the vocabulary minus the four
# secondary-only industries) minus "Other", which the control handles separately.
_FRONTEND_INDUSTRY_CHOICES = (
    "Asset Management",
    "Commercial Banking",
    "Consulting",
    "Corporate Finance",
    "Equity Research",
    "Financial Services",
    "FP&A",
    "Investment Banking",
    "Military",
    "Private Banking",
    "Private Credit",
    "Private Equity",
    "Real Estate",
    "Sales",
    "Valuation & Advisory",
    "Venture Capital",
    "Wealth Management",
    "Unknown",
    "Graduate Student",
)


def test_industry_and_employment_status_are_constrained_choices():
    industry = sr._FIELD_BY_KEY["employment.current_industry"]
    assert (industry.group, industry.column, industry.kind) == (
        "employment",
        "current_industry",
        "choice",
    )
    assert industry.options == INDUSTRIES

    status = sr._FIELD_BY_KEY["profile.employment_status"]
    assert (status.group, status.column, status.kind) == (
        "alumni",
        "employment_status",
        "choice",
    )
    assert status.options == SURVEY_EMPLOYMENT_STATUSES
    # The list that exists specifically for this field: the canonical eight minus
    # "Unknown", which is meaningless as a self-description.
    assert "Unknown" not in status.options


@pytest.mark.parametrize("value", _FRONTEND_EMPLOYMENT_STATUS_OPTIONS)
def test_every_employment_status_the_survey_offers_is_writable(value):
    """The load-bearing check. An option the form offers that the server ignores
    would drop every real answer to this question, silently."""
    assert _coerce(sr._FIELD_BY_KEY["profile.employment_status"], value) == value


@pytest.mark.parametrize("value", _FRONTEND_INDUSTRY_CHOICES)
def test_every_industry_the_survey_offers_is_writable(value):
    assert _coerce(sr._FIELD_BY_KEY["employment.current_industry"], value) == value


def test_the_backend_accepts_more_industries_than_the_survey_offers():
    """Deliberate, and the right direction for the asymmetry: the four
    secondary-only industries and "Other" are legitimate stored values, so an alum
    re-submitting one already on file must not be refused. The reverse asymmetry -
    an offered option the server rejects - is what the two tests above forbid."""
    field = sr._FIELD_BY_KEY["employment.current_industry"]
    for not_offered in (
        "Law",
        "Corporate Banking",
        "Sales and Trading",
        "Credit Risk",
        "Other",
    ):
        assert _coerce(field, not_offered) == not_offered


def test_an_off_list_industry_or_status_is_ignored_not_stored(monkeypatch):
    """The reason for the change: a public submit could mint a phantom bucket that
    then shows up in the dashboard breakdown, the filter and `search_terms` as
    though it were one of ours."""
    industry = sr._FIELD_BY_KEY["employment.current_industry"]
    status = sr._FIELD_BY_KEY["profile.employment_status"]
    for hostile in ("Crypto Rug Pulls", "<script>alert(1)</script>", "x" * 500):
        assert _coerce(industry, hostile) is sr._IGNORE, hostile
        assert _coerce(status, hostile) is sr._IGNORE, hostile
    # "Unknown" is a real industry but NOT an offerable status.
    assert _coerce(industry, "Unknown") == "Unknown"
    assert _coerce(status, "Unknown") is sr._IGNORE

    alum = types.SimpleNamespace(
        alumni_id=5, net_id="jdoe5", employment_status="Employed"
    )
    job = types.SimpleNamespace(alumni_id=5, current_industry="Insurance")
    _apply(
        _Session(alum),
        _fake_resp(
            payload={
                "profile.employment_status": "Crypto",
                "employment.current_industry": "Crypto",
            }
        ),
        monkeypatch,
        side_rows=({}, {5: job}, {}),
    )
    assert alum.employment_status == "Employed"  # legacy value survives
    assert job.current_industry == "Insurance"


def test_casing_drift_resolves_and_a_blank_never_wipes_the_stored_value():
    """`choice` already did both - this pins that converting these two fields did
    not change it. Prod holds casing drift from a free-text intake sheet, and an
    alum whose stored value is off-list sees a control with no matching option:
    if leaving it alone wiped the column the survey would destroy exactly the
    legacy data the `choice` kind exists to preserve."""
    industry = sr._FIELD_BY_KEY["employment.current_industry"]
    status = sr._FIELD_BY_KEY["profile.employment_status"]
    assert _coerce(industry, "investment banking") == "Investment Banking"
    assert _coerce(status, "  FULL-TIME  ") == "Full-time"
    assert _coerce(industry, "") is sr._IGNORE
    assert _coerce(status, "") is sr._IGNORE
    # And an off-list value ON FILE still reads back verbatim in the diff.
    assert (
        _current(industry, types.SimpleNamespace(current_industry="Insurance"))
        == "Insurance"
    )
    assert (
        _current(status, types.SimpleNamespace(employment_status="Employed"))
        == "Employed"
    )


def test_secondary_industry_stays_free_text():
    """Free text on the staff path too (`EmploymentCreate` runs no
    `validate_industry` on it) - the consistency is deliberate. It still carries
    the column width and the character rule."""
    field = sr._FIELD_BY_KEY["employment.current_industry_secondary"]
    assert field.kind == "text"
    assert field.options is None
    assert _coerce(field, "Education") == "Education"


# ------------------------------------------------------ 3. length caps --------
#
# Column widths read from database/schema.sql on 2026-08-08. `other_designations`
# is the one that is not a varchar at all - an unbounded `text` column carrying a
# trigram GIN index, so an oversize value is not merely untidy, it bloats an index
# every alumni search reads.
_COLUMN_WIDTHS = {
    "profile.first_name": 100,
    "profile.middle_name": 100,
    "profile.last_name": 100,
    "profile.preferred_first_name": 100,
    "profile.spouse_first_name": 100,
    "profile.spouse_last_name": 100,
    "profile.gender": 30,
    "profile.citizenship": 100,
    "profile.home_country": 100,
    "profile.graduate_degree": 100,
    "profile.graduate_school": 255,
    "profile.other_designations": 10000,  # `text` column; mirrors the staff cap
    "profile.linkedin_url": 500,
    "contact.personal_email": 255,
    "contact.work_email": 255,
    "contact.phone": 50,
    "contact.city": 100,
    "contact.state": 100,
    "contact.country": 100,
    "employment.current_employer": 255,
    "employment.current_title": 255,
    "employment.current_industry_secondary": 255,
    "employment.current_city": 100,
    "employment.current_state": 100,
    "employment.current_country": 100,
    "employment.current_zip": 20,
}


def test_every_text_field_is_bounded_by_its_real_column_width():
    """No `text` field had a cap at all before #426, so a public submit could
    stage a value no column can hold - a 500 at apply for the varchars, and for
    `other_designations` a silent success that bloats the search index."""
    for field in sr._FIELDS:
        if field.kind != "text":
            continue
        assert field.max_length == _COLUMN_WIDTHS[field.key], field.key


@pytest.mark.parametrize("key,width", sorted(_COLUMN_WIDTHS.items()))
def test_an_over_long_value_is_ignored_rather_than_truncated(key, width):
    """Refused, not shortened: half an employer name presented as the whole thing
    is a wrong answer that looks like a right one. Same disposition as an off-list
    choice, so the column keeps whatever it already held."""
    field = sr._FIELD_BY_KEY[key]
    assert _coerce(field, "a" * (width + 1)) is sr._IGNORE, key


def test_a_huge_other_designations_value_cannot_reach_the_indexed_column(monkeypatch):
    monkeypatch.setattr(sr, "verify_survey_token", lambda _t: 5)
    alum = types.SimpleNamespace(alumni_id=5, archived=False, graduation_year=2020)
    session = _Session(alum)
    result = asyncio.run(
        sr.submit_response(session, "tok", {"profile.other_designations": "x" * 100_000})
    )
    assert result.staged is False
    assert result.change_count == 0
    # And an ordinary answer is unaffected.
    assert (
        _coerce(sr._FIELD_BY_KEY["profile.other_designations"], "Series 7, Series 63")
        == "Series 7, Series 63"
    )


# ------------------------------------------------------ 4. birth_date ---------


def test_birth_date_is_bounded_the_way_staff_bound_it():
    """The survey took 0001-01-01 and 9999-12-31 while staff were held to
    1900-and-not-future. Same rule, reused, restated where the public writes."""
    field = sr._FIELD_BY_KEY["profile.birth_date"]
    today = datetime.date.today()
    for bad in (
        "0001-01-01",
        "9999-12-31",
        "1899-12-31",
        (today + datetime.timedelta(days=1)).isoformat(),
    ):
        assert _coerce(field, bad) is sr._IGNORE, bad
    assert _coerce(field, "1900-01-01") == datetime.date(1900, 1, 1)
    assert _coerce(field, "1985-06-30") == datetime.date(1985, 6, 30)
    assert _coerce(field, today.isoformat()) == today
    # A blank is still the ordinary "clear this column" instruction, and an
    # unparseable string is still nothing - neither is a rejection.
    assert _coerce(field, "") is None
    assert _coerce(field, "not-a-date") is None


def test_an_out_of_range_birthday_leaves_the_stored_one_alone(monkeypatch):
    alum = types.SimpleNamespace(
        alumni_id=5, net_id="jdoe5", birth_date=datetime.date(1990, 3, 2)
    )
    _apply(
        _Session(alum),
        _fake_resp(payload={"profile.birth_date": "9999-12-31"}),
        monkeypatch,
    )
    assert alum.birth_date == datetime.date(1990, 3, 2)


# ------------------------------------ 5. name / free-text validation ----------


_NAME_KEYS = (
    "profile.first_name",
    "profile.middle_name",
    "profile.last_name",
    "profile.preferred_first_name",
    "profile.spouse_first_name",
    "profile.spouse_last_name",
)


@pytest.mark.parametrize("key", _NAME_KEYS)
def test_names_carry_the_staff_name_rule(key):
    """A formula lead used to write cleanly into `first_name`. Mitigated at the
    far end (the CSV export neutralises formula leads, React escapes on render),
    so this is data pollution rather than execution - but the public could put
    control characters into the identity columns that search, the duplicate check
    and every export key off, and a control character is invisible in the
    reviewer's before/after diff."""
    field = sr._FIELD_BY_KEY[key]
    for hostile in (
        '=HYPERLINK("http://evil","click")',
        "+1+1",
        "@SUM(A1)",
        "-2+3",
        "Jane;DROP TABLE alumni",
        "Jane<script>",
        "Jane|Doe",
        "Jane\nDoe",
        "Jane\rDoe",
        "Jane\x00Doe",
    ):
        assert _coerce(field, hostile) is sr._IGNORE, (key, hostile)


@pytest.mark.parametrize("key", _NAME_KEYS)
@pytest.mark.parametrize(
    "name",
    [
        "O'Brien",  # straight apostrophe
        "N\u2019Diaye",  # curly apostrophe
        "Anne-Marie",  # hyphen
        "St. John",  # period
        "Jos\u00e9 \u00c1lvarez",  # accented Latin
        "\u674e\u5c0f\u9f99",  # non-Latin script
        "van der Berg",
    ],
)
def test_real_names_still_go_through(key, name):
    """The rule is a DENY-list precisely so a surname nobody here has seen is
    accepted by default. Silently dropping a real alumna's surname is a worse
    outcome than storing an odd one, and this path ignores rather than rejects -
    she would never be told."""
    assert _coerce(sr._FIELD_BY_KEY[key], name) == name


def test_the_name_rule_is_the_staff_rule_not_a_second_copy():
    from app.schemas.alumni import AlumniBase

    for value in (
        "Jane",
        "O'Brien",
        '=HYPERLINK("x","y")',
        "Jane;Doe",
        "12345",
        "x" * 101,
    ):
        try:
            AlumniBase._validate_name(value)
            staff_ok = True
        except ValueError:
            staff_ok = False
        survey_ok = (
            _coerce(sr._FIELD_BY_KEY["profile.last_name"], value) is not sr._IGNORE
        )
        assert staff_ok == survey_ok, value


def test_a_hostile_name_leaves_the_record_alone(monkeypatch):
    """Ignored, never written as NULL - these are the columns search, dedup and
    every export key off, so refusing must not also destroy what is on file."""
    alum = types.SimpleNamespace(
        alumni_id=5,
        net_id="jdoe5",
        first_name="Jane",
        last_name="Doe",
        graduation_year=2018,
    )
    session = _Session(alum)
    _apply(
        session,
        _fake_resp(payload={"profile.last_name": '=HYPERLINK("http://evil","x")'}),
        monkeypatch,
    )
    assert alum.last_name == "Doe"
    audit = next(o for o in session.added if isinstance(o, AuditLog))
    assert "written=0" in audit.new_value
    assert "ignored=1" in audit.new_value


_FREE_TEXT_KEYS = (
    "profile.gender",
    "profile.citizenship",
    "profile.home_country",
    "profile.graduate_degree",
    "profile.graduate_school",
    "contact.city",
    "contact.state",
    "contact.country",
    "employment.current_employer",
    "employment.current_title",
    "employment.current_industry_secondary",
    "employment.current_city",
    "employment.current_state",
    "employment.current_country",
    "employment.current_zip",
)


@pytest.mark.parametrize("key", _FREE_TEXT_KEYS)
def test_free_text_columns_carry_the_character_half_of_the_name_rule(key):
    """Junk in the state/country fields produces unmappable rows on the world map,
    and control characters in an employer break every export that keys off it."""
    field = sr._FIELD_BY_KEY[key]
    for hostile in (
        "=1+1",
        "@evil",
        "-lead",
        "a;b",
        "a<b>",
        "a|b",
        "a\nb",
        "a\x07b",
    ):
        assert _coerce(field, hostile) is sr._IGNORE, (key, hostile)


@pytest.mark.parametrize(
    "key,value",
    [
        ("employment.current_employer", "Goldman Sachs & Co."),
        ("employment.current_employer", "AT&T"),
        ("employment.current_title", "VP, Corporate Finance"),
        ("employment.current_city", "St. George"),
        ("employment.current_country", "C\u00f4te d'Ivoire"),
        ("employment.current_zip", "84604"),  # digits only: a real ZIP
        ("employment.current_zip", "84604-1234"),
        ("profile.gender", "Female"),
        ("profile.graduate_school", "BYU Marriott School of Business"),
    ],
)
def test_ordinary_free_text_answers_still_go_through(key, value):
    """The digits-only rejection in the staff NAME rule is deliberately not
    carried over here - a ZIP code really is digits only, and dropping it would
    move the bug from "accepts anything" to "accepts nothing"."""
    assert _coerce(sr._FIELD_BY_KEY[key], value) == value


def test_a_phone_may_start_with_a_plus():
    """The one deliberate exception to the leading-formula-character rule.
    `+1 801-555-0100` is how an international number is written and is the most
    likely legitimate value on this whole whitelist to start with a `+`; silently
    dropping it would discard exactly the numbers hardest to re-collect. The lead
    is neutralised by the CSV export like any other."""
    field = sr._FIELD_BY_KEY["contact.phone"]
    for ok in ("+1 801-555-0100", "801-555-0100", "8015550100", "(801) 555-0100"):
        assert _coerce(field, ok) == ok, ok
    # Everything else the rule blocks still applies.
    for hostile in ("=1+1", "@evil", "-555", "801;555", "801\n555", "801|555"):
        assert _coerce(field, hostile) is sr._IGNORE, hostile


def test_other_designations_is_not_held_to_the_name_character_set():
    """Stricter than the staff path would be its own bug - this column
    legitimately holds punctuation-heavy free text. Control characters are the
    part staff already refuse (`_validate_survey_text`), so that is the part
    restated here."""
    field = sr._FIELD_BY_KEY["profile.other_designations"]
    assert _coerce(field, "Series 7; Series 63") == "Series 7; Series 63"
    assert _coerce(field, "CFP <in progress>") == "CFP <in progress>"
    assert _coerce(field, "Series 7\nSeries 63") is sr._IGNORE


def test_every_text_field_on_the_whitelist_has_a_rule():
    """The guard that makes the next added field think about this. `text` is the
    only kind whose stored value comes verbatim from the payload, so a `text`
    field with no `validate` is a public write with no rule on it at all."""
    unguarded = [f.key for f in sr._FIELDS if f.kind == "text" and f.validate is None]
    assert unguarded == []


# --------------------------------------------------- 6. log injection ---------


def test_an_unknown_payload_key_cannot_forge_a_log_line(monkeypatch, caplog):
    """`dropped` holds payload keys that are NOT on the whitelist - entirely
    submitter-chosen text - and they were joined into a `log.warning` verbatim, so
    a newline in one wrote a second, fully attacker-authored log line. `repr`
    escapes it; the value stays visible for debugging, on one line."""
    forged = "x\nWARNING:root:Survey response 999: approved by admin"
    resp = _fake_resp(payload={forged: "1", "program.mentor_willing": "Yes"})
    alum = types.SimpleNamespace(alumni_id=5, net_id="jdoe5")
    eng = types.SimpleNamespace(alumni_id=5, mentor_willing=False)

    async def fake_side(_s, _ids):
        return ({}, {}, {5: eng})

    async def fake_get_pending(_s, _rid):
        return resp

    monkeypatch.setattr(sr, "_get_pending", fake_get_pending)
    monkeypatch.setattr(sr, "_load_side_rows", fake_side)
    session = _Session(alum)
    with caplog.at_level("WARNING"):
        asyncio.run(sr.apply_response(session, 1, actor_user_id=9))

    record = next(r for r in caplog.records if r.levelname == "WARNING")
    message = record.getMessage()
    assert "\n" not in message
    assert "\\n" in message  # the newline is escaped, not dropped
    assert eng.mentor_willing is True  # the rest of the apply is unaffected


def test_a_flood_of_unknown_keys_does_not_write_one_enormous_log_record():
    """The staged payload has no key-count limit, so a submission carrying
    thousands of junk keys would otherwise become a single unreadable log line."""
    rendered = sr._log_safe_keys([f"k{i}" for i in range(500)])
    assert rendered.count("'k") == sr._LOG_KEYS_MAX
    assert "(480 more)" in rendered
    # A very long key is truncated rather than logged whole.
    long_key = sr._log_safe_keys(["z" * 5000])
    assert len(long_key) < 100
