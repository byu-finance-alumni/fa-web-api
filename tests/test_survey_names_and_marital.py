"""Alumni can submit their own name (#646); marital status is a fixed choice (#647).

Two changes to the survey whitelist that share one rule, which is what this file
is really about: **the server decides what gets written, not the payload.** The
submit endpoint is public (token-gated), so anything holding a survey link can
POST at ``_FIELDS`` — and the two columns added here are the ones where trusting
that payload is most expensive.

* **Names** are the identity search, exports and the duplicate check all key off.
  They round-trip submit -> review -> apply like every other answer (there is no
  auto-apply path), but a BLANK one is ignored rather than written as NULL: the
  confirm page pre-fills all four boxes, so an empty box means "cleared", never
  "this person has no surname".
* **Marital status** was free text and is now one of four options. An off-list
  answer is IGNORED — not stored, and crucially not written as NULL, because prod
  holds legitimate off-list values ("Separated") that must stay readable.

Plus the #627 reuse: applying a rename now runs the same fuzzy first + last +
graduation-year duplicate check the staff rename path runs, and hands the warning
back to whoever approved it. It never blocks.

Offline: fake sessions and monkeypatched storage, no database and no network.
"""

from __future__ import annotations

import asyncio
import types
import uuid

import pytest

from app.core.dropdowns import MARITAL_STATUSES
from app.models.audit import AuditLog
from app.models.survey_response import SurveyResponse
from app.services import hygiene
from app.services import survey_responses as sr
from app.services.survey_responses import _IGNORE, _after, _coerce, _current

# ------------------------------------------------------------- fakes ---------


class _Result:
    """One canned ``execute`` result that answers BOTH access shapes.

    ``apply_response`` reads the alumnus via ``scalar_one_or_none``; the fuzzy
    duplicate query inside ``hygiene.detect_duplicates`` reads its matches via
    ``scalars().all()``. Different call sites, so one object can serve both.
    """

    def __init__(self, obj, rows):
        self._obj = obj
        self._rows = rows

    def scalar_one_or_none(self):
        return self._obj

    def scalars(self):
        return self

    def all(self):
        return list(self._rows)


class _Session:
    def __init__(self, obj=None, duplicate_rows=()):
        self._obj = obj
        self._rows = list(duplicate_rows)
        self.added = []
        self.committed = 0

    async def execute(self, _stmt):
        return _Result(self._obj, self._rows)

    def add(self, obj):
        self.added.append(obj)

    async def flush(self):
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


def _alum(**kw):
    base = dict(
        alumni_id=5,
        net_id="jdoe5",
        archived=False,
        first_name="Jane",
        middle_name=None,
        last_name="Doe",
        preferred_first_name=None,
        graduation_year=2018,
        marital_status=None,
    )
    base.update(kw)
    return types.SimpleNamespace(**base)


def _apply(session, resp, monkeypatch, side_rows=({}, {}, {})):
    """Run ``apply_response`` with the side-row load stubbed out.

    Returns just the duplicate warnings — these tests are about renames, and the
    photo half of ``ApplyOutcome`` is covered in ``test_survey_photo_hardening``.
    """

    async def fake_get_pending(_s, _rid):
        return resp

    async def fake_side(_s, _ids):
        return side_rows

    monkeypatch.setattr(sr, "_get_pending", fake_get_pending)
    monkeypatch.setattr(sr, "_load_side_rows", fake_side)
    return asyncio.run(sr.apply_response(session, 1, actor_user_id=9)).duplicate_warnings


# =========================================================== names (#646) =====


def test_the_four_name_columns_are_whitelisted():
    # Verified against database/schema.sql: alumni.first_name / middle_name /
    # last_name / preferred_first_name.
    for key, column in (
        ("profile.first_name", "first_name"),
        ("profile.middle_name", "middle_name"),
        ("profile.last_name", "last_name"),
        ("profile.preferred_first_name", "preferred_first_name"),
    ):
        field = sr._FIELD_BY_KEY[key]
        assert (field.group, field.column, field.kind) == ("alumni", column, "text")


def test_middle_name_is_labelled_middle_or_maiden():
    """A decided product call, not a guess: staff have been entering maiden names
    in ``middle_name``, so the LABEL was changed to match the data rather than the
    data migrated to match the label. Pinned because the wording is the whole
    point of the field."""
    assert sr._FIELD_BY_KEY["profile.middle_name"].label == "Middle or Maiden name"


def test_birth_name_is_not_written_by_the_survey():
    """``alumni.birth_name`` stays in the schema and stays UNUSED (#646). It is
    the column you would expect a maiden name to live in, which is exactly why
    this guard exists — pointing the survey at it would split one fact across two
    columns and leave every existing maiden name invisible to it."""
    assert all(f.column != "birth_name" for f in sr._FIELDS)


