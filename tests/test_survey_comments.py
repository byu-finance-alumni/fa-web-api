"""The survey's optional "Anything else you'd like to share?" box (#890).

What is pinned here:

  * **Submit**: the comment is accepted, optional, cleaned (stripped, invisible
    and control characters removed, newlines kept), stored under its own payload
    key, blank is ABSENT rather than an empty string, and over 2000 characters
    is a 422 at the route. A comment on its own still stages a row, and is never
    counted as a changed field.
  * **Review queue**: the comment reaches the reviewer verbatim, and a row with
    no comment key (every row before #890) reads as None.
  * **Apply**: exactly ONE profile Note, dated by submission, attributed to the
    reviewer, audited like any note — and a second apply of the same response
    cannot add another.
  * **Reject**: no Note, ever.

Offline throughout — fake sessions in the style of ``test_survey_responses``;
no Postgres, no network.
"""

import asyncio
import datetime
import types

import pytest
from pydantic import ValidationError

from app.core.audit_context import reset_audit_actor, reset_audit_source
from app.core.errors import InvalidRequestError
from app.models.audit import AuditLog
from app.models.note import Note
from app.models.survey_response import (
    COMMENTS_KEY,
    SurveyResponse,
    payload_field_count,
)
from app.schemas.survey import (
    SURVEY_COMMENTS_MAX,
    SurveySubmitRequest,
    clean_survey_comment,
)
from app.services import survey_responses as sr


@pytest.fixture(autouse=True)
def _clean_audit_context():
    reset_audit_source()
    reset_audit_actor()
    yield
    reset_audit_source()
    reset_audit_actor()


# ------------------------------------------------------------- fakes ---------


class _Scalar:
    def __init__(self, obj, rows=()):
        self._obj = obj
        self._rows = list(rows)

    def scalar_one_or_none(self):
        return self._obj

    def scalars(self):
        return self

    def all(self):
        return list(self._rows)


def _reads_survey_responses(stmt) -> bool:
    try:
        return any(
            getattr(f, "name", None) == "survey_responses" for f in stmt.get_final_froms()
        )
    except Exception:  # noqa: BLE001 - a fake
        return False


class _Session:
    """Answers ``survey_responses`` reads with ``response`` and everything else
    with ``obj``; records adds and commits."""

    def __init__(self, obj=None, *, response=None):
        self._obj = obj
        self._response = response
        self.added = []
        self.committed = 0

    async def execute(self, stmt):
        if _reads_survey_responses(stmt):
            return _Scalar(self._response)
        return _Scalar(self._obj)

    def add(self, obj):
        self.added.append(obj)

    async def flush(self):
        for obj in self.added:
            if isinstance(obj, SurveyResponse) and obj.survey_response_id is None:
                obj.survey_response_id = 777

    async def commit(self):
        self.committed += 1


def _alum():
    return types.SimpleNamespace(
        alumni_id=5,
        net_id="jdoe5",
        archived=False,
        first_name="Jane",
        middle_name=None,
        last_name="Doe",
        preferred_first_name=None,
        graduation_year=2018,
    )


def _fake_resp(**kw):
    base = dict(
        survey_response_id=1,
        alumni_id=5,
        payload={},
        status="pending",
        staged_photo_path=None,
        reviewed_by_user_id=None,
        reviewed_at=None,
        submitted_at=datetime.datetime(2026, 10, 22, 18, 30, tzinfo=datetime.UTC),
    )
    base.update(kw)
    return types.SimpleNamespace(**base)


def _submit(monkeypatch, fields, **kw):
    monkeypatch.setattr(sr, "verify_survey_token", lambda _t: 5)

    async def no_cycle(*_a, **_k):
        return (None, None)

    monkeypatch.setattr(sr, "_sent_cycle_and_stage", no_cycle)
    session = _Session(_alum())
    result = asyncio.run(sr.submit_response(session, "tok", fields, **kw))
    staged = [o for o in session.added if isinstance(o, SurveyResponse)]
    return result, staged, session


def _apply(session, resp, monkeypatch):
    async def fake_get_pending(_s, _rid):
        return resp

    async def fake_side(_s, _ids):
        return ({}, {}, {})

    monkeypatch.setattr(sr, "_get_pending", fake_get_pending)
    monkeypatch.setattr(sr, "_load_side_rows", fake_side)
    return asyncio.run(sr.apply_response(session, 1, actor_user_id=9))


def _notes(session) -> list[Note]:
    return [o for o in session.added if isinstance(o, Note)]


# ------------------------------------------------------------ schema ---------


def test_comment_is_optional_and_absent_by_default():
    assert SurveySubmitRequest(fields={}).comments is None


