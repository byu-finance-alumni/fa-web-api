"""The new-people survey (api#661): one shared link, a holding queue, a review.

Pinned here:

  * PUBLIC routes: the contract's required-field groups, 404 with one message
    for a wrong key AND a switched-off link, the honeypot (200, nothing stored),
    the survey's 413 size caps, required-group 422s that name the keys, the
    global pending cap (429), per-IP rate limiting, Cache-Control: no-store.
  * STAFF routes: every one is 403 without ``surveys.manage``; the link is
    created disabled, switched, reissued (old key dies) and never audited with
    its key; the queue lists pending only, oldest first, with possible matches.
  * APPROVE: create alum (grad year + degree land on real columns), create
    friend, merge onto an existing record through the survey's own apply
    machinery, comment saved as a Note, the 409 stale check, no double approve.
  * REJECT purges the held answers and audits without PII.
  * Held rows never become alumni or survey responses until approved.

Offline throughout: an in-memory fake session, no Postgres, no network.
"""

from __future__ import annotations

import datetime
import re
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.api.dependencies.auth import get_current_db_user
from app.core import rate_limit
from app.core.audit_context import reset_audit_actor, reset_audit_source
from app.core.database import get_session
from app.main import app
from app.models.alumni import Alumni
from app.models.audit import AuditLog
from app.models.contact import AlumniContactInfo
from app.models.employment import CurrentEmployment, EducationHistory
from app.models.engagement import AlumniProgramEngagement
from app.models.new_people import NewPeopleSurveyLink, NewPersonSubmission
from app.models.note import Note
from app.models.survey_response import SurveyResponse
from app.schemas.auth import UserContext
from app.services import new_people_survey as nps
from app.services import survey_email

REPO = Path(__file__).resolve().parents[1]
KEY = "k" * 43
OTHER_KEY = "x" * 43
T0 = datetime.datetime(2026, 11, 10, 15, 30, 0, 123456, tzinfo=datetime.UTC)


@pytest.fixture(autouse=True)
def _clean_audit_context():
    reset_audit_source()
    reset_audit_actor()
    yield
    reset_audit_source()
    reset_audit_actor()


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


def _names(from_) -> set[str]:
    if hasattr(from_, "left") and hasattr(from_, "right"):  # a JOIN
        return _names(from_.left) | _names(from_.right)
    return {getattr(from_, "name", None)}


def _froms(stmt) -> set[str]:
    try:
        return set().union(*(_names(f) for f in stmt.get_final_froms()))
    except Exception:  # noqa: BLE001 - a fake
        return set()


class FakeSession:
    """Just enough of an AsyncSession for the new-people service and the shared
    survey apply. Tables are dicts; SELECTs are answered by the table they read."""

    def __init__(self):
        self.link: NewPeopleSurveyLink | None = None
        self.subs: dict[int, NewPersonSubmission] = {}
        self.alumni: dict[int, Alumni] = {}
        # Canned rows for the two possible-match lookups.
        self.email_rows: list[tuple] = []
        self.name_rows: list[tuple] = []
        self.pending_override: int | None = None
        # table name -> rows answered for `_load_side_rows`.
        self.side: dict[str, list] = {}
        self.locks = 0
        self.locks_at_count: list[int] = []
        self.added: list = []
        self.commits = 0
        self._next_alumni_id = 9001
        self._next_sub_id = 1

    # -- seeding --
    def seed_link(self, *, enabled=True, key=KEY):
        self.link = NewPeopleSurveyLink(
            id=1, link_key=key, enabled=enabled, created_at=T0, updated_at=T0
        )
        return self.link

    def seed_sub(self, *, kind="alum", payload=None, comments=None, submitted_at=T0):
        sub = NewPersonSubmission(
            submission_id=self._next_sub_id,
            kind=kind,
            payload=payload if payload is not None else _alum_payload(),
            comments=comments,
            status="pending",
            submitted_at=submitted_at,
        )
        self.subs[sub.submission_id] = sub
        self._next_sub_id += 1
        return sub

    # -- session surface --
    async def execute(self, stmt):
        froms = _froms(stmt)
        params = stmt.compile().params
        if "new_people_survey_link" in froms:
            return _Res(one=self.link)
        if "new_people_submissions" in froms:
            ids = params.get("submission_id_1")
            if isinstance(ids, list):  # bulk reject
                rows = [
                    self.subs[i]
                    for i in sorted(ids)
                    if i in self.subs and self.subs[i].status == "pending"
                ]
                return _Res(rows=rows)
            if ids is not None:
                return _Res(one=self.subs.get(ids))
            if "payload_1" in params:  # the submit's email dedupe lookup
                emails = set(params.get("lower_1", [])) | set(params.get("lower_2", []))
                hits = [
                    sub
                    for sub in self.subs.values()
                    if sub.status == "pending"
                    and any(
                        (sub.payload.get(k) or "").strip().lower() in emails
                        for k in ("contact.personal_email", "contact.work_email")
                    )
                ]
                return _Res(one=hits[-1] if hits else None)
            pending = sorted(
                (s for s in self.subs.values() if s.status == "pending"),
                key=lambda s: (s.submitted_at, s.submission_id),
            )
            return _Res(rows=pending)
        if froms == {"alumni", "alumni_contact_info"}:
            return _Res(rows=self.email_rows)
        if froms == {"alumni"}:
            cols = stmt.column_descriptions
            if len(cols) == 1 and cols[0].get("expr") is Alumni:
                if "alumni_id_1" in params:
                    return _Res(one=self.alumni.get(params["alumni_id_1"]))
                return _Res(rows=[])  # the fuzzy duplicate check: no twins
            return _Res(rows=self.name_rows)
        # Side rows (contact / employment / engagement) as seeded; history none.
        if len(froms) == 1:
            (name,) = froms
            if name in self.side:
                return _Res(rows=self.side[name])
        if any("pg_advisory_xact_lock" in str(c) for c in getattr(stmt, "selected_columns", [])):
            self.locks += 1
        return _Res(rows=[])

    async def scalar(self, stmt):
        self.locks_at_count.append(self.locks)
        if self.pending_override is not None:
            return self.pending_override
        return sum(1 for s in self.subs.values() if s.status == "pending")

    def add(self, obj):
        self.added.append(obj)
        if isinstance(obj, NewPeopleSurveyLink):
            self.link = obj

    async def flush(self):
        for obj in self.added:
            if isinstance(obj, Alumni) and obj.alumni_id is None:
                obj.alumni_id = self._next_alumni_id
                self._next_alumni_id += 1
                self.alumni[obj.alumni_id] = obj

    async def commit(self):
        await self.flush()
        for obj in self.added:
            if isinstance(obj, NewPersonSubmission) and obj.submission_id is None:
                obj.submission_id = self._next_sub_id
                obj.submitted_at = T0
                self._next_sub_id += 1
                self.subs[obj.submission_id] = obj
        self.commits += 1

    async def rollback(self):
        pass

    # -- helpers --
    def added_of(self, cls):
        return [o for o in self.added if isinstance(o, cls)]

    def audits(self, action=None):
        rows = self.added_of(AuditLog)
        return [a for a in rows if action is None or a.action_type == action]