def test_the_name_block_leads_the_personal_group():
    """The four read as one "Name" question at the head of the personal group,
    immediately before Gender — not scattered through the form."""
    keys = [f.key for f in sr._FIELDS]
    block = [
        "profile.first_name",
        "profile.middle_name",
        "profile.last_name",
        "profile.preferred_first_name",
    ]
    start = keys.index("profile.first_name")
    assert keys[start : start + 4] == block
    assert keys[start + 4] == "profile.gender"


def test_names_round_trip_from_submit_to_apply(monkeypatch):
    # Submit stages them (and NOTHING is written to the record here) ...
    monkeypatch.setattr(sr, "verify_survey_token", lambda _t: 5)
    alum = _alum()
    submit_session = _Session(alum)
    result = asyncio.run(
        sr.submit_response(
            submit_session,
            "tok",
            {
                "profile.first_name": "  Jane ",
                "profile.middle_name": "Whitaker",
                "profile.last_name": "Smith",
                "profile.preferred_first_name": "Janie",
            },
        )
    )
    assert result.staged is True
    assert result.change_count == 4
    staged = next(o for o in submit_session.added if isinstance(o, SurveyResponse))
    assert staged.status == "pending"
    # The review queue is not optional: submit touched no name on the record.
    assert (alum.first_name, alum.last_name) == ("Jane", "Doe")

    # ... and only apply writes them.
    applied_alum = _alum()
    resp = _fake_resp(payload=staged.payload)
    _apply(_Session(applied_alum), resp, monkeypatch)
    assert applied_alum.first_name == "Jane"  # trimmed
    assert applied_alum.middle_name == "Whitaker"
    assert applied_alum.last_name == "Smith"
    assert applied_alum.preferred_first_name == "Janie"
    assert resp.status == "applied"


def test_a_name_change_still_needs_review_and_cannot_be_applied_twice(monkeypatch):
    """No auto-apply path: a rename is staged pending like every other answer, and
    an already-reviewed response is refused."""
    from app.core.errors import InvalidRequestError

    reviewed = _fake_resp(status="applied", payload={"profile.last_name": "Smith"})
    session = _Session(reviewed)
    with pytest.raises(InvalidRequestError):
        asyncio.run(sr.apply_response(session, 1, actor_user_id=9))


@pytest.mark.parametrize(
    "key,stored",
    [
        ("first_name", "Jane"),
        ("middle_name", "Whitaker"),
        ("last_name", "Doe"),
        ("preferred_first_name", "Janie"),
    ],
)
def test_a_blank_name_is_ignored_rather_than_written_as_null(key, stored, monkeypatch):
    """Anything with a survey link can POST here. A blank on a pre-filled name box
    means "cleared or never rendered", so it must leave the column alone — NULLing
    an identity column that search, the exports and the duplicate check key off is
    not an edit a public form gets to stage."""
    field = sr._FIELD_BY_KEY[f"profile.{key}"]
    assert _coerce(field, "") is _IGNORE
    assert _coerce(field, "   ") is _IGNORE

    alum = _alum(
        first_name="Jane", middle_name="Whitaker", last_name="Doe",
        preferred_first_name="Janie",
    )
    session = _Session(alum)
    _apply(session, _fake_resp(payload={f"profile.{key}": ""}), monkeypatch)
    assert getattr(alum, key) == stored
    # Nothing was written, and the audit row says so rather than claiming success.
    audit = next(o for o in session.added if isinstance(o, AuditLog))
    assert "written=0" in audit.new_value
    assert "ignored=1" in audit.new_value


def test_a_blank_name_is_not_staged_at_submit(monkeypatch):
    """Same rule one step earlier: a value apply would ignore must never reach the
    review queue, or a reviewer approves a "change" that silently does nothing."""
    monkeypatch.setattr(sr, "verify_survey_token", lambda _t: 5)
    session = _Session(_alum())
    result = asyncio.run(
        sr.submit_response(
            session, "tok", {"profile.last_name": "", "profile.first_name": "Jane"}
        )
    )
    assert result.change_count == 1
    staged = next(o for o in session.added if isinstance(o, SurveyResponse))
    assert staged.payload == {"profile.first_name": "Jane"}


# The confirm page's side of this — all four boxes pre-filled, and an off-list
# marital status sent verbatim — is asserted in tests/test_survey_email.py, next
# to the rest of `get_respondent`'s coverage and its fixtures.


# ============================================ rename duplicate check (#627) ===


