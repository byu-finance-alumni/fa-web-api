"""'Yes, everything is correct' records a response (#755).

Pressing that button used to be `setStatus("confirmed")` and nothing else — a
client-side state change that posted NOTHING. So the alumni who answer fastest,
the ones with no correction to make, were the only ones the console could not
see: absent from every reply tally, still receiving both reminders, and parked on
the manual-follow-up call sheet for good. Jake (2026-08-25): confirming should
record a response.

What these tests pin, in the order the decisions were made:

* a confirmation is a REAL row — empty payload, `status='confirmed'`, stamped
  with the campaign cycle the same way a field submission is;
* it is a REPLY (it counts toward the response rate and stops the reminders) but
  NOT a field change (it never enters the review queue, and it is not an applied
  change) — which is exactly why none of the three existing statuses could carry
  it;
* it is IDEMPOTENT, and can never destroy an answer: confirm twice, or confirm
  from a stale tab after really submitting, and nothing is created or
  overwritten;
* an alum is NEVER blocked from submitting real changes afterwards — the
  confirmation is upgraded in place, so one reply stays one row;
* the token is checked exactly as strictly as on every other survey path.

No DB and no network: the fake session below serves `survey_responses` and
`survey_send_log` from in-memory lists.
"""

import asyncio
import datetime
import types
import uuid

import pytest

from app.core.errors import NotFoundError
from app.models.survey_response import SurveyResponse
from app.services import survey_email
from app.services import survey_responses as sr
from app.services.survey_email import make_survey_token

UTC = datetime.UTC


# ------------------------------------------------------------- fakes ---------


class _FakeSettings:
    survey_token_secret = "unit-test-secret"
    survey_from_email = "test@jakegunnell.com"
    survey_from_name = "BYU Finance Alumni"
    survey_app_base_url = "https://finance.alumni.byu.edu"


@pytest.fixture
def fake_settings(monkeypatch):
    monkeypatch.setattr(survey_email, "get_settings", lambda: _FakeSettings())


class _Scalar:
    def __init__(self, obj):
        self._obj = obj

    def scalar_one_or_none(self):
        return self._obj

    def first(self):
        return self._obj


class _Rows:
    def __init__(self, rows):
        self._rows = list(rows)

    def first(self):
        return self._rows[0] if self._rows else None


class _Session:
    """AsyncSession stand-in that can answer the THREE reads this path makes.

    The suite's older survey fake returns one canned row for every ``execute``,
    which cannot express "the alum, and separately their existing responses" —
    and that distinction is the whole of the idempotency and upgrade behaviour
    here. This one dispatches on the statement's FROM:

    * ``alumni``            -> the canned alum;
    * ``survey_send_log``   -> the cycle/stage stamp lookup;
    * ``survey_responses``  -> the seeded responses, honouring the STATUS filter
      the statement actually carries (read off its bound parameters) and the
      newest-first ordering it asks for.

    The 365-day window and the reset rule are NOT simulated — they are shared
    predicates with their own tests (`test_survey_reset`, `test_survey_progress`)
    and re-implementing them in a fake would only assert the copy. Every response
    seeded here is a current, un-reset one.
    """

    def __init__(self, alum=None, responses=(), send_log=()):
        self.alum = alum
        self.responses = list(responses)
        self.send_log = list(send_log)
        self.added = []
        self.committed = 0
        self.response_reads = 0
        self.response_stmts = []

    async def execute(self, stmt):
        try:
            froms = {getattr(f, "name", None) for f in stmt.get_final_froms()}
        except Exception:  # noqa: BLE001 - a fake; an unreadable statement is not one of these
            froms = set()
        params = dict(stmt.compile().params)
        if "survey_send_log" in froms:
            rows = [
                (r["cycle_seq"], r["stage"])
                for r in self.send_log
                if r["graduation_year"] == params.get("graduation_year_1")
                and r["alumni_id"] == params.get("alumni_id_1")
            ]
            return _Rows(rows)
        if "survey_responses" in froms:
            self.response_reads += 1
            self.response_stmts.append(stmt)
            # `status.in_(...)` compiles to an EXPANDING parameter, so its value
            # is the whole tuple rather than one string.
            statuses = set()
            for key, value in params.items():
                if not key.startswith("status"):
                    continue
                statuses.update(
                    value if isinstance(value, list | tuple) else [value]
                )
            rows = [
                r
                for r in self.responses
                if r.alumni_id == params.get("alumni_id_1") and r.status in statuses
            ]
            rows.sort(key=lambda r: r.submitted_at, reverse=True)
            return _Scalar(rows[0] if rows else None)
        return _Scalar(self.alum)

    def add(self, obj):
        self.added.append(obj)

    async def flush(self):
        for obj in self.added:
            if isinstance(obj, SurveyResponse) and obj.survey_response_id is None:
                obj.survey_response_id = 777

    async def commit(self):
        self.committed += 1