def _alum_payload(**extra):
    payload = {
        "profile.first_name": "Pat",
        "profile.last_name": "Quinn",
        "contact.personal_email": "pat@example.org",
        "employment.current_employer": "Acme Capital",
        "employment.current_industry": "Investment Banking",
        "employment.current_city": "Denver",
        "employment.current_country": "United States",
        "profile.linkedin_url": "https://www.linkedin.com/in/patquinn",
        "new.graduation_year": "2009",
        "new.byu_degree": "BS Finance",
    }
    payload.update(extra)
    return payload


def _friend_fields(**extra):
    fields = {k: v for k, v in _alum_payload().items() if not k.startswith("new.")}
    fields.update(extra)
    return fields


def _ctx(*roles: str) -> UserContext:
    return UserContext(
        user_id=42,
        auth_user_id=uuid.UUID("11111111-1111-1111-1111-111111111111"),
        roles=list(roles),
    )


@pytest.fixture
def db():
    return FakeSession()


@pytest.fixture
def client(db, monkeypatch):
    async def _session():
        yield db

    async def _no_contact(_session):
        return None

    monkeypatch.setattr(survey_email, "survey_support_contact", _no_contact)
    app.dependency_overrides[get_session] = _session
    rate_limit.reset()
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.pop(get_session, None)
    app.dependency_overrides.pop(get_current_db_user, None)
    rate_limit.reset()


def _as(role: str) -> None:
    app.dependency_overrides[get_current_db_user] = lambda: _ctx(role)


def _post(client, key=KEY, **body):
    body.setdefault("kind", "alum")
    body.setdefault("fields", _alum_payload())
    return client.post(f"/survey/new/{key}", json=body)


# ----------------------------------------------------------- public: GET -----


def test_required_fields_match_the_contract(client, db):
    db.seed_link()
    resp = client.get(f"/survey/new/{KEY}")
    assert resp.status_code == 200
    assert resp.headers["cache-control"] == "no-store"
    friend = [
        ["profile.first_name"],
        ["profile.last_name"],
        ["contact.personal_email", "contact.work_email"],
        ["employment.current_employer"],
        ["employment.current_industry"],
        ["employment.current_city"],
        ["employment.current_country"],
        ["profile.linkedin_url"],
    ]
    assert resp.json() == {
        "required_fields": {
            "alum": friend + [["new.graduation_year"], ["new.byu_degree"]],
            "friend": friend,
        },
        "support_contact": None,
    }


@pytest.mark.parametrize("setup", ["wrong_key", "disabled", "no_link"])
def test_wrong_key_disabled_and_missing_link_all_404_alike(client, db, setup):
    if setup == "wrong_key":
        db.seed_link(enabled=True)
        key = OTHER_KEY
    elif setup == "disabled":
        db.seed_link(enabled=False)
        key = KEY
    else:
        key = KEY
    for resp in (client.get(f"/survey/new/{key}"), _post(client, key=key)):
        assert resp.status_code == 404
        assert resp.json()["error"]["message"] == "This survey link is closed."
    assert db.added_of(NewPersonSubmission) == []


def test_key_comparison_is_constant_time_even_when_disabled(db, monkeypatch):
    import asyncio

    calls = []
    real = nps.hmac.compare_digest

    def spy(a, b):
        calls.append((a, b))
        return real(a, b)

    monkeypatch.setattr(nps.hmac, "compare_digest", spy)
    db.seed_link(enabled=False)
    with pytest.raises(nps.NotFoundError):
        asyncio.run(nps.verify_key(db, KEY))
    assert calls, "the key must be compared with hmac.compare_digest"


# ---------------------------------------------------------- public: POST -----


def test_alum_submission_is_held_cleaned_and_not_applied(client, db):
    db.seed_link()
    fields = _alum_payload(
        **{
            "employment.current_title": "  VP  ",
            "program.mentor_willing": "yes",
            "unknown.key": "dropped",
            "contact.phone": "",
        }
    )
    resp = _post(client, fields=fields, comments="  Happy to help​!  ", fill_seconds=10**9)
    assert resp.status_code == 200
    assert resp.json() == {"received": True}
    assert resp.headers["cache-control"] == "no-store"
    (sub,) = db.added_of(NewPersonSubmission)
    assert sub.kind == "alum"
    assert sub.status == "pending"
    assert sub.payload["employment.current_title"] == "VP"
    assert sub.payload["new.graduation_year"] == "2009"
    assert sub.payload["new.byu_degree"] == "BS Finance"
    assert sub.payload["program.mentor_willing"] == "yes"
    assert "unknown.key" not in sub.payload
    assert "contact.phone" not in sub.payload  # blanks are not held
    assert sub.comments == "Happy to help!"
    assert sub.fill_seconds is None  # absurd timer dropped, not stored
    # Nothing touched the real tables.
    assert db.added_of(Alumni) == []
    assert db.added_of(SurveyResponse) == []