def test_applying_a_rename_runs_the_duplicate_check_and_returns_the_warning(monkeypatch):
    """The #627 check reused on the survey approval path. It is reachable because
    ``hygiene.detect_duplicates`` takes a plain identity dict and a session —
    nothing about it is bound to the staff edit route."""
    other = types.SimpleNamespace(alumni_id=99, first_name="Jane", last_name="Smith")
    alum = _alum(first_name="Jane", last_name="Doe", graduation_year=2018)
    resp = _fake_resp(payload={"profile.last_name": "Smith"})
    session = _Session(alum, duplicate_rows=[other])
    warnings = _apply(session, resp, monkeypatch)

    assert alum.last_name == "Smith"
    assert [w["code"] for w in warnings] == ["possible_duplicate"]
    assert warnings[0]["alumni_id"] == 99
    # Warn-and-continue: the apply completed and committed anyway.
    assert resp.status == "applied"
    assert session.committed == 1


def test_the_duplicate_check_sees_the_POST_rename_identity(monkeypatch):
    """It must be measured against the name the apply PRODUCES, not the one on
    file — checking the old name is the #627 failure in a new place."""
    seen = {}

    async def fake_detect(_session, cleaned, exclude_alumni_id=None):
        seen["cleaned"] = cleaned
        seen["exclude"] = exclude_alumni_id
        return [], []

    monkeypatch.setattr(hygiene, "detect_duplicates", fake_detect)
    alum = _alum(first_name="Jane", last_name="Doe", graduation_year=2018)
    _apply(_Session(alum), _fake_resp(payload={"profile.last_name": "Smith"}), monkeypatch)

    assert seen["cleaned"]["last_name"] == "Smith"
    assert seen["cleaned"]["first_name"] == "Jane"
    assert seen["cleaned"]["graduation_year"] == 2018
    assert seen["exclude"] == 5  # never flags the record against itself
    # byu_id / net_id are deliberately NOT passed: the survey cannot write them,
    # so including them would surface a pre-existing data problem (or an archived
    # ghost) as though this approval had caused it.
    assert "byu_id" not in seen["cleaned"]
    assert "net_id" not in seen["cleaned"]


def test_no_duplicate_query_runs_when_no_name_moved(monkeypatch):
    """One extra query on renames only. Middle / preferred names are not part of
    the dedup identity, so they must not trigger it either."""
    calls = []

    async def fake_detect(*a, **k):
        calls.append(a)
        return [], []

    monkeypatch.setattr(hygiene, "detect_duplicates", fake_detect)
    alum = _alum()
    warnings = _apply(
        _Session(alum),
        _fake_resp(
            payload={
                "profile.middle_name": "Whitaker",
                "profile.preferred_first_name": "Janie",
                "contact.city": "Provo",
            }
        ),
        monkeypatch,
        side_rows=({}, {}, {}),
    )
    assert calls == []
    assert warnings == []


def test_resubmitting_the_same_name_is_not_a_rename(monkeypatch):
    """An alum confirming their details unchanged sends their name back verbatim.
    That is the common case and must not cost a duplicate query."""
    calls = []

    async def fake_detect(*a, **k):
        calls.append(a)
        return [], []

    monkeypatch.setattr(hygiene, "detect_duplicates", fake_detect)
    alum = _alum(first_name="Jane", last_name="Doe")
    _apply(
        _Session(alum),
        _fake_resp(payload={"profile.first_name": "Jane", "profile.last_name": "Doe"}),
        monkeypatch,
    )
    assert calls == []


# ==================================================== marital status (#647) ===


def test_marital_status_is_a_constrained_choice_over_the_canonical_four():
    field = sr._FIELD_BY_KEY["profile.marital_status"]
    assert (field.group, field.column, field.kind) == ("alumni", "marital_status", "choice")
    assert field.options == MARITAL_STATUSES
    assert MARITAL_STATUSES == ("Single", "Married", "Divorced", "Widowed")


@pytest.mark.parametrize("value", MARITAL_STATUSES)
def test_each_canonical_option_is_writable(value):
    assert _coerce(sr._FIELD_BY_KEY["profile.marital_status"], value) == value


def test_casing_drift_resolves_to_the_canonical_option():
    field = sr._FIELD_BY_KEY["profile.marital_status"]
    assert _coerce(field, "married") == "Married"
    assert _coerce(field, "  WIDOWED  ") == "Widowed"
    # And the reviewer's "after" shows what will actually be stored.
    assert _after(field, "married") == "Married"


@pytest.mark.parametrize(
    "value",
    [
        "Separated",
        "It's complicated",
        "Single; DROP TABLE alumni",
        "x" * 200,
        "Partnered",
    ],
)
def test_an_off_list_value_is_never_written(value, monkeypatch):
    """The submit route is public. An unrecognized answer is ignored outright —
    not stored as the free text this field used to be."""
    field = sr._FIELD_BY_KEY["profile.marital_status"]
    assert _coerce(field, value) is _IGNORE

    alum = _alum(marital_status="Married")
    _apply(_Session(alum), _fake_resp(payload={"profile.marital_status": value}), monkeypatch)
    assert alum.marital_status == "Married"  # untouched