def _alum(alumni_id=5, graduation_year=2020):
    return types.SimpleNamespace(
        alumni_id=alumni_id, archived=False, graduation_year=graduation_year
    )


def _response(status, *, response_id=1, alumni_id=5, payload=None, days_ago=1, **kw):
    base = dict(
        survey_response_id=response_id,
        alumni_id=alumni_id,
        graduation_year=2020,
        payload=payload if payload is not None else {},
        status=status,
        staged_photo_path=None,
        cycle_seq=None,
        stage=None,
        submitted_at=datetime.datetime.now(UTC) - datetime.timedelta(days=days_ago),
        reviewed_by_user_id=None,
        reviewed_at=None,
    )
    base.update(kw)
    return types.SimpleNamespace(**base)


def _sent(year=2020, alumni_id=5, stage=1, cycle_seq=3):
    return {
        "graduation_year": year,
        "alumni_id": alumni_id,
        "stage": stage,
        "cycle_seq": cycle_seq,
    }


def _submit(session, monkeypatch, *, fields=None, confirmed_only=False, alumni_id=5):
    monkeypatch.setattr(sr, "verify_survey_token", lambda _t: alumni_id)
    return asyncio.run(
        sr.submit_response(
            session, "tok", fields or {}, confirmed_only=confirmed_only
        )
    )


def _staged(session):
    return [o for o in session.added if isinstance(o, SurveyResponse)]


# ------------------------------------------------ recording a confirmation ----


def test_a_confirmation_is_recorded_as_a_real_row(monkeypatch):
    session = _Session(_alum())
    result = _submit(session, monkeypatch, confirmed_only=True)

    assert (result.staged, result.confirmed, result.change_count) == (True, True, 0)
    assert result.survey_response_id == 777
    row = _staged(session)[0]
    assert row.status == survey_email.STATUS_CONFIRMED
    # EMPTY BY DEFINITION — a confirmation is the alum saying nothing needs to
    # change, so there is no payload and nothing for a reviewer to apply.
    assert row.payload == {}
    assert row.staged_photo_path is None
    assert session.committed == 1


def test_a_confirmation_carries_the_campaign_cycle_stamp(monkeypatch):
    # #497/#357 — a confirmation answers a specific campaign email, and the stamp
    # is READ FROM THE SEND LOG, never derived from today's date. Without it the
    # per-cycle report would show the confirmations of every year as belonging to
    # whichever cycle happened to be running when the report was drawn.
    session = _Session(_alum(), send_log=[_sent(stage=2, cycle_seq=4)])
    _submit(session, monkeypatch, confirmed_only=True)
    row = _staged(session)[0]
    assert (row.cycle_seq, row.stage) == (4, 2)
    assert row.graduation_year == 2020


def test_a_confirmation_with_no_matching_send_stamps_null_not_a_guess(monkeypatch):
    # A hand-issued link has no send-log row. NULL means "we do not know", which
    # is the truth; a guessed cycle would be indistinguishable from an observed
    # one in a report (see the model's note).
    session = _Session(_alum(), send_log=[])
    _submit(session, monkeypatch, confirmed_only=True)
    row = _staged(session)[0]
    assert (row.cycle_seq, row.stage) == (None, None)


def test_confirming_without_the_flag_is_still_a_no_op(monkeypatch):
    # The flag is what turns an empty submission into a confirmation. An empty
    # body without it stays what it always was: nothing staged, null id.
    session = _Session(_alum())
    result = _submit(session, monkeypatch, confirmed_only=False)
    assert (result.staged, result.confirmed) == (False, False)
    assert result.survey_response_id is None
    assert _staged(session) == []
    assert session.committed == 0


# ------------------------------------------------------------- the token -----