def test_friend_submission_needs_no_byu_fields_and_drops_them(client, db):
    db.seed_link()
    fields = _friend_fields(**{"new.graduation_year": "2001", "new.byu_degree": "MBA"})
    resp = _post(client, kind="friend", fields=fields, fill_seconds=300)
    assert resp.status_code == 200
    (sub,) = db.added_of(NewPersonSubmission)
    assert sub.kind == "friend"
    assert not any(k.startswith("new.") for k in sub.payload)
    assert sub.fill_seconds == 300


def test_work_email_alone_satisfies_the_email_group(client, db):
    db.seed_link()
    fields = _friend_fields()
    del fields["contact.personal_email"]
    fields["contact.work_email"] = "pat@acme.example"
    assert _post(client, kind="friend", fields=fields).status_code == 200


@pytest.mark.parametrize(
    ("drop", "expect"),
    [
        ("profile.first_name", ["profile.first_name"]),
        ("employment.current_industry", ["employment.current_industry"]),
        ("profile.linkedin_url", ["profile.linkedin_url"]),
        ("new.graduation_year", ["new.graduation_year"]),
        ("new.byu_degree", ["new.byu_degree"]),
        ("contact.personal_email", ["contact.personal_email", "contact.work_email"]),
    ],
)
def test_missing_required_group_is_422_naming_keys(client, db, drop, expect):
    db.seed_link()
    fields = _alum_payload()
    del fields[drop]
    resp = _post(client, fields=fields)
    assert resp.status_code == 422
    err = resp.json()["error"]
    assert err["code"] == "validation_error"
    assert [f["field"] for f in err["fields"]] == expect
    assert db.added_of(NewPersonSubmission) == []


@pytest.mark.parametrize(
    ("key", "bad"),
    [
        ("new.graduation_year", "19x5"),
        ("new.graduation_year", "1850"),
        ("new.graduation_year", "09"),
        ("new.graduation_year", "3000"),
        ("new.byu_degree", "=HYPERLINK(1)"),
        ("new.byu_degree", "B" * 101),
        ("profile.linkedin_url", "javascript:alert(1)"),
        ("employment.current_industry", "Made Up Industry"),
        ("contact.personal_email", "a@b.com, evil@x.com"),
    ],
)
def test_unusable_required_answer_is_422_not_valid(client, db, key, bad):
    db.seed_link()
    resp = _post(client, fields=_alum_payload(**{key: bad}))
    assert resp.status_code == 422
    fields = resp.json()["error"]["fields"]
    assert key in [f["field"] for f in fields]
    assert all(bad not in f["message"] for f in fields)  # never echoes the value
    assert db.added_of(NewPersonSubmission) == []


def test_unusable_optional_answer_is_dropped_silently(client, db):
    db.seed_link()
    resp = _post(client, fields=_alum_payload(**{"profile.birth_date": "0001-01-01"}))
    assert resp.status_code == 200
    (sub,) = db.added_of(NewPersonSubmission)
    assert "profile.birth_date" not in sub.payload


def test_honeypot_returns_200_and_stores_nothing(client, db):
    db.seed_link()
    resp = _post(client, website="http://spam.example")
    assert resp.status_code == 200
    assert resp.json() == {"received": True}
    assert db.added == []
    assert db.commits == 0


def test_honeypot_with_wrong_key_is_still_404(client, db):
    db.seed_link()
    assert _post(client, key=OTHER_KEY, website="x").status_code == 404


def test_one_oversized_answer_is_413(client, db):
    db.seed_link()
    resp = _post(client, fields=_alum_payload(**{"profile.other_designations": "x" * 41000}))
    assert resp.status_code == 413
    assert resp.json()["error"]["code"] == "payload_too_large"
    assert db.added_of(NewPersonSubmission) == []


def test_oversized_total_is_413(client, db):
    db.seed_link()
    junk = {f"junk.{i}": "y" * 4000 for i in range(20)}
    resp = _post(client, fields={**_alum_payload(), **junk})
    assert resp.status_code == 413
    assert db.added_of(NewPersonSubmission) == []


def test_over_cap_comment_is_422(client, db):
    db.seed_link()
    resp = _post(client, comments="c" * 2001)
    assert resp.status_code == 422
    assert db.added_of(NewPersonSubmission) == []


def test_pending_cap_refuses_politely(client, db):
    db.seed_link()
    db.pending_override = nps.PENDING_CAP
    resp = _post(client)
    assert resp.status_code == 429
    assert resp.json()["error"]["code"] == "rate_limited"
    assert db.added_of(NewPersonSubmission) == []
    db.pending_override = nps.PENDING_CAP - 1
    assert _post(client).status_code == 200


def test_submit_is_rate_limited_per_client_ip(client, db):
    db.seed_link()
    statuses = [_post(client).status_code for _ in range(11)]
    assert statuses[:10] == [200] * 10
    assert statuses[10] == 429


def test_rate_limit_uses_the_edge_hop_not_a_spoofed_one(client, db):
    db.seed_link()
    for i in range(10):
        headers = {"x-forwarded-for": f"198.51.100.{i}, 203.0.113.7"}
        resp = client.post(
            f"/survey/new/{KEY}",
            json={"kind": "alum", "fields": _alum_payload()},
            headers=headers,
        )
        assert resp.status_code == 200
    resp = client.post(
        f"/survey/new/{KEY}",
        json={"kind": "alum", "fields": _alum_payload()},
        headers={"x-forwarded-for": "198.51.100.99, 203.0.113.7"},
    )
    assert resp.status_code == 429