def test_an_off_list_value_is_rejected_at_submit(monkeypatch):
    monkeypatch.setattr(sr, "verify_survey_token", lambda _t: 5)
    session = _Session(_alum())
    result = asyncio.run(
        sr.submit_response(
            session,
            "tok",
            {"profile.marital_status": "Separated", "profile.gender": "Female"},
        )
    )
    assert result.change_count == 1
    staged = next(o for o in session.added if isinstance(o, SurveyResponse))
    assert "profile.marital_status" not in staged.payload


def test_an_off_list_stored_value_still_displays():
    """The list constrains what may be WRITTEN, never what may be READ. A stored
    "Separated" has to survive and be shown, here in the reviewer's before/after
    diff and (asserted above) on the confirm page."""
    field = sr._FIELD_BY_KEY["profile.marital_status"]
    assert _current(field, types.SimpleNamespace(marital_status="Separated")) == "Separated"
    assert _current(field, types.SimpleNamespace(marital_status="married")) == "married"


def test_a_blank_marital_status_does_not_blank_the_stored_value(monkeypatch):
    """The case that would destroy the legacy data: an alum whose stored status is
    off-list sees a dropdown with no matching option and leaves it alone. Under
    the old free-text field that blank wrote NULL."""
    field = sr._FIELD_BY_KEY["profile.marital_status"]
    assert _coerce(field, "") is _IGNORE

    alum = _alum(marital_status="Separated")
    _apply(_Session(alum), _fake_resp(payload={"profile.marital_status": ""}), monkeypatch)
    assert alum.marital_status == "Separated"


def test_the_review_queue_drops_a_value_staged_before_the_rule(monkeypatch):
    """Rows staged while the field was free text can still hold an off-list value.
    Apply will ignore them, so the queue must not advertise them as a change the
    reviewer is about to make."""
    resp = types.SimpleNamespace(
        survey_response_id=1,
        alumni_id=5,
        payload={"profile.marital_status": "Separated", "profile.gender": "Female"},
        status="pending",
        staged_photo_path=None,
        submitted_at=__import__("datetime").datetime(2026, 8, 6),
    )
    alum = _alum(marital_status="Married", gender=None)

    class _ListSession:
        async def execute(self, _stmt):
            # list_pending reads responses then alumni, both via .scalars().all().
            return _Result(None, _ListSession.queue.pop(0))

    _ListSession.queue = [[resp], [alum]]

    async def fake_side(_s, _ids):
        return ({}, {}, {})

    monkeypatch.setattr(sr, "_load_side_rows", fake_side)
    items = asyncio.run(sr.list_pending(_ListSession(), 2018))
    keys = [c.field_key for c in items[0].changes]
    assert "profile.marital_status" not in keys
    assert "profile.gender" in keys


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


def test_apply_route_returns_the_duplicate_warnings(client, monkeypatch):
    """The endpoint was a bodyless 204; it now reports what the reviewer could not
    have known before clicking. Never a blocker — still a success status."""
    from app.api.dependencies.auth import get_current_db_user
    from app.main import app
    from app.schemas.auth import UserContext

    app.dependency_overrides[get_current_db_user] = lambda: UserContext(
        user_id=1,
        auth_user_id=uuid.UUID("11111111-1111-1111-1111-111111111111"),
        roles=["super_admin"],
    )

    async def fake_apply(_session, _rid, _actor):
        return sr.ApplyOutcome(
            duplicate_warnings=[
                {
                    "code": "possible_duplicate",
                    "message": "Possible duplicate of Jane Smith (Class of 2018).",
                    "alumni_id": 99,
                }
            ],
            photo_dropped=False,
        )

    monkeypatch.setattr(sr, "apply_response", fake_apply)
    resp = client.post("/survey/responses/5/apply")
    assert resp.status_code == 200
    assert resp.json() == {
        "duplicate_warnings": [
            {
                "code": "possible_duplicate",
                "message": "Possible duplicate of Jane Smith (Class of 2018).",
                "alumni_id": 99,
            }
        ],
        "photo_dropped": False,
    }


def test_apply_route_reports_an_empty_list_when_nothing_collided(client, monkeypatch):
    from app.api.dependencies.auth import get_current_db_user
    from app.main import app
    from app.schemas.auth import UserContext

    app.dependency_overrides[get_current_db_user] = lambda: UserContext(
        user_id=1,
        auth_user_id=uuid.UUID("11111111-1111-1111-1111-111111111111"),
        roles=["super_admin"],
    )

    async def fake_apply(_session, _rid, _actor):
        return sr.ApplyOutcome(duplicate_warnings=[], photo_dropped=False)

    monkeypatch.setattr(sr, "apply_response", fake_apply)
    resp = client.post("/survey/responses/5/apply")
    assert resp.status_code == 200
    assert resp.json() == {"duplicate_warnings": [], "photo_dropped": False}