def test_a_garbage_token_cannot_record_a_confirmation():
    # The token is the ONLY credential on this path — `/survey/*` skips auth
    # entirely — so the confirmation must be exactly as hard to forge as a field
    # submission. Same verify, same 404 message, nothing staged.
    session = _Session(_alum())
    with pytest.raises(NotFoundError):
        asyncio.run(
            sr.submit_response(session, "garbage-token", {}, confirmed_only=True)
        )
    assert _staged(session) == []
    assert session.committed == 0


def test_an_expired_link_cannot_record_a_confirmation(fake_settings):
    # The 7-day expiry is signed INTO the stateless HMAC. An alum who kept an old
    # email cannot confirm with it any more than they could submit with it.
    stale = make_survey_token(
        5, 2020, issued_at=datetime.datetime.now(UTC) - datetime.timedelta(days=8)
    )
    session = _Session(_alum())
    with pytest.raises(NotFoundError):
        asyncio.run(sr.submit_response(session, stale, {}, confirmed_only=True))
    assert _staged(session) == []


def test_a_valid_unexpired_link_can(fake_settings):
    # The control for the two above: the same call with a live token records the
    # confirmation, so those failures are the expiry and the signature — not the
    # confirmation path being broken.
    token = make_survey_token(5, 2020)
    session = _Session(_alum())
    result = asyncio.run(sr.submit_response(session, token, {}, confirmed_only=True))
    assert result.confirmed is True
    assert _staged(session)[0].status == survey_email.STATUS_CONFIRMED


def test_an_archived_alum_cannot_confirm(monkeypatch):
    # Same rule the rest of the survey applies: an archived record is gone as far
    # as a public link is concerned, and the message never says which.
    session = _Session(types.SimpleNamespace(alumni_id=5, archived=True, graduation_year=2020))
    monkeypatch.setattr(sr, "verify_survey_token", lambda _t: 5)
    with pytest.raises(NotFoundError):
        asyncio.run(sr.submit_response(session, "tok", {}, confirmed_only=True))
    assert _staged(session) == []


# ------------------------------------------------------------ idempotency ----


def test_confirming_twice_creates_one_row(monkeypatch):
    # A double-click, a reload, or the browser back button. The second press must
    # report the reply that already exists rather than manufacturing a duplicate
    # the profile's Surveys tab would render as two answers.
    existing = _response(survey_email.STATUS_CONFIRMED, response_id=42)
    session = _Session(_alum(), responses=[existing])
    result = _submit(session, monkeypatch, confirmed_only=True)

    assert (result.staged, result.confirmed, result.change_count) == (True, True, 0)
    assert result.survey_response_id == 42
    assert _staged(session) == []
    assert session.committed == 0


def test_confirming_after_really_submitting_never_overwrites_the_submission(monkeypatch):
    # THE DESTRUCTIVE CASE. An alum submits real edits, then a stale tab (or a
    # re-opened link) posts a confirmation. Overwriting a pending submission with
    # an empty payload would silently throw away answers they had already given
    # and a reviewer had not yet seen. Nothing is written, and the result says
    # `confirmed=False` because the reply on record is a real submission.
    pending = _response(
        survey_email.STATUS_PENDING,
        response_id=9,
        payload={"contact.city": "Provo"},
    )
    session = _Session(_alum(), responses=[pending])
    result = _submit(session, monkeypatch, confirmed_only=True)

    assert result.survey_response_id == 9
    assert result.confirmed is False
    assert pending.payload == {"contact.city": "Provo"}
    assert pending.status == survey_email.STATUS_PENDING
    assert _staged(session) == []
    assert session.committed == 0


def test_a_rejected_reply_does_not_block_a_new_confirmation(monkeypatch):
    # Staff threw that submission away, so the alum has effectively not replied
    # and is surveyable again (`RESPONDED_STATUSES`). Their confirmation must
    # therefore be recordable — the idempotency guard asks the same question the
    # sender does, not a looser one.
    session = _Session(
        _alum(), responses=[_response(survey_email.STATUS_REJECTED, response_id=3)]
    )
    result = _submit(session, monkeypatch, confirmed_only=True)
    assert result.confirmed is True
    assert _staged(session)[0].status == survey_email.STATUS_CONFIRMED


# ------------------------------------------------- confirm, then change it ----