# ------------------------------------------------------------- staff: auth ---

_STAFF_ROUTES = [
    ("get", "/survey/new-people/link", {}),
    ("put", "/survey/new-people/link", {"json": {"enabled": True}}),
    ("post", "/survey/new-people/link/reissue", {}),
    ("get", "/survey/new-people/submissions", {}),
    (
        "post",
        "/survey/new-people/submissions/1/approve",
        {"json": {"action": "create", "submitted_at": T0.isoformat()}},
    ),
    ("post", "/survey/new-people/submissions/1/reject", {}),
]


@pytest.mark.parametrize("role", ["view_only", "student"])
@pytest.mark.parametrize(("method", "path", "kwargs"), _STAFF_ROUTES)
def test_staff_routes_forbid_non_managers(client, db, role, method, path, kwargs):
    db.seed_link()
    db.seed_sub()
    _as(role)
    resp = getattr(client, method)(path, **kwargs)
    assert resp.status_code == 403
    assert db.subs[1].status == "pending"
    assert db.link.enabled is True


def test_staff_routes_require_login(client):
    for method, path, kwargs in _STAFF_ROUTES:
        assert getattr(client, method)(path, **kwargs).status_code == 401


# ------------------------------------------------------------- staff: link ---


def test_link_is_created_disabled_on_first_read(client, db):
    _as("super_admin")
    resp = client.get("/survey/new-people/link")
    assert resp.status_code == 200
    body = resp.json()
    assert body["enabled"] is False
    assert len(body["key"]) >= 32
    assert re.fullmatch(r"[A-Za-z0-9_-]+", body["key"])
    (audit,) = db.audits("create_new_people_link")
    assert body["key"] not in (audit.new_value or "")
    # A second read returns the same link, no new audit.
    assert client.get("/survey/new-people/link").json()["key"] == body["key"]
    assert len(db.audits("create_new_people_link")) == 1


def test_link_switch_and_reissue(client, db):
    db.seed_link(enabled=False)
    _as("super_admin")
    resp = client.put("/survey/new-people/link", json={"enabled": True})
    assert resp.status_code == 200
    assert resp.json()["enabled"] is True
    assert resp.json()["key"] == KEY
    assert client.get(f"/survey/new/{KEY}").status_code == 200

    resp = client.post("/survey/new-people/link/reissue")
    assert resp.status_code == 200
    new_key = resp.json()["key"]
    assert new_key != KEY and len(new_key) >= 32
    assert resp.json()["enabled"] is True  # unchanged by a reissue
    assert client.get(f"/survey/new/{KEY}").status_code == 404  # old key dead
    assert client.get(f"/survey/new/{new_key}").status_code == 200

    for audit in db.audits():
        assert KEY not in (audit.new_value or "")
        assert new_key not in (audit.new_value or "")
    assert [a.action_type for a in db.audits()] == [
        "update_new_people_link",
        "reissue_new_people_link",
    ]


# ------------------------------------------------------------ staff: queue ---


def test_queue_lists_pending_oldest_first_with_matches(client, db):
    later = db.seed_sub(submitted_at=T0 + datetime.timedelta(hours=1))
    first = db.seed_sub(kind="friend", payload=_friend_fields(), comments="hello")
    done = db.seed_sub()
    done.status = "rejected"
    db.email_rows = [(77, "Pat", "Quinn", 2009, True, "PAT@example.org ", None)]
    db.name_rows = [
        (77, "Pat", None, "Quinn", 2009, True),  # already an email hit: once only
        (88, "Patricia", "Pat", "Quinn", None, False),
    ]
    _as("super_admin")
    resp = client.get("/survey/new-people/submissions")
    assert resp.status_code == 200
    items = resp.json()
    assert [i["submission_id"] for i in items] == [first.submission_id, later.submission_id]
    item = items[0]
    assert item["kind"] == "friend"
    assert item["name"] == "Pat Quinn"
    assert item["email"] == "pat@example.org"
    assert item["comments"] == "hello"
    assert item["submitted_at"] == T0.isoformat()
    labels = {f["field_key"]: f["label"] for f in item["fields"]}
    assert labels["employment.current_employer"] == "Company"
    assert item["possible_matches"] == [
        {
            "alumni_id": 77,
            "name": "Pat Quinn",
            "graduation_year": 2009,
            "is_friend": False,
            "match_reason": "email",
        },
        {
            "alumni_id": 88,
            "name": "Patricia Quinn",
            "graduation_year": None,
            "is_friend": True,
            "match_reason": "name",
        },
    ]
    alum_item = items[1]
    keys = [f["field_key"] for f in alum_item["fields"]]
    assert keys[-2:] == ["new.graduation_year", "new.byu_degree"]
    (audit,) = db.audits("read_new_people_submissions")
    assert "pat" not in (audit.new_value or "").lower()


def test_possible_matches_are_capped_at_five(client, db):
    db.seed_sub()
    db.name_rows = [(i, "Pat", None, "Quinn", 2000 + i, True) for i in range(1, 9)]
    _as("super_admin")
    (item,) = client.get("/survey/new-people/submissions").json()
    assert len(item["possible_matches"]) == 5


# ---------------------------------------------------------- staff: approve ---


def _approve(client, sub_id=1, **body):
    body.setdefault("submitted_at", T0.isoformat())
    return client.post(f"/survey/new-people/submissions/{sub_id}/approve", json=body)