def test_comment_is_stripped_and_blank_becomes_absent():
    assert SurveySubmitRequest(fields={}, comments="  Go Cougs!  ").comments == "Go Cougs!"
    assert SurveySubmitRequest(fields={}, comments="   \n\t ").comments is None
    assert SurveySubmitRequest(fields={}, comments="").comments is None


def test_comment_keeps_newlines_and_drops_invisible_and_control_chars():
    raw = "Line one\r\nLine​ two‮\x00\x07\n\tindented"
    assert clean_survey_comment(raw) == "Line one\nLine two\n\tindented"


def test_comment_at_the_cap_is_accepted_and_over_it_is_refused():
    assert len(SurveySubmitRequest(fields={}, comments="x" * SURVEY_COMMENTS_MAX).comments) == (
        SURVEY_COMMENTS_MAX
    )
    with pytest.raises(ValidationError):
        SurveySubmitRequest(fields={}, comments="x" * (SURVEY_COMMENTS_MAX + 1))


def test_comment_must_be_text():
    with pytest.raises(ValidationError):
        SurveySubmitRequest(fields={}, comments=["not", "text"])


# ------------------------------------------------------------- route ---------


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


def test_route_passes_the_cleaned_comment_through(client, monkeypatch):
    from app.schemas.survey import SurveySubmitResult

    seen = {}

    async def fake_submit(
        session, token, fields, has_photo=False, confirmed_only=False, fill_seconds=None,
        comments=None,
    ):
        seen["comments"] = comments
        return SurveySubmitResult(staged=True, change_count=0, survey_response_id=1)

    monkeypatch.setattr(sr, "submit_response", fake_submit)
    resp = client.post(
        "/survey/respond/sometoken", json={"fields": {}, "comments": "  Thanks!  "}
    )
    assert resp.status_code == 200
    assert seen["comments"] == "Thanks!"


def test_route_refuses_an_over_cap_comment_with_422(client, monkeypatch):
    called = []

    async def fake_submit(*_a, **_k):
        called.append(True)

    monkeypatch.setattr(sr, "submit_response", fake_submit)
    resp = client.post(
        "/survey/respond/sometoken",
        json={"fields": {}, "comments": "x" * (SURVEY_COMMENTS_MAX + 1)},
    )
    assert resp.status_code == 422
    assert called == []


def test_route_without_a_comment_still_works(client, monkeypatch):
    from app.schemas.survey import SurveySubmitResult

    seen = {}

    async def fake_submit(
        session, token, fields, has_photo=False, confirmed_only=False, fill_seconds=None,
        comments=None,
    ):
        seen["comments"] = comments
        return SurveySubmitResult(staged=True, change_count=1)

    monkeypatch.setattr(sr, "submit_response", fake_submit)
    resp = client.post("/survey/respond/sometoken", json={"fields": {"contact.city": "Provo"}})
    assert resp.status_code == 200
    assert seen["comments"] is None


# ------------------------------------------------------------ submit ---------


def test_submit_stores_the_comment_under_its_own_key(monkeypatch):
    result, staged, _ = _submit(
        monkeypatch, {"contact.city": "Provo"}, comments="Happy to help with recruiting."
    )
    assert len(staged) == 1
    assert staged[0].payload == {
        "contact.city": "Provo",
        COMMENTS_KEY: "Happy to help with recruiting.",
    }
    # The comment is not a changed field.
    assert result.change_count == 1


def test_submit_with_a_blank_comment_stores_no_key(monkeypatch):
    _, staged, _ = _submit(monkeypatch, {"contact.city": "Provo"}, comments="   ")
    assert COMMENTS_KEY not in staged[0].payload


def test_a_comment_alone_still_stages_a_row(monkeypatch):
    # Nothing else changed, but a reviewer has to read what they wrote — it must
    # not be lost as a no-op.
    result, staged, session = _submit(monkeypatch, {}, comments="Just saying hi.")
    assert result.staged is True
    assert result.change_count == 0
    assert result.survey_response_id == 777
    assert staged[0].payload == {COMMENTS_KEY: "Just saying hi."}
    assert session.committed == 1


def test_a_comment_wins_over_confirmed_only(monkeypatch):
    # Same rule as fields and photos: content always wins over the flag.
    result, staged, _ = _submit(monkeypatch, {}, confirmed_only=True, comments="One thing...")
    assert result.confirmed is False
    assert staged[0].payload == {COMMENTS_KEY: "One thing..."}
    assert staged[0].status == "pending"


def test_a_comment_smuggled_into_fields_is_ignored(monkeypatch):
    # `fields` is the whitelist of columns; the comment only travels top-level.
    result, staged, _ = _submit(monkeypatch, {COMMENTS_KEY: "sneaky"})
    assert result.staged is False
    assert staged == []