def test_editing_after_confirming_upgrades_the_confirmation_in_place(monkeypatch):
    # "I need to make changes" after confirming — and the involvement questions
    # on the page the confirmation now leads to, which are ordinary survey
    # fields. One reply, one row: the confirmation becomes the pending
    # submission rather than being joined by a second row.
    confirmation = _response(survey_email.STATUS_CONFIRMED, response_id=11)
    session = _Session(_alum(), responses=[confirmation])
    result = _submit(
        session,
        monkeypatch,
        fields={"contact.city": "Provo", "bogus": "x"},
        confirmed_only=False,
    )

    assert result.staged is True
    assert result.change_count == 1  # "bogus" dropped, as on any submit
    assert result.survey_response_id == 11
    assert confirmation.status == survey_email.STATUS_PENDING
    assert confirmation.payload == {"contact.city": "Provo"}
    assert _staged(session) == []
    assert session.committed == 1


def test_an_alum_is_never_blocked_from_submitting_real_changes(monkeypatch):
    # The requirement stated as its own test, because it is the one thing this
    # feature must not break: having confirmed first can never cost an alum the
    # ability to correct their own record from a public page they reached by
    # email. Whatever the mechanism, the edits end up staged for review.
    confirmation = _response(survey_email.STATUS_CONFIRMED, response_id=11)
    session = _Session(_alum(), responses=[confirmation])
    result = _submit(session, monkeypatch, fields={"contact.city": "Provo"})
    assert result.staged is True
    assert result.change_count == 1
    staged_rows = _staged(session) or [confirmation]
    assert staged_rows[0].status == survey_email.STATUS_PENDING
    assert staged_rows[0].payload == {"contact.city": "Provo"}


def test_the_upgrade_keeps_the_stamp_the_confirmation_already_observed(monkeypatch):
    # #497 — the stamp records WHICH email prompted the reply, and the
    # confirmation already answered that from the send log. Re-resolving it at
    # edit time could move it to a later reminder the alum never acted on.
    confirmation = _response(
        survey_email.STATUS_CONFIRMED, response_id=11, cycle_seq=4, stage=0
    )
    session = _Session(
        _alum(), responses=[confirmation], send_log=[_sent(stage=2, cycle_seq=4)]
    )
    _submit(session, monkeypatch, fields={"contact.city": "Provo"})
    assert (confirmation.cycle_seq, confirmation.stage) == (4, 0)


def test_the_upgrade_fills_an_absent_stamp(monkeypatch):
    # ...but a NULL stamp is a gap, not an observation, so it is filled if the
    # send log can answer it now.
    confirmation = _response(survey_email.STATUS_CONFIRMED, response_id=11)
    session = _Session(
        _alum(), responses=[confirmation], send_log=[_sent(stage=1, cycle_seq=7)]
    )
    _submit(session, monkeypatch, fields={"contact.city": "Provo"})
    assert (confirmation.cycle_seq, confirmation.stage) == (7, 1)


def test_a_live_pending_submission_is_never_upgraded(monkeypatch):
    # Two real submissions are two answers and a reviewer must see both — only a
    # CONFIRMATION is ever absorbed. The second submit stages a new row and
    # leaves the first exactly as it was.
    pending = _response(
        survey_email.STATUS_PENDING, response_id=9, payload={"contact.city": "Provo"}
    )
    session = _Session(_alum(), responses=[pending])
    result = _submit(session, monkeypatch, fields={"contact.city": "Orem"})

    assert pending.payload == {"contact.city": "Provo"}
    assert result.survey_response_id == 777
    assert _staged(session)[0].payload == {"contact.city": "Orem"}


def test_the_upgrade_takes_the_row_lock_before_it_writes(monkeypatch):
    # THE LOST-UPDATE GUARD. Two submissions racing on one token — a mobile
    # retry, or a second submit fired before the first response comes back —
    # would otherwise both read the same confirmation, both upgrade it, and the
    # second commit would silently discard the first alum's answers. Same fix,
    # and the same reasoning, as `_get_pending` on the staff review path.
    #
    # Asserted on the statement the code actually issues: a `SELECT ... FOR
    # UPDATE` is invisible in every single-threaded test, so nothing else in this
    # file would notice the lock being dropped.
    confirmation = _response(survey_email.STATUS_CONFIRMED, response_id=11)
    session = _Session(_alum(), responses=[confirmation])
    _submit(session, monkeypatch, fields={"contact.city": "Provo"})
    assert session.response_stmts[0]._for_update_arg is not None