def test_approve_create_alum(client, db):
    sub = db.seed_sub(
        payload=_alum_payload(**{"program.mentor_willing": "yes"}), comments="Glad to help"
    )
    _as("super_admin")
    resp = _approve(client, action="create")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["created"] is True
    alumni_id = body["alumni_id"]
    (alum,) = db.added_of(Alumni)
    assert alum.alumni_id == alumni_id
    assert alum.is_alumni is True
    assert alum.graduation_year == 2009
    assert alum.first_name == "Pat" and alum.last_name == "Quinn"
    assert alum.linkedin_url == "https://www.linkedin.com/in/patquinn"
    assert alum.profile_updated_by_user_id == 42
    (edu,) = db.added_of(EducationHistory)
    assert (edu.alumni_id, edu.degree, edu.degree_year) == (alumni_id, "BS Finance", 2009)
    (contact,) = db.added_of(AlumniContactInfo)
    assert contact.personal_email == "pat@example.org"
    (job,) = db.added_of(CurrentEmployment)
    assert job.current_employer == "Acme Capital"
    assert job.current_industry == "Investment Banking"
    (eng,) = db.added_of(AlumniProgramEngagement)
    assert eng.mentor_willing is True
    (note,) = db.added_of(Note)
    assert note.alumni_id == alumni_id
    assert note.body == "Survey comment (2026-11-10): Glad to help"
    # The submission is decided, linked, and its PII purged.
    assert sub.status == "approved"
    assert sub.alumni_id == alumni_id
    assert sub.decided_by_user_id == 42
    assert sub.payload is None and sub.comments is None
    # Audited: the create, the apply summary, field rows, and the decision —
    # one change set for the record's rows.
    (create,) = db.audits("create")
    (summary,) = db.audits("apply_new_person_submission")
    (decision,) = db.audits("approve_new_person_submission")
    assert create.change_set_id == summary.change_set_id
    fields = {a.field_name for a in db.audits("update")}
    assert {"graduation_year", "education.degree", "first_name"} <= fields
    assert {"contact.personal_email", "career.current_employer"} <= fields
    assert decision.entity_type == "new_people_submission"
    assert decision.new_value == f"action=create kind=alum alumni_id={alumni_id}"
    assert summary.new_value.startswith(f"new_people_submission={sub.submission_id} ")


def test_approve_create_friend_by_override(client, db):
    sub = db.seed_sub()  # submitted as an alum
    _as("super_admin")
    resp = _approve(client, action="create", kind="friend")
    assert resp.status_code == 200
    (alum,) = db.added_of(Alumni)
    assert alum.is_alumni is False
    assert alum.friend_id == f"FRIEND-{alum.alumni_id:05d}"
    assert alum.graduation_year is None
    assert db.added_of(EducationHistory) == []
    assert sub.status == "approved"
    (decision,) = db.audits("approve_new_person_submission")
    assert "kind=friend" in decision.new_value


def test_approve_create_friend_submission(client, db):
    db.seed_sub(kind="friend", payload=_friend_fields())
    _as("super_admin")
    resp = _approve(client, action="create")
    assert resp.status_code == 200
    (alum,) = db.added_of(Alumni)
    assert alum.is_alumni is False


def test_approve_merge_applies_like_a_survey_and_ignores_new_keys(client, db):
    existing = Alumni(
        alumni_id=500,
        first_name="Patrick",
        last_name="Quinn",
        graduation_year=1998,
        is_alumni=True,
        archived=False,
    )
    db.alumni[500] = existing
    sub = db.seed_sub(comments="Moved to Denver")
    _as("super_admin")
    resp = _approve(client, action="merge", alumni_id=500, overwrite_fields=["profile.first_name"])
    assert resp.status_code == 200, resp.text
    assert resp.json() == {
        "alumni_id": 500,
        "created": False,
        "duplicate_warnings": [],
        "warnings": [],
    }
    assert db.added_of(Alumni) == []
    assert existing.first_name == "Pat"  # listed in overwrite_fields: replaced
    assert existing.linkedin_url == "https://www.linkedin.com/in/patquinn"  # blank: filled
    assert existing.graduation_year == 1998  # new.* ignored on merge
    assert db.added_of(EducationHistory) == []
    (note,) = db.added_of(Note)
    assert note.alumni_id == 500
    assert sub.status == "approved" and sub.alumni_id == 500
    assert sub.payload is None
    assert db.audits("create") == []
    (summary,) = db.audits("apply_new_person_submission")
    assert summary.entity_id == 500


def test_merge_requires_a_live_target(client, db):
    db.seed_sub()
    _as("super_admin")
    assert _approve(client, action="merge").status_code == 422
    assert _approve(client, action="merge", alumni_id=999).status_code == 404
    db.alumni[5] = Alumni(alumni_id=5, archived=True, is_alumni=True)
    assert _approve(client, action="merge", alumni_id=5).status_code == 404
    assert db.subs[1].status == "pending"
    assert db.subs[1].payload is not None


@pytest.mark.parametrize(
    "sent",
    [
        (T0 + datetime.timedelta(seconds=1)).isoformat(),
        "2026-11-10T15:30:00Z",
        "not a date",
    ],
)
def test_stale_submitted_at_is_409(client, db, sent):
    sub = db.seed_sub()
    _as("super_admin")
    resp = _approve(client, action="create", submitted_at=sent)
    assert resp.status_code == 409
    assert resp.json()["error"]["message"] == "This submission changed since you opened it."
    assert sub.status == "pending"
    assert db.added_of(Alumni) == []


def test_submitted_at_with_z_suffix_matches(client, db):
    db.seed_sub()
    _as("super_admin")
    sent = T0.isoformat().replace("+00:00", "Z")
    assert _approve(client, action="create", submitted_at=sent).status_code == 200


def test_approve_locks_the_row(db):
    import asyncio

    seen = []
    real = db.execute

    async def spy(stmt):
        if "new_people_submissions" in _froms(stmt):
            seen.append(stmt._for_update_arg is not None)
        return await real(stmt)

    db.execute = spy
    db.seed_sub()
    body = nps.NewPersonApproveRequest(action="create", submitted_at=T0.isoformat())
    asyncio.run(nps.approve(db, 1, body, 42))
    assert seen and all(seen)