def test_payload_field_count_skips_the_comment():
    assert payload_field_count({"contact.city": "Provo", COMMENTS_KEY: "hi"}) == 1
    assert payload_field_count({COMMENTS_KEY: "hi"}) == 0
    assert payload_field_count(None) == 0


# ------------------------------------------------------------ review ---------


def _list(monkeypatch, payload):
    resp = _fake_resp(payload=payload)
    alum = _alum()

    class _ListSession:
        queue = [[resp], [alum]]

        async def execute(self, _stmt):
            return _Scalar(None, _ListSession.queue.pop(0))

    async def fake_side(_s, _ids):
        return ({}, {}, {})

    monkeypatch.setattr(sr, "_load_side_rows", fake_side)
    return asyncio.run(sr.list_pending(_ListSession(), 2018))


def test_review_queue_shows_the_comment_verbatim(monkeypatch):
    text = "<b>not html</b>\nsecond line"
    [item] = _list(monkeypatch, {COMMENTS_KEY: text})
    assert item.comments == text
    # Not a field, so it is never shown as a change.
    assert item.changes == []


def test_review_queue_handles_a_row_without_a_comment_key(monkeypatch):
    [item] = _list(monkeypatch, {"contact.city": "Provo"})
    assert item.comments is None
    assert [c.field_key for c in item.changes] == ["contact.city"]


# ------------------------------------------------------------- apply ---------


def test_apply_saves_the_comment_as_exactly_one_note(monkeypatch):
    session = _Session(_alum())
    resp = _fake_resp(payload={"contact.city": "Provo", COMMENTS_KEY: "Loved the program."})
    _apply(session, resp, monkeypatch)

    [note] = _notes(session)
    assert note.alumni_id == 5
    assert note.body == "Survey comment (2026-10-22): Loved the program."
    # Attributed to the reviewer who applied it, like any note.
    assert note.created_by_user_id == 9
    assert note.updated_by_user_id == 9
    assert note.interaction_id is None and note.event_id is None
    assert resp.status == "applied"
    assert session.committed == 1


def test_apply_audits_the_note_like_any_other_note(monkeypatch):
    session = _Session(_alum())
    _apply(session, _fake_resp(payload={COMMENTS_KEY: "Hi"}), monkeypatch)
    audits = [o for o in session.added if isinstance(o, AuditLog)]
    [note_row] = [a for a in audits if a.action_type == "add_note"]
    assert note_row.entity_type == "alumni"
    assert note_row.entity_id == 5
    assert note_row.field_name == "note"
    assert note_row.new_value == "Survey comment (2026-10-22): Hi"
    [summary] = [a for a in audits if a.action_type == "apply_survey_response"]
    # Same approval, same change set; the comment is not counted as a field and
    # is not reported as a dropped key.
    assert note_row.change_set_id == summary.change_set_id
    assert "fields=0" in summary.new_value
    assert "dropped=0" in summary.new_value
    assert "comment_note=added" in summary.new_value


def test_apply_without_a_comment_creates_no_note(monkeypatch):
    session = _Session(_alum())
    _apply(session, _fake_resp(payload={"contact.city": "Provo"}), monkeypatch)
    assert _notes(session) == []
    assert not any(
        isinstance(o, AuditLog) and o.action_type == "add_note" for o in session.added
    )


def test_applying_twice_cannot_add_a_second_note(monkeypatch):
    # The first apply flips the row to `applied`; the second goes through the
    # REAL `_get_pending`, which refuses a reviewed row before anything is added.
    session = _Session(_alum())
    resp = _fake_resp(payload={COMMENTS_KEY: "Once only."})
    _apply(session, resp, monkeypatch)
    assert len(_notes(session)) == 1
    assert resp.status == "applied"

    monkeypatch.undo()
    second = _Session(_alum(), response=resp)
    with pytest.raises(InvalidRequestError):
        asyncio.run(sr.apply_response(second, 1, actor_user_id=9))
    assert _notes(second) == []
    assert second.committed == 0


def test_reject_never_creates_a_note():
    resp = _fake_resp(payload={"contact.city": "Provo", COMMENTS_KEY: "Please ignore."})
    session = _Session(_alum(), response=resp)
    asyncio.run(sr.reject_response(session, 1, actor_user_id=9))
    assert resp.status == "rejected"
    assert _notes(session) == []
    assert not any(
        isinstance(o, AuditLog) and o.action_type == "add_note" for o in session.added
    )


def test_note_body_falls_back_to_today_without_a_stamp():
    today = datetime.datetime.now(datetime.UTC).date().isoformat()
    assert sr.survey_comment_note_body("hi", None) == f"Survey comment ({today}): hi"