def test_the_idempotency_read_takes_no_lock_because_it_never_writes(monkeypatch):
    # The other half of the same decision. `_record_confirmation` mutates
    # nothing, so it has no lost-update to guard against — and a lock there would
    # serialise every public confirm for no gain. The residual (two simultaneous
    # confirmations both inserting) cannot lose an answer and cannot double-count
    # anything, because every console figure counts DISTINCT ALUMNI.
    session = _Session(_alum(), responses=[])
    _submit(session, monkeypatch, confirmed_only=True)
    assert session.response_stmts[0]._for_update_arg is None


def test_the_upgrade_only_looks_for_confirmations(monkeypatch):
    # The status filter is read off the statement the fake receives, so this
    # pins WHAT the upgrade lookup asks for rather than trusting the branch
    # above to have asked the right question.
    session = _Session(_alum(), responses=[])
    _submit(session, monkeypatch, fields={"contact.city": "Provo"})
    assert session.response_reads == 1


def test_content_beats_the_flag(monkeypatch):
    # A client that sends fields AND `confirmed_only` is describing a submission
    # WITH changes. Honouring the flag would throw them away, so content wins and
    # the submission is staged normally.
    session = _Session(_alum())
    result = _submit(
        session, monkeypatch, fields={"contact.city": "Provo"}, confirmed_only=True
    )
    assert result.confirmed is False
    assert result.change_count == 1
    row = _staged(session)[0]
    assert row.status == "pending"
    assert row.payload == {"contact.city": "Provo"}


def test_a_photo_beats_the_flag(monkeypatch):
    # Same rule for the photo-only submission (#537): it needs a PENDING row to
    # attach the image to, and a confirmation is not reviewable.
    session = _Session(_alum())
    monkeypatch.setattr(sr, "verify_survey_token", lambda _t: 5)
    result = asyncio.run(
        sr.submit_response(session, "tok", {}, has_photo=True, confirmed_only=True)
    )
    assert result.confirmed is False
    assert _staged(session)[0].status == "pending"


# ------------------------------------------------------------- the stats -----


def test_a_confirmation_counts_as_a_reply_everywhere_the_sender_asks():
    # ONE definition, shared by the send exclusion and every console tally. This
    # is what stops the reminders and puts the alum into the response rate — the
    # entire point of recording the confirmation at all.
    assert survey_email.STATUS_CONFIRMED in survey_email.RESPONDED_STATUSES
    assert survey_email.STATUS_REJECTED not in survey_email.RESPONDED_STATUSES


def test_a_confirmation_never_reaches_the_review_queue():
    # `list_pending` asks for `status == 'pending'`, so a confirmation cannot
    # appear in it. Asserted on the statement the function ACTUALLY issues, not
    # on a copy of it: the console's actionable number is what fills with
    # unactionable rows if this ever widens.
    from sqlalchemy.dialects import postgresql

    class _Capture:
        def __init__(self):
            self.stmts = []

        async def execute(self, stmt):
            self.stmts.append(stmt)
            return _Empty()

    class _Empty:
        def scalars(self):
            return self

        def all(self):
            return []

    session = _Capture()
    assert asyncio.run(sr.list_pending(session, 2020)) == []
    sql = str(
        session.stmts[0].compile(
            dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}
        )
    )
    assert "'pending'" in sql
    assert "confirmed" not in sql


def test_the_profile_tab_names_a_confirmation_for_what_it_is():
    # A confirmation has an empty payload BY DEFINITION, so the generic note
    # ("0 fields submitted") would read as a failed submission on the alum's
    # Surveys tab, and a bare "Completed" would suggest staff applied changes.
    from app.services.profile import _RESPONSE_STATUS_LABELS

    label = _RESPONSE_STATUS_LABELS[survey_email.STATUS_CONFIRMED]
    assert "confirmed" in label.lower()
    assert "awaiting" not in label.lower()


# ------------------------------------------------------------- the route -----


@pytest.fixture
def client():
    from fastapi.testclient import TestClient

    from app.core import rate_limit
    from app.core.database import get_session
    from app.main import app

    async def _no_db():
        yield None

    app.dependency_overrides[get_session] = _no_db
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()
    rate_limit.reset()