def test_double_approve_is_404_and_creates_one_record(client, db):
    db.seed_sub()
    _as("super_admin")
    assert _approve(client, action="create").status_code == 200
    second = _approve(client, action="create")
    assert second.status_code == 404
    assert len(db.added_of(Alumni)) == 1
    assert len(db.audits("approve_new_person_submission")) == 1


def test_approve_unknown_submission_is_404(client, db):
    _as("super_admin")
    assert _approve(client, sub_id=12345, action="create").status_code == 404


# ----------------------------------------------------------- staff: reject ---


def test_reject_purges_held_answers(client, db):
    sub = db.seed_sub(comments="private words")
    _as("super_admin")
    resp = client.post(f"/survey/new-people/submissions/{sub.submission_id}/reject")
    assert resp.status_code == 204
    assert sub.status == "rejected"
    assert sub.payload is None and sub.comments is None
    assert sub.decided_by_user_id == 42 and sub.decided_at is not None
    assert sub.kind == "alum" and sub.submitted_at == T0
    (audit,) = db.audits("reject_new_person_submission")
    assert audit.entity_id == sub.submission_id
    assert audit.new_value == "kind=alum"
    assert db.added_of(Alumni) == [] and db.added_of(Note) == []
    # Decided: it leaves the queue, and cannot be rejected or approved again.
    assert client.get("/survey/new-people/submissions").json() == []
    again = client.post(f"/survey/new-people/submissions/{sub.submission_id}/reject")
    assert again.status_code == 404
    assert _approve(client, sub_id=sub.submission_id, action="create").status_code == 404


# ------------------------------------------------ held rows are not alumni ---


def test_held_submissions_do_not_create_alumni_or_survey_rows(client, db):
    db.seed_link()
    for i in range(3):
        fields = _alum_payload(**{"contact.personal_email": f"p{i}@example.org"})
        assert _post(client, fields=fields).status_code == 200
    assert len(db.added_of(NewPersonSubmission)) == 3
    assert db.alumni == {}
    assert db.added_of(Alumni) == [] and db.added_of(SurveyResponse) == []


def test_nothing_outside_the_feature_reads_the_holding_table():
    """Counts, KPIs, campaign tallies and the normal review queue cannot include
    held people if nothing but the new-people service ever reads their table."""
    allowed = {
        REPO / "app" / "models" / "new_people.py",
        REPO / "app" / "models" / "__init__.py",
        REPO / "app" / "services" / "new_people_survey.py",
        REPO / "app" / "schemas" / "new_people.py",
        REPO / "app" / "api" / "routes" / "survey_new_people.py",
    }
    offenders = []
    for path in (REPO / "app").rglob("*.py"):
        if path in allowed:
            continue
        text = path.read_text(encoding="utf-8")
        if "NewPersonSubmission" in text or "new_people_submissions" in text:
            offenders.append(str(path.relative_to(REPO)))
    assert offenders == []


# ------------------------------------------------------------- migration -----


def test_migration_is_additive_and_locked_down():
    sql = (REPO / "database" / "migrations" / "2026-11-10_new_people_survey.sql").read_text(
        encoding="utf-8"
    )
    body = "\n".join(line for line in sql.splitlines() if not line.lstrip().startswith("--"))
    body = body.replace("ON DELETE SET NULL", "")
    assert not re.search(r"\b(DROP|TRUNCATE|DELETE|UPDATE)\b", body, re.IGNORECASE)
    assert re.findall(r"ALTER TABLE (\w+)", body) == [
        "new_people_survey_link",
        "new_people_submissions",
    ]
    rls = (REPO / "database" / "rls_lockdown.sql").read_text(encoding="utf-8")
    schema = (REPO / "database" / "schema.sql").read_text(encoding="utf-8")
    for table in ("new_people_survey_link", "new_people_submissions"):
        assert f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY;" in body
        assert f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY;" in rls
        assert f"CREATE TABLE {table} (" in schema


# ================================================== round 2 (security fixes) ==


def _existing(db, alumni_id=500, *, deceased=False, is_alumni=True, **cols):
    alum = Alumni(
        alumni_id=alumni_id,
        first_name="Patrick",
        last_name="Quinn",
        graduation_year=1998,
        is_alumni=is_alumni,
        archived=False,
        deceased=deceased,
        **cols,
    )
    db.alumni[alumni_id] = alum
    return alum


def _seed_contact(db, alumni_id=500, **cols):
    contact = AlumniContactInfo(alumni_id=alumni_id, **cols)
    db.side["alumni_contact_info"] = [contact]
    return contact


# ------------------------------------------------------- merge: fill blanks --


def test_merge_never_changes_a_populated_email_by_default(client, db):
    _existing(db)
    contact = _seed_contact(db, personal_email="old@family.example", work_email=None)
    db.seed_sub(payload=_alum_payload(**{"contact.work_email": "pat@acme.example"}))
    _as("super_admin")
    resp = _approve(client, action="merge", alumni_id=500)
    assert resp.status_code == 200, resp.text
    assert contact.personal_email == "old@family.example"  # populated: kept
    assert contact.work_email == "pat@acme.example"  # blank: filled
    assert db.alumni[500].first_name == "Patrick"  # populated name kept too
    (summary,) = db.audits("apply_new_person_submission")
    assert "kept=" in summary.new_value
    changed = {a.field_name for a in db.audits("update")}
    assert "contact.personal_email" not in changed
    assert "contact.work_email" in changed


def test_merge_replaces_a_populated_email_only_when_listed(client, db):
    _existing(db)
    contact = _seed_contact(db, personal_email="old@family.example")
    db.seed_sub()
    _as("super_admin")
    resp = _approve(
        client, action="merge", alumni_id=500, overwrite_fields=["contact.personal_email"]
    )
    assert resp.status_code == 200
    assert contact.personal_email == "pat@example.org"
    assert db.alumni[500].first_name == "Patrick"  # not listed: kept


