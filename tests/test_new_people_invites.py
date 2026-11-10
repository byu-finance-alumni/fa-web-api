"""Email the new-people survey link from the site (api#661 add-on).

Pinned here:

  * parsing: any mix of commas / semicolons / spaces / new lines, trim +
    lower-case, the 0-token and >50-unique 422s;
  * every skip reason (invalid, duplicate, in_database, recently_invited) and
    that a duplicate is still sent once;
  * the link-off 409, the all-or-nothing budget 409, the busy-lock 409;
  * a Resend failure is a 502 and writes NO ledger row; a success writes one
    hashed row per email (never the address) and returns ``sent_to``;
  * the invite ledger is counted into the shared send usage;
  * auth: 401 without a login, 403 without ``surveys.manage``;
  * the email: subject, intro text, the link, the footer, NO unsubscribe;
  * the migration / RLS / schema registration.

Offline throughout: an in-memory fake session, Resend mocked, no network.
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime
import hashlib
import re
import uuid
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient

from app.api.dependencies.auth import get_current_db_user
from app.core import rate_limit
from app.core.database import get_session
from app.main import app
from app.models.audit import AuditLog
from app.models.new_people import NewPeopleInvite, NewPeopleSurveyLink
from app.schemas.auth import UserContext
from app.schemas.survey import SurveySupportContact
from app.services import mailer, new_people_invites, new_people_survey, survey_email

REPO = Path(__file__).resolve().parents[1]
KEY = "k" * 43
BASE = "https://finance.example.test"
NOW = datetime.datetime.now(datetime.UTC)


def _h(address: str) -> str:
    return hashlib.sha256(address.encode("utf-8")).hexdigest()


# --------------------------------------------------------------- the fake ----


class _Res:
    def __init__(self, rows=(), one=None):
        self._rows = list(rows)
        self._one = one

    def scalar_one_or_none(self):
        return self._one

    def scalars(self):
        return self

    def all(self):
        return list(self._rows)

    def first(self):
        return self._rows[0] if self._rows else None


def _froms(stmt) -> set[str]:
    try:
        return {getattr(f, "name", None) for f in stmt.get_final_froms()}
    except Exception:  # noqa: BLE001 - a fake
        return set()


class FakeSession:
    def __init__(self):
        self.link: NewPeopleSurveyLink | None = None
        # (personal_email, work_email, best_contact) per contact row.
        self.contacts: list[tuple] = []
        # (email_hash, sent_at) ledger rows already committed.
        self.ledger: list[tuple[str, datetime.datetime]] = []
        self.added: list = []
        self.commits = 0

    def seed_link(self, *, enabled=True):
        self.link = NewPeopleSurveyLink(
            id=1, link_key=KEY, enabled=enabled, created_at=NOW, updated_at=NOW
        )

    async def execute(self, stmt):
        froms = _froms(stmt)
        params = stmt.compile().params
        if "new_people_survey_link" in froms:
            return _Res(one=self.link)
        if "alumni_contact_info" in froms:
            wanted = set(params.get("lower_1", []))
            rows = [
                r
                for r in self.contacts
                if any(isinstance(v, str) and v.strip().lower() in wanted for v in r)
            ]
            return _Res(rows=rows)
        if "new_people_invites" in froms:
            hashes = set(params.get("email_hash_1", []))
            cutoff = params["sent_at_1"]
            return _Res(rows=sorted({h for h, at in self.ledger if h in hashes and at >= cutoff}))
        return _Res()

    def add(self, obj):
        self.added.append(obj)

    async def commit(self):
        self.commits += 1

    async def rollback(self):
        pass

    def invites(self):
        return [o for o in self.added if isinstance(o, NewPeopleInvite)]

    def audits(self):
        return [o for o in self.added if isinstance(o, AuditLog)]


def _ctx(*roles: str) -> UserContext:
    return UserContext(
        user_id=42,
        auth_user_id=uuid.UUID("11111111-1111-1111-1111-111111111111"),
        roles=list(roles),
    )


def _as(role: str) -> None:
    app.dependency_overrides[get_current_db_user] = lambda: _ctx(role)


class _Resend:
    """Records each batch; answers with ids, an error status, or a transport error."""

    def __init__(self):
        self.calls: list[dict] = []
        self.status = 200
        self.raise_transport = False

    async def __call__(self, url, *, api_key, payload, timeout):
        self.calls.append({"url": url, "api_key": api_key, "payload": payload})
        if self.raise_transport:
            raise httpx.ConnectError("boom")
        body = (
            {"data": [{"id": f"re_{i}"} for i in range(len(payload))]}
            if self.status == 200
            else {"message": "nope"}
        )
        return httpx.Response(self.status, json=body, request=httpx.Request("POST", url))


@pytest.fixture
def db():
    return FakeSession()


@pytest.fixture
def resend(monkeypatch):
    fake = _Resend()
    monkeypatch.setattr(mailer, "post_json", fake)
    return fake


@pytest.fixture
def state(monkeypatch):
    """Knobs the tests turn: the allowance, the support contact, the lock."""
    knobs = SimpleNamespace(allowance=None, contact=None, lock=True)

    async def _allowance(_session):
        return knobs.allowance

    async def _contact(_session):
        return knobs.contact

    @contextlib.asynccontextmanager
    async def _lock():
        yield knobs.lock

    settings = SimpleNamespace(
        survey_app_base_url=BASE + "/",
        survey_from_email="byufinancealumni@mailing.example.test",
        survey_from_name="BYU Finance Alumni",
        resend_api_key="re_test_key",
    )
    monkeypatch.setattr(survey_email, "remaining_send_allowance", _allowance)
    monkeypatch.setattr(survey_email, "survey_support_contact", _contact)
    monkeypatch.setattr(survey_email, "send_lock", _lock)
    monkeypatch.setattr(new_people_invites, "get_settings", lambda: settings)
    monkeypatch.setattr(new_people_invites, "_LOCK_WAIT_SECONDS", 0.0)
    knobs.settings = settings
    return knobs


@pytest.fixture
def client(db, resend, state):
    async def _session():
        yield db

    app.dependency_overrides[get_session] = _session
    rate_limit.reset()
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.pop(get_session, None)
    app.dependency_overrides.pop(get_current_db_user, None)
    rate_limit.reset()


def _invite(client, emails: str):
    return client.post("/survey/new-people/invite", json={"emails": emails})


def _message(resp) -> str:
    return resp.json()["error"]["message"]


# ----------------------------------------------------------------- parsing ---


def test_parse_splits_on_any_mix_and_normalises():
    candidates, skipped = new_people_invites.parse(
        "  A@Example.org, b@example.org;c@example.org\n\nd@example.org\te@example.org  "
        "<F@Example.org>"
    )
    assert candidates == [
        "a@example.org",
        "b@example.org",
        "c@example.org",
        "d@example.org",
        "e@example.org",
        "f@example.org",
    ]
    assert skipped == []


def test_parse_reports_invalid_once_and_duplicates_once():
    candidates, skipped = new_people_invites.parse(
        "a@example.org, A@example.org , a@example.org, nope, nope, x@y, q@example.org"
    )
    assert candidates == ["a@example.org", "q@example.org"]
    assert [(s.email, s.reason) for s in skipped] == [
        ("a@example.org", "duplicate"),
        ("nope", "invalid"),
        ("x@y", "invalid"),
    ]


@pytest.mark.parametrize(
    "bad", ["a@b@example.org", "a@example.org?x", "<script>@x.org", "a@example.org&b"]
)
def test_parse_uses_the_survey_shape_gate(bad):
    candidates, skipped = new_people_invites.parse(bad)
    assert candidates == []
    assert [s.reason for s in skipped] == ["invalid"]


def test_email_hash_is_sha256_of_the_normalised_address():
    assert new_people_invites.email_hash("  Pat@Example.ORG ") == _h("pat@example.org")


# ----------------------------------------------------------------- the 422s ---


@pytest.mark.parametrize("emails", ["", "   ", " ,;\n\t "])
def test_nothing_to_send_is_422(client, db, resend, emails):
    db.seed_link()
    _as("super_admin")
    resp = _invite(client, emails)
    assert resp.status_code == 422
    assert resend.calls == []


def test_more_than_50_unique_is_422(client, db, resend):
    db.seed_link()
    _as("super_admin")
    resp = _invite(client, ",".join(f"p{i}@example.org" for i in range(51)))
    assert resp.status_code == 422
    assert _message(resp) == "You can send to up to 50 addresses at a time."
    assert resend.calls == [] and db.invites() == []


def test_exactly_50_unique_with_repeats_is_allowed(client, db, resend):
    db.seed_link()
    _as("super_admin")
    addresses = [f"p{i}@example.org" for i in range(50)]
    resp = _invite(client, "\n".join(addresses + addresses[:10]))
    assert resp.status_code == 200
    assert resp.json()["sent"] == 50
    assert len(resend.calls) == 1 and len(resend.calls[0]["payload"]) == 50


def test_over_20000_chars_is_422(client, db, resend):
    db.seed_link()
    _as("super_admin")
    resp = _invite(client, "a" * 20_001)
    assert resp.status_code == 422
    assert resend.calls == []


# ---------------------------------------------------------------- the 409s ---


@pytest.mark.parametrize("seed", [True, False])
def test_link_off_or_missing_is_409(client, db, resend, seed):
    if seed:
        db.seed_link(enabled=False)
    _as("super_admin")
    resp = _invite(client, "a@example.org")
    assert resp.status_code == 409
    assert _message(resp) == "The survey link is turned off."
    assert resend.calls == [] and db.invites() == []


def test_over_budget_refuses_the_whole_send(client, db, resend, state):
    db.seed_link()
    state.allowance = 1
    _as("super_admin")
    resp = _invite(client, "a@example.org, b@example.org")
    assert resp.status_code == 409
    assert _message(resp) == "Only 1 emails left in today's sending budget."
    assert resend.calls == [] and db.invites() == []


def test_budget_counts_only_what_would_actually_send(client, db, resend, state):
    """Skipped addresses cost nothing, so a send that fits after skips goes."""
    db.seed_link()
    db.contacts.append(("b@example.org", None, None))
    state.allowance = 1
    _as("super_admin")
    resp = _invite(client, "a@example.org, b@example.org")
    assert resp.status_code == 200
    assert resp.json()["sent_to"] == ["a@example.org"]


def test_unlimited_budget_does_not_block(client, db, resend, state):
    db.seed_link()
    state.allowance = None
    _as("super_admin")
    assert _invite(client, "a@example.org").status_code == 200


def test_a_running_survey_send_is_409_not_a_wait_forever(client, db, resend, state):
    db.seed_link()
    state.lock = False
    _as("super_admin")
    resp = _invite(client, "a@example.org")
    assert resp.status_code == 409
    assert resend.calls == [] and db.invites() == []


# ------------------------------------------------------------- the skips ----


def test_each_skip_reason(client, db, resend):
    db.seed_link()
    db.contacts += [
        ("Known@Example.org", None, None),
        (None, "work@example.org", None),
        (None, None, " best@example.org "),
    ]
    db.ledger += [
        (_h("recent@example.org"), NOW - datetime.timedelta(days=29)),
        (_h("old@example.org"), NOW - datetime.timedelta(days=31)),
    ]
    _as("super_admin")
    resp = _invite(
        client,
        "new@example.org, NEW@example.org, junk, known@example.org, work@example.org, "
        "best@example.org, recent@example.org, old@example.org",
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["sent"] == 2
    assert body["sent_to"] == ["new@example.org", "old@example.org"]
    assert sorted((s["email"], s["reason"]) for s in body["skipped"]) == [
        ("best@example.org", "in_database"),
        ("junk", "invalid"),
        ("known@example.org", "in_database"),
        ("new@example.org", "duplicate"),
        ("recent@example.org", "recently_invited"),
        ("work@example.org", "in_database"),
    ]
    sent = [m["to"] for m in resend.calls[0]["payload"]]
    assert sent == [["new@example.org"], ["old@example.org"]]


def test_all_skipped_is_200_sent_zero_without_calling_resend(client, db, resend):
    db.seed_link()
    db.contacts.append(("a@example.org", None, None))
    _as("super_admin")
    resp = _invite(client, "a@example.org, nope")
    assert resp.status_code == 200
    assert resp.json()["sent"] == 0 and resp.json()["sent_to"] == []
    assert resend.calls == [] and db.invites() == []


# ----------------------------------------------------------------- Resend ---


@pytest.mark.parametrize("mode", ["status", "transport"])
def test_resend_failure_is_502_and_writes_no_ledger(client, db, resend, mode):
    db.seed_link()
    if mode == "status":
        resend.status = 422
    else:
        resend.raise_transport = True
    _as("super_admin")
    resp = _invite(client, "a@example.org, b@example.org")
    assert resp.status_code == 502
    assert _message(resp) == "The email service didn't accept the send. Nothing was sent."
    assert db.invites() == []
    assert db.commits == 0


def test_success_writes_hashed_ledger_rows_and_returns_sent_to(client, db, resend):
    db.seed_link()
    _as("super_admin")
    resp = _invite(client, "A@example.org\nb@example.org")
    assert resp.status_code == 200
    assert resp.json() == {
        "sent": 2,
        "sent_to": ["a@example.org", "b@example.org"],
        "skipped": [],
    }
    rows = db.invites()
    assert [r.email_hash for r in rows] == [_h("a@example.org"), _h("b@example.org")]
    assert [r.resend_email_id for r in rows] == ["re_0", "re_1"]
    assert all(r.sent_by_user_id == 42 for r in rows)
    # Never the address, anywhere on the row.
    for r in rows:
        assert "example.org" not in repr(vars(r))
    # One audit row, counts only.
    (audit,) = db.audits()
    assert audit.action_type == "send_new_people_invites"
    assert audit.new_value == "sent=2 skipped=0"
    assert "example.org" not in (audit.new_value or "")
    assert db.commits == 1


def test_one_batch_call_with_the_survey_sender(client, db, resend, state):
    db.seed_link()
    _as("super_admin")
    _invite(client, "a@example.org, b@example.org")
    (call,) = resend.calls
    assert call["url"] == mailer.RESEND_BATCH_URL
    assert call["api_key"] == "re_test_key"
    for msg in call["payload"]:
        assert msg["from"] == "BYU Finance Alumni <byufinancealumni@mailing.example.test>"
        assert msg["subject"] == "Join the BYU Finance Alumni Network"
        assert len(msg["to"]) == 1


def test_missing_mail_config_is_502_before_sending(client, db, resend, state):
    db.seed_link()
    state.settings.resend_api_key = None
    _as("super_admin")
    resp = _invite(client, "a@example.org")
    assert resp.status_code == 502
    assert resend.calls == [] and db.invites() == []


# -------------------------------------------------------------------- email ---


def test_email_body_has_intro_link_footer_and_no_unsubscribe(client, db, resend):
    db.seed_link()
    _as("super_admin")
    _invite(client, "a@example.org")
    msg = resend.calls[0]["payload"][0]
    link = f"{BASE}/survey/new/{KEY}"
    for part in (msg["html"], msg["text"]):
        assert link in part
        assert "Start the survey" in part
        assert "Tanya Harmon" in part and "tanya_harmon@byu.edu" in part
        assert "unsubscribe" not in part.lower()
        for paragraph in new_people_survey.NEW_PEOPLE_INTRO_PARAGRAPHS:
            # The HTML escapes the apostrophes.
            assert paragraph in part or paragraph.replace("'", "&#x27;") in part
    assert "Questions? Email Tanya Harmon at tanya_harmon@byu.edu" in msg["text"]
    assert f'href="{link}"' in msg["html"]


def test_footer_uses_the_survey_support_contact_when_set(client, db, resend, state):
    db.seed_link()
    state.contact = SurveySupportContact(name="Amy <b>D</b>", email="amy@example.org")
    _as("super_admin")
    _invite(client, "a@example.org")
    msg = resend.calls[0]["payload"][0]
    assert "Questions? Email Amy <b>D</b> at amy@example.org" in msg["text"]
    assert "Amy &lt;b&gt;D&lt;/b&gt;" in msg["html"]
    assert "<b>D</b>" not in msg["html"]


def test_render_escapes_the_link():
    _, html, _ = new_people_invites.render_invite_email(
        'https://x.test/survey/new/a"><script>', "T", "t@example.org"
    )
    assert "<script>" not in html
    assert "&quot;&gt;&lt;script&gt;" in html


def test_backend_intro_matches_the_frontend_copy_verbatim():
    """The public page renders its own copy; keep them identical (see the
    constant's comment). Pinned here so a backend-only edit is a deliberate one."""
    assert new_people_survey.NEW_PEOPLE_INTRO_PARAGRAPHS == (
        "Whether you're a BYU Finance alum or a professional who stays engaged with our "
        "students, your experience and willingness to help are a valued part of our community.",
        "We're updating our records so we can stay connected with you throughout your career. "
        "Please take a moment to complete this survey with your current career and contact "
        "information. A few optional questions will also help us understand how you'd like to "
        "stay involved and support our students.",
        "Thank you for being part of the BYU Finance community. We look forward to staying "
        "connected with you in the years ahead!",
    )
    assert new_people_survey.NEW_PEOPLE_INTRO_SIGNOFF == (
        "Warmest regards,",
        "Tanya Harmon & Amy Densley",
        "BYU Finance Career Directors",
    )


# -------------------------------------------------------------------- auth ----


def test_invite_requires_login(client, resend):
    assert _invite(client, "a@example.org").status_code == 401
    assert resend.calls == []


@pytest.mark.parametrize("role", ["view_only", "student"])
def test_invite_forbids_non_managers(client, db, resend, role):
    db.seed_link()
    _as(role)
    assert _invite(client, "a@example.org").status_code == 403
    assert resend.calls == [] and db.invites() == []


def test_invite_is_rate_limited(client, db, resend):
    db.seed_link()
    db.contacts.append(("a@example.org", None, None))  # skip -> no Resend call
    _as("super_admin")
    codes = [_invite(client, "a@example.org").status_code for _ in range(11)]
    assert codes[:10] == [200] * 10
    assert codes[10] == 429


# ------------------------------------------------------------------- usage ---


class _UsageSession:
    def __init__(self, survey=(0, 0), invites=(0, 0), invites_break=False):
        self._survey = survey  # (month, today)
        self._invites = invites
        self._break = invites_break

    @contextlib.asynccontextmanager
    async def begin_nested(self):
        yield

    async def execute(self, stmt):
        sql = str(stmt)
        if "new_people_invites" in sql:
            if self._break:
                raise RuntimeError('relation "new_people_invites" does not exist')
            row = self._invites
        elif "opportunity_link_digest_send_log" in sql:
            row = (0, 0)
        else:
            row = self._survey
        return SimpleNamespace(first=lambda: row)


@pytest.fixture
def no_baseline(monkeypatch):
    monkeypatch.setattr(
        survey_email,
        "get_settings",
        lambda: SimpleNamespace(
            survey_usage_baseline_at=None,
            survey_usage_baseline_today=0,
            survey_usage_baseline_month=0,
        ),
    )


def test_usage_counts_invites_in_the_day_and_the_month(no_baseline):
    usage = asyncio.run(survey_email.get_send_usage(_UsageSession(survey=(31, 7), invites=(5, 3))))
    assert (usage.sent_today, usage.sent_this_month) == (10, 36)


def test_an_unreadable_invite_ledger_counts_zero(no_baseline):
    usage = asyncio.run(
        survey_email.get_send_usage(_UsageSession(survey=(31, 7), invites_break=True))
    )
    assert (usage.sent_today, usage.sent_this_month) == (7, 31)


def test_invite_count_respects_the_baseline_anchor():
    anchor = datetime.datetime(2026, 11, 10, tzinfo=datetime.UTC)
    seen = {}

    class _S(_UsageSession):
        async def execute(self, stmt):
            seen["sql"] = str(stmt.compile(compile_kwargs={"literal_binds": True}))
            return SimpleNamespace(first=lambda: (0, 0))

    asyncio.run(
        new_people_invites.sent_counts(
            _S(), start_today=anchor, start_month=anchor.replace(day=1), after=anchor
        )
    )
    assert "new_people_invites.sent_at >" in seen["sql"]


# --------------------------------------------------------------- migration ---


def test_migration_is_additive_and_locked_down():
    sql = (REPO / "database" / "migrations" / "2026-11-10_new_people_invites.sql").read_text(
        encoding="utf-8"
    )
    body = "\n".join(line for line in sql.splitlines() if not line.lstrip().startswith("--"))
    body_no_fk = body.replace("ON DELETE SET NULL", "")
    assert not re.search(r"\b(DROP|TRUNCATE|DELETE|UPDATE)\b", body_no_fk, re.IGNORECASE)
    assert "CREATE TABLE IF NOT EXISTS new_people_invites" in body
    assert "email_hash" in body and "resend_email_id" in body
    # No column could hold an address.
    assert not re.search(r"\b(email|address|sent_to)\s+(text|varchar)", body, re.IGNORECASE)
    assert "ALTER TABLE new_people_invites ENABLE ROW LEVEL SECURITY;" in body
    rls = (REPO / "database" / "rls_lockdown.sql").read_text(encoding="utf-8")
    assert "ALTER TABLE new_people_invites ENABLE ROW LEVEL SECURITY;" in rls
    schema = (REPO / "database" / "schema.sql").read_text(encoding="utf-8")
    assert "CREATE TABLE new_people_invites (" in schema


def test_openapi_names_the_contract_schemas():
    spec = app.openapi()
    schemas = spec["components"]["schemas"]
    assert set(schemas["NewPeopleInviteRequest"]["properties"]) == {"emails"}
    assert set(schemas["NewPeopleInviteResult"]["properties"]) == {"sent", "sent_to", "skipped"}
    op = spec["paths"]["/survey/new-people/invite"]["post"]
    ref = op["responses"]["200"]["content"]["application/json"]["schema"]["$ref"]
    assert ref.endswith("/NewPeopleInviteResult")