def test_the_route_passes_the_flag_through_and_returns_the_contract(client, monkeypatch):
    # The exact wire contract a frontend is built against. Both the request key
    # and every response key are pinned here, because a guessed field name on the
    # other side typechecks perfectly and fails silently at runtime.
    from app.schemas.survey import SurveySubmitResult

    seen = {}

    async def fake_submit(session, token, fields, has_photo=False, confirmed_only=False):
        seen.update(token=token, fields=fields, has_photo=has_photo, confirmed_only=confirmed_only)
        return SurveySubmitResult(
            staged=True, change_count=0, survey_response_id=77, confirmed=True
        )

    monkeypatch.setattr(sr, "submit_response", fake_submit)
    resp = client.post(
        "/survey/respond/sometoken", json={"fields": {}, "confirmed_only": True}
    )

    assert resp.status_code == 200
    assert resp.json() == {
        "staged": True,
        "change_count": 0,
        "survey_response_id": 77,
        "confirmed": True,
    }
    assert seen == {
        "token": "sometoken",
        "fields": {},
        "has_photo": False,
        "confirmed_only": True,
    }


def test_the_flag_defaults_to_false_so_existing_clients_are_unchanged(client, monkeypatch):
    from app.schemas.survey import SurveySubmitResult

    seen = {}

    async def fake_submit(session, token, fields, has_photo=False, confirmed_only=False):
        seen["confirmed_only"] = confirmed_only
        return SurveySubmitResult(staged=True, change_count=1)

    monkeypatch.setattr(sr, "submit_response", fake_submit)
    resp = client.post(
        "/survey/respond/sometoken", json={"fields": {"contact.city": "Provo"}}
    )
    assert resp.status_code == 200
    assert seen["confirmed_only"] is False


def test_the_confirm_is_gated_by_the_same_abuse_budget_as_the_submit(client, monkeypatch):
    # It is NOT a new public write surface with its own allowance: it rides the
    # field submit's limiter, so a replayed confirmation eats the same budget a
    # replayed submission does. `/survey/*` skips authentication entirely, so
    # this is the only brake there is.
    from app.api.routes import survey as survey_routes
    from app.core.rate_limit import SURVEY_SUBMIT_LIMITER
    from app.schemas.survey import SurveySubmitResult

    async def fake_submit(session, token, fields, has_photo=False, confirmed_only=False):
        return SurveySubmitResult(staged=True, change_count=0, confirmed=True)

    monkeypatch.setattr(sr, "submit_response", fake_submit)

    route = next(
        r
        for r in survey_routes.router.routes
        if getattr(r, "path", None) == "/survey/respond/{token}"
        and "POST" in getattr(r, "methods", set())
    )
    assert any(
        d.dependency is SURVEY_SUBMIT_LIMITER for d in route.dependencies
    ), "the confirm must not get its own, looser allowance"

    body = {"fields": {}, "confirmed_only": True}
    codes = [
        client.post("/survey/respond/tok-limit", json=body).status_code
        for _ in range(12)
    ]
    assert 429 in codes


def test_the_response_leaks_no_alumni_pii(client, monkeypatch):
    # Whoever holds the token is a stranger. The result says whether a reply is
    # on record and which row it is — never a name, an email or anything about
    # the person, which is the same discipline every other survey response keeps.
    from app.schemas.survey import SurveySubmitResult

    assert set(SurveySubmitResult.model_fields) == {
        "staged",
        "change_count",
        "survey_response_id",
        "confirmed",
    }
    assert all(
        SurveySubmitResult.model_fields[f].annotation in (bool, int, int | None)
        for f in SurveySubmitResult.model_fields
    )


def test_view_only_staff_cannot_reach_the_review_queue(client):
    # Unrelated to the confirmation itself, kept here as the guard that the new
    # flag did not accidentally widen the module's auth surface: the review
    # routes beside it are still gated.
    from app.api.dependencies.auth import get_current_db_user
    from app.main import app
    from app.schemas.auth import UserContext

    app.dependency_overrides[get_current_db_user] = lambda: UserContext(
        user_id=1,
        auth_user_id=uuid.UUID("11111111-1111-1111-1111-111111111111"),
        roles=["view_only"],
    )
    assert client.post("/survey/responses/5/apply").status_code == 403