def test_merge_turns_on_an_unset_flag_without_overwrite(client, db):
    _existing(db)
    db.seed_sub(payload=_alum_payload(**{"program.mentor_willing": "yes"}))
    _as("super_admin")
    assert _approve(client, action="merge", alumni_id=500).status_code == 200
    (eng,) = db.added_of(AlumniProgramEngagement)
    assert eng.mentor_willing is True


def test_merge_into_deceased_is_409(client, db):
    _existing(db, deceased=True)
    sub = db.seed_sub()
    _as("super_admin")
    resp = _approve(client, action="merge", alumni_id=500)
    assert resp.status_code == 409
    assert resp.json()["error"]["message"] == "This person is marked deceased."
    assert sub.status == "pending" and sub.payload is not None
    assert db.alumni[500].first_name == "Patrick"


@pytest.mark.parametrize(
    ("sub_kind", "target_is_alum", "expect"),
    [
        ("friend", True, ["kind_mismatch"]),
        ("alum", False, ["kind_mismatch"]),
        ("alum", True, []),
        ("friend", False, []),
    ],
)
def test_merge_kind_mismatch_is_a_warning_not_a_block(client, db, sub_kind, target_is_alum, expect):
    _existing(db, is_alumni=target_is_alum)
    payload = _alum_payload() if sub_kind == "alum" else _friend_fields()
    db.seed_sub(kind=sub_kind, payload=payload)
    _as("super_admin")
    resp = _approve(client, action="merge", alumni_id=500)
    assert resp.status_code == 200
    assert resp.json()["warnings"] == expect


def test_create_has_no_warnings(client, db):
    db.seed_sub()
    _as("super_admin")
    assert _approve(client, action="create").json()["warnings"] == []


# ---------------------------------------------------------------- preview ---


def test_preview_shows_changes_and_overwrites(client, db):
    _existing(db, linkedin_url=None)
    _seed_contact(db, personal_email="old@family.example")
    sub = db.seed_sub()
    _as("super_admin")
    resp = client.get(f"/survey/new-people/submissions/{sub.submission_id}/preview?alumni_id=500")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["alumni_id"] == 500
    assert body["name"] == "Patrick Quinn"
    assert body["deceased"] is False
    changes = {c["field_key"]: c for c in body["changes"]}
    assert changes["contact.personal_email"] == {
        "field_key": "contact.personal_email",
        "label": "Personal email",
        "before": "old@family.example",
        "after": "pat@example.org",
    }
    assert "profile.last_name" not in changes  # unchanged: not listed
    assert not any(k.startswith("new.") for k in changes)
    assert set(body["overwrites"]) == {"contact.personal_email", "profile.first_name"}
    assert changes["profile.linkedin_url"]["before"] == ""
    # Read-only: nothing written but the disclosure audit.
    assert sub.status == "pending"
    assert [a.action_type for a in db.audits()] == ["preview_new_person_merge"]


def test_preview_reports_deceased(client, db):
    _existing(db, deceased=True)
    db.seed_sub()
    _as("super_admin")
    resp = client.get("/survey/new-people/submissions/1/preview?alumni_id=500")
    assert resp.status_code == 200
    assert resp.json()["deceased"] is True


def test_preview_404s(client, db):
    _existing(db)
    _existing(db, alumni_id=501).archived = True
    done = db.seed_sub()
    pending = db.seed_sub()
    done.status = "rejected"
    _as("super_admin")
    base = "/survey/new-people/submissions"
    assert client.get(f"{base}/{done.submission_id}/preview?alumni_id=500").status_code == 404
    assert client.get(f"{base}/999/preview?alumni_id=500").status_code == 404
    assert client.get(f"{base}/{pending.submission_id}/preview?alumni_id=999").status_code == 404
    assert client.get(f"{base}/{pending.submission_id}/preview?alumni_id=501").status_code == 404
    assert client.get(f"{base}/{pending.submission_id}/preview").status_code == 422


# ------------------------------------------------------------- bulk reject --


def test_bulk_reject_purges_pending_and_skips_the_rest(client, db):
    a = db.seed_sub(comments="a")
    b = db.seed_sub(comments="b")
    done = db.seed_sub()
    done.status = "approved"
    done.payload = None
    keep = db.seed_sub()
    _as("super_admin")
    resp = client.post(
        "/survey/new-people/submissions/reject",
        json={
            "submission_ids": [
                b.submission_id,
                a.submission_id,
                done.submission_id,
                999,
                a.submission_id,
            ]
        },
    )
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"rejected": 2}
    for sub in (a, b):
        assert sub.status == "rejected" and sub.payload is None and sub.comments is None
        assert sub.decided_by_user_id == 42
    assert done.status == "approved"
    assert keep.status == "pending" and keep.payload is not None
    (audit,) = db.audits()
    assert audit.action_type == "bulk_reject_new_person_submissions"
    assert audit.new_value == "rejected=2 requested=4"


@pytest.mark.parametrize("ids", [[], list(range(1, 502))])
def test_bulk_reject_bounds(client, db, ids):
    _as("super_admin")
    resp = client.post("/survey/new-people/submissions/reject", json={"submission_ids": ids})
    assert resp.status_code == 422


@pytest.mark.parametrize("role", ["view_only", "student"])
def test_round_two_staff_routes_forbid_non_managers(client, db, role):
    _existing(db)
    sub = db.seed_sub()
    _as(role)
    assert (
        client.post(
            "/survey/new-people/submissions/reject", json={"submission_ids": [1]}
        ).status_code
        == 403
    )
    assert client.get("/survey/new-people/submissions/1/preview?alumni_id=500").status_code == 403
    assert sub.status == "pending"


def test_single_reject_route_still_takes_an_id(client, db):
    sub = db.seed_sub()
    _as("super_admin")
    assert (
        client.post(f"/survey/new-people/submissions/{sub.submission_id}/reject").status_code == 204
    )
    assert db.audits()[0].action_type == "reject_new_person_submission"


# ----------------------------------------------------- submit: dedupe + cap --


def test_same_email_replaces_the_pending_row(client, db):
    db.seed_link()
    assert _post(client, comments="first").status_code == 200
    (first,) = db.added_of(NewPersonSubmission)
    newer = _friend_fields(
        **{
            "contact.personal_email": "  PAT@Example.org ",
            "employment.current_employer": "New Co",
        }
    )
    assert _post(client, kind="friend", fields=newer, fill_seconds=90).status_code == 200
    assert len(db.subs) == 1  # no second row
    assert db.added_of(NewPersonSubmission) == [first]
    assert first.kind == "friend"
    assert first.payload["employment.current_employer"] == "New Co"
    assert "new.graduation_year" not in first.payload
    assert first.comments is None  # replaced whole, newest wins
    assert first.fill_seconds == 90
    assert first.submitted_at != T0  # bumped: a stale reviewer gets the 409


def test_matching_on_the_work_email_also_dedupes(client, db):
    db.seed_link()
    fields = _friend_fields(**{"contact.work_email": "pat@acme.example"})
    assert _post(client, kind="friend", fields=fields).status_code == 200
    other = _friend_fields(**{"contact.personal_email": "pat@acme.example"})
    assert _post(client, kind="friend", fields=other).status_code == 200
    assert len(db.subs) == 1


def test_different_emails_are_different_rows(client, db):
    db.seed_link()
    assert _post(client).status_code == 200
    other = _alum_payload(**{"contact.personal_email": "someone.else@example.org"})
    assert _post(client, fields=other).status_code == 200
    assert len(db.subs) == 2


def test_decided_rows_are_not_replaced(client, db):
    db.seed_link()
    old = db.seed_sub()
    old.status = "rejected"
    old.payload = None
    assert _post(client).status_code == 200
    assert len(db.subs) == 2


def test_replacement_is_allowed_at_the_cap(client, db):
    db.seed_link()
    db.seed_sub()
    db.pending_override = nps.PENDING_CAP
    assert _post(client).status_code == 200
    assert len(db.subs) == 1


def test_cap_count_runs_under_the_advisory_lock(client, db):
    db.seed_link()
    assert _post(client).status_code == 200
    assert db.locks == 1
    assert db.locks_at_count == [1]  # the lock was taken before the count


# ------------------------------------------------------------ queue alert ---


@pytest.fixture
def alerts(monkeypatch):
    sent = []

    async def fake_deliver(subject, intro, rows, *, purpose, slack_summary=None):
        sent.append(
            {
                "subject": subject,
                "intro": intro,
                "rows": rows,
                "purpose": purpose,
                "summary": slack_summary,
            }
        )
        return True

    monkeypatch.setattr(nps.failure_alert, "alerting_enabled", lambda: True)
    monkeypatch.setattr(nps.failure_alert, "deliver_alert", fake_deliver)
    return sent


def test_one_alert_when_the_queue_reaches_eighty_percent(client, db, alerts):
    assert nps.PENDING_ALERT_AT == nps.PENDING_CAP * 8 // 10
    db.seed_link()
    db.pending_override = nps.PENDING_ALERT_AT - 2
    assert (
        _post(client, fields=_alum_payload(**{"contact.personal_email": "a@x.org"})).status_code
        == 200
    )
    assert alerts == []
    db.pending_override = nps.PENDING_ALERT_AT - 1  # this one crosses the line
    assert (
        _post(client, fields=_alum_payload(**{"contact.personal_email": "b@x.org"})).status_code
        == 200
    )
    db.pending_override = nps.PENDING_ALERT_AT  # past it: no repeat
    assert (
        _post(client, fields=_alum_payload(**{"contact.personal_email": "c@x.org"})).status_code
        == 200
    )
    assert len(alerts) == 1
    (alert,) = alerts
    assert alert["purpose"] == nps.failure_alert.SECURITY
    text = repr(alert)
    assert str(nps.PENDING_ALERT_AT) in text
    for pii in ("b@x.org", "Pat", "Quinn", "Acme"):
        assert pii not in text


def test_a_replacement_never_alerts(client, db, alerts):
    db.seed_link()
    db.seed_sub()
    db.pending_override = nps.PENDING_ALERT_AT - 1
    assert _post(client).status_code == 200
    assert alerts == []


def test_a_failing_alerter_does_not_fail_the_submit(client, db, monkeypatch):
    async def boom(*_a, **_k):
        raise RuntimeError("slack down")

    monkeypatch.setattr(nps.failure_alert, "alerting_enabled", lambda: True)
    monkeypatch.setattr(nps.failure_alert, "deliver_alert", boom)
    db.seed_link()
    db.pending_override = nps.PENDING_ALERT_AT - 1
    assert _post(client).status_code == 200
    assert len(db.subs) == 1


def test_new_audit_actions_are_classified_for_record_history():
    from app.services import alumni_history

    assert "apply_new_person_submission" in alumni_history.HISTORY_ACTIONS
    assert "apply_new_person_submission" in alumni_history._SUMMARY_ACTIONS
    assert "preview_new_person_merge" in alumni_history.NON_HISTORY_ACTIONS


def test_payload_column_purges_to_sql_null_not_json_null():
    """Regression (found on dev 2026-11-10): without none_as_null, a purged
    payload is written as JSON `null`, which violates
    ck_new_people_submissions_payload_state on real Postgres. The fake session
    in this file can't see that, so pin the column setting directly."""
    from app.models.new_people import NewPersonSubmission

    assert NewPersonSubmission.__table__.c.payload.type.none_as_null is True
