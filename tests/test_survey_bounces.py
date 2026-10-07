"""Survey email bounces (fa-web-app #858).

Staff want to see whose survey email bounced so they can fix the address. Four
pieces, each pinned here:

* the SEND path records Resend's message id + the address on each send-log row,
  and tags every email -- best effort, so a bookkeeping failure never fails a
  send;
* ``POST /webhooks/resend`` accepts ONLY Svix-signed, fresh deliveries, fails
  CLOSED (503) without a secret, caps the body, and is idempotent on svix-id;
* a bounce is matched to the alum by message id, falling back to the tags;
* ``GET /survey/campaigns/{year}/bounced`` lists PERMANENT bounces only, is gated
  like ``/unreachable`` and leaves an audit row.
"""

import asyncio
import base64
import datetime
import hashlib
import hmac
import json
import time
import uuid

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool
from sqlalchemy.sql.dml import Insert, Update
from sqlalchemy.sql.selectable import Select

from app.api.dependencies import auth as auth_deps
from app.api.routes import webhooks as webhook_routes
from app.core import webhooks
from app.core.capabilities import DEFAULT_GRANTS
from app.core.database import Base, get_session
from app.core.roles import RoleName
from app.core.security import AuthorizationError
from app.main import app
from app.models.alumni import Alumni
from app.models.audit import AuditLog
from app.models.contact import AlumniContactInfo
from app.models.survey_email_event import SurveyEmailEvent
from app.models.survey_schedule import SurveySendLog
from app.models.tags import AlumniStatusLabel, StatusLabel
from app.schemas.auth import UserContext
from app.services import survey_bounces, survey_email
from app.services.survey_message import SurveyMessage
from tests.survey_fakes import SendLogSession

_KEY = b"0123456789abcdef0123456789abcdef"
_SECRET = "whsec_" + base64.b64encode(_KEY).decode()
_YEAR = 2020


def _sign(body: bytes, *, svix_id="msg_1", ts=None, key=_KEY) -> dict[str, str]:
    ts = str(int(time.time()) if ts is None else ts)
    sig = base64.b64encode(
        hmac.new(key, f"{svix_id}.{ts}.".encode() + body, hashlib.sha256).digest()
    ).decode()
    return {"svix-id": svix_id, "svix-timestamp": ts, "svix-signature": f"v1,{sig}"}


def _verify(headers, body, **kw):
    return webhooks.verify_svix_signature(
        secret=kw.pop("secret", _SECRET),
        svix_id=headers.get("svix-id"),
        svix_timestamp=headers.get("svix-timestamp"),
        svix_signature=headers.get("svix-signature"),
        body=body,
        **kw,
    )


def _all_routes(router):
    """Every real route, flattened -- `include_router` nests them."""
    for route in getattr(router, "routes", []):
        inner = getattr(route, "original_router", None)
        if inner is not None:
            yield from _all_routes(inner)
        elif hasattr(route, "routes"):
            yield from _all_routes(route)
        else:
            yield route


def _route(path, method):
    return next(
        r
        for r in _all_routes(app)
        if getattr(r, "path", None) == path and method in getattr(r, "methods", set())
    )


# ============================================================ signature =====


def test_a_correctly_signed_fresh_delivery_verifies():
    body = b'{"type":"email.bounced"}'
    assert _verify(_sign(body), body) is True


def test_the_secret_works_without_its_whsec_prefix_too():
    body = b"{}"
    assert _verify(_sign(body), body, secret=base64.b64encode(_KEY).decode())


def test_a_tampered_body_fails():
    body = b'{"type":"email.bounced"}'
    assert _verify(_sign(body), body + b" ") is False


def test_a_signature_made_with_another_key_fails():
    body = b"{}"
    assert _verify(_sign(body, key=b"x" * 32), body) is False


def test_a_changed_svix_id_fails():
    body = b"{}"
    headers = _sign(body)
    headers["svix-id"] = "msg_other"
    assert _verify(headers, body) is False


@pytest.mark.parametrize("skew", [-301, 301, -10_000, 10_000])
def test_a_stale_or_future_timestamp_fails_even_when_signed(skew):
    body = b"{}"
    now = time.time()
    headers = _sign(body, ts=int(now) + skew)
    assert _verify(headers, body, now=now) is False


def test_a_timestamp_inside_the_window_passes():
    body = b"{}"
    now = time.time()
    assert _verify(_sign(body, ts=int(now) - 240), body, now=now) is True


def test_any_matching_entry_in_a_multi_signature_header_is_accepted():
    body = b"{}"
    headers = _sign(body)
    good = headers["svix-signature"]
    headers["svix-signature"] = f"v1,AAAAbad= v2,whatever {good} v1,ZZZZ"
    assert _verify(headers, body) is True


def test_a_header_with_only_wrong_or_unversioned_entries_fails():
    body = b"{}"
    real = _sign(body)["svix-signature"].split(",", 1)[1]
    headers = _sign(body)
    headers["svix-signature"] = f"v1,AAAA {real} v0,{real}"  # bare / wrong version
    assert _verify(headers, body) is False


@pytest.mark.parametrize("missing", ["svix-id", "svix-timestamp", "svix-signature"])
def test_a_missing_header_fails(missing):
    body = b"{}"
    headers = _sign(body)
    headers.pop(missing)
    assert _verify(headers, body) is False


def test_garbage_headers_are_a_clean_false_not_an_exception():
    body = b"{}"
    headers = {
        "svix-id": "msg_\xe9",
        "svix-timestamp": "not-a-number",
        "svix-signature": "v1,\xff\xfe",
    }
    assert _verify(headers, body) is False
    headers["svix-timestamp"] = str(int(time.time()))
    assert _verify(headers, body) is False


def test_an_unusable_secret_is_a_configuration_error():
    with pytest.raises(webhooks.WebhookNotConfigured):
        _verify(_sign(b"{}"), b"{}", secret="whsec_!!!not base64!!!")


def test_the_verifier_compares_bytes_in_constant_time():
    src = webhooks.__file__
    text = open(src, encoding="utf-8").read()
    assert "hmac.compare_digest(" in text
    assert ".encode(" in text  # bytes, never str (see app/core/cron.py)


# =============================================================== route ======


class _Settings:
    def __init__(self, secret):
        self.resend_webhook_secret = secret


@pytest.fixture
def webhook_client(monkeypatch):
    calls: list[dict] = []

    async def fake_record(session, *, svix_id, payload):
        calls.append({"svix_id": svix_id, "payload": payload})
        return "stored"

    monkeypatch.setattr(survey_bounces, "record_webhook_event", fake_record)

    async def _override():
        yield object()

    app.dependency_overrides[get_session] = _override
    client = TestClient(app, raise_server_exceptions=False)
    client.calls = calls

    def set_secret(secret):
        monkeypatch.setattr(webhooks, "get_settings", lambda: _Settings(secret))

    client.set_secret = set_secret
    set_secret(_SECRET)
    yield client
    app.dependency_overrides.clear()


_BOUNCE = {
    "type": "email.bounced",
    "created_at": "2026-10-07T18:00:00.000Z",
    "data": {
        "email_id": "re_123",
        "to": ["someone@example.org"],
        "bounce": {"type": "Permanent", "subType": "General", "message": "x"},
        "tags": {"alumni_id": "5", "graduation_year": "2020", "stage": "0"},
    },
}


def test_without_a_secret_the_webhook_fails_closed_and_processes_nothing(
    webhook_client,
):
    webhook_client.set_secret(None)
    body = json.dumps(_BOUNCE).encode()
    r = webhook_client.post("/webhooks/resend", content=body, headers=_sign(body))
    assert r.status_code == 503
    assert webhook_client.calls == []


def test_a_blank_secret_is_the_same_as_none(webhook_client):
    webhook_client.set_secret("   ")
    body = b"{}"
    r = webhook_client.post("/webhooks/resend", content=body, headers=_sign(body))
    assert r.status_code == 503
    assert webhook_client.calls == []


def test_an_unsigned_delivery_is_refused(webhook_client):
    body = json.dumps(_BOUNCE).encode()
    r = webhook_client.post("/webhooks/resend", content=body)
    assert r.status_code == 401
    assert webhook_client.calls == []


def test_a_badly_signed_delivery_is_refused(webhook_client):
    body = json.dumps(_BOUNCE).encode()
    r = webhook_client.post(
        "/webhooks/resend", content=body, headers=_sign(body, key=b"y" * 32)
    )
    assert r.status_code == 401
    assert webhook_client.calls == []


def test_a_replayed_old_delivery_is_refused(webhook_client):
    body = json.dumps(_BOUNCE).encode()
    r = webhook_client.post(
        "/webhooks/resend",
        content=body,
        headers=_sign(body, ts=int(time.time()) - 3600),
    )
    assert r.status_code == 401
    assert webhook_client.calls == []


def test_an_oversized_body_is_refused_before_it_is_verified(webhook_client):
    body = b'{"pad":"' + b"a" * (webhook_routes.MAX_BODY_BYTES + 10) + b'"}'
    r = webhook_client.post("/webhooks/resend", content=body, headers=_sign(body))
    assert r.status_code == 413
    assert webhook_client.calls == []


def test_a_signed_non_json_body_is_a_400(webhook_client):
    body = b"not json"
    r = webhook_client.post("/webhooks/resend", content=body, headers=_sign(body))
    assert r.status_code == 400
    assert webhook_client.calls == []


def test_a_deeply_nested_signed_body_is_a_clean_400(webhook_client):
    half = webhook_routes.MAX_BODY_BYTES // 2
    body = b"[" * half + b"]" * half
    r = webhook_client.post("/webhooks/resend", content=body, headers=_sign(body))
    assert r.status_code == 400
    assert webhook_client.calls == []


def test_an_over_long_svix_id_is_refused_not_truncated(webhook_client):
    body = json.dumps(_BOUNCE).encode()
    r = webhook_client.post(
        "/webhooks/resend",
        content=body,
        headers=_sign(body, svix_id="m" * (webhook_routes.MAX_SVIX_ID_LEN + 1)),
    )
    assert r.status_code == 400
    assert webhook_client.calls == []


def test_a_valid_delivery_is_handed_on_with_its_svix_id(webhook_client):
    body = json.dumps(_BOUNCE).encode()
    r = webhook_client.post(
        "/webhooks/resend", content=body, headers=_sign(body, svix_id="msg_abc")
    )
    assert r.status_code == 200
    assert webhook_client.calls == [{"svix_id": "msg_abc", "payload": _BOUNCE}]


def test_the_webhook_is_rate_limited_and_kept_out_of_the_schema():
    route = _route("/webhooks/resend", "POST")
    assert route.include_in_schema is False
    from app.core.rate_limit import RESEND_WEBHOOK_LIMITER

    assert RESEND_WEBHOOK_LIMITER in {d.call for d in route.dependant.dependencies}


# ====================================================== record / match ======


class _Result:
    def __init__(self, row=None):
        self._row = row

    def first(self):
        return self._row


class _EventSession:
    """Enough of a session for ``record_webhook_event``: a send log keyed by
    message id, a set of existing alumni, and a real svix_id unique store."""

    def __init__(self, *, send_log=None, alumni=(), deleted_before_insert=()):
        self.send_log = dict(send_log or {})  # email_id -> (alumni_id, year)
        self.alumni = set(alumni)
        # Alumni that pass the existence check but are gone by the INSERT.
        self.deleted_before_insert = set(deleted_before_insert)
        self.events: dict[str, dict] = {}
        self.commits = 0
        self.rollbacks = 0

    async def execute(self, stmt):
        params = dict(stmt.compile().params)
        table = getattr(getattr(stmt, "table", None), "name", None)
        if isinstance(stmt, Insert) and table == "survey_email_events":
            sid = params["svix_id"]
            if params.get("alumni_id") in self.deleted_before_insert:
                raise IntegrityError("INSERT", {}, Exception("fk violation"))
            if sid in self.events:
                return _Result(None)
            self.events[sid] = params
            return _Result((len(self.events),))
        if isinstance(stmt, Select):
            froms = {getattr(f, "name", None) for f in stmt.get_final_froms()}
            if "survey_send_log" in froms:
                return _Result(self.send_log.get(params.get("resend_email_id_1")))
            if "alumni" in froms:
                aid = params.get("alumni_id_1")
                return _Result((aid,) if aid in self.alumni else None)
        raise AssertionError(f"unexpected statement: {stmt}")

    async def commit(self):
        self.commits += 1

    async def rollback(self):
        self.rollbacks += 1


def _record(session, payload, svix_id="msg_1"):
    return asyncio.run(
        survey_bounces.record_webhook_event(session, svix_id=svix_id, payload=payload)
    )


def test_a_bounce_is_matched_through_the_send_log_message_id():
    payload = json.loads(json.dumps(_BOUNCE))
    del payload["data"]["tags"]  # matched by message id alone
    session = _EventSession(send_log={"re_123": (42, 2019)}, alumni={5})
    assert _record(session, payload) == "stored"
    row = session.events["msg_1"]
    # The year comes from the send log too, not from anywhere else.
    assert (row["alumni_id"], row["graduation_year"]) == (42, 2019)
    assert row["event_type"] == "email.bounced"
    assert row["bounce_type"] == "permanent"
    assert row["bounce_subtype"] == "General"
    assert row["resend_email_id"] == "re_123"
    assert row["occurred_at"] == datetime.datetime(
        2026, 10, 7, 18, 0, tzinfo=datetime.UTC
    )


def test_without_a_send_log_match_the_tags_are_the_fallback():
    session = _EventSession(alumni={5})
    assert _record(session, _BOUNCE) == "stored"
    row = session.events["msg_1"]
    assert (row["alumni_id"], row["graduation_year"]) == (5, 2020)


def test_list_shaped_tags_are_understood_too():
    payload = json.loads(json.dumps(_BOUNCE))
    payload["data"]["tags"] = [
        {"name": "alumni_id", "value": "5"},
        {"name": "graduation_year", "value": "2020"},
    ]
    session = _EventSession(alumni={5})
    _record(session, payload)
    assert session.events["msg_1"]["alumni_id"] == 5


def test_a_tag_naming_a_deleted_alum_is_stored_unmatched_not_failed():
    session = _EventSession(alumni=set())
    assert _record(session, _BOUNCE) == "stored"
    assert session.events["msg_1"]["alumni_id"] is None


def test_a_send_log_match_contradicted_by_the_tag_is_left_unattributed():
    # The id was paired with alum 42 by position; the signed tag says alum 5.
    session = _EventSession(send_log={"re_123": (42, 2019)}, alumni={5, 42})
    assert _record(session, _BOUNCE) == "stored"
    row = session.events["msg_1"]
    assert row["alumni_id"] is None and row["graduation_year"] is None


def test_a_send_log_match_with_an_agreeing_tag_is_kept():
    payload = json.loads(json.dumps(_BOUNCE))
    payload["data"]["tags"]["alumni_id"] = "42"
    session = _EventSession(send_log={"re_123": (42, 2019)})
    _record(session, payload)
    assert session.events["msg_1"]["alumni_id"] == 42


def test_an_alum_deleted_between_check_and_insert_is_stored_unattributed():
    session = _EventSession(alumni={5}, deleted_before_insert={5})
    assert _record(session, _BOUNCE, "msg_race") == "stored"
    assert session.rollbacks == 1
    row = session.events["msg_race"]
    assert row["alumni_id"] is None
    # Idempotency survives the retry path.
    assert _record(session, _BOUNCE, "msg_race") == "duplicate"
    assert len(session.events) == 1


def test_an_over_long_svix_id_is_never_truncated_into_storage():
    session = _EventSession(alumni={5})
    assert _record(session, _BOUNCE, "m" * 101) == "ignored"
    assert session.events == {}


def test_a_redelivery_with_the_same_svix_id_is_a_no_op():
    session = _EventSession(alumni={5})
    assert _record(session, _BOUNCE, "msg_dup") == "stored"
    assert _record(session, _BOUNCE, "msg_dup") == "duplicate"
    assert len(session.events) == 1


def test_a_temporary_bounce_is_stored_with_its_type():
    payload = json.loads(json.dumps(_BOUNCE))
    payload["data"]["bounce"] = {"type": "Transient", "subType": "MailboxFull"}
    session = _EventSession(alumni={5})
    _record(session, payload)
    assert session.events["msg_1"]["bounce_type"] == "transient"


def test_a_complaint_is_stored_without_bounce_fields():
    payload = {"type": "email.complained", "data": {"email_id": "re_9"}}
    session = _EventSession(send_log={"re_9": (7, 2020)})
    assert _record(session, payload) == "stored"
    row = session.events["msg_1"]
    assert row["event_type"] == "email.complained"
    assert row["bounce_type"] is None


@pytest.mark.parametrize(
    "payload",
    [
        {"type": "email.delivered", "data": {"email_id": "re_1"}},
        {"type": "email.opened"},
        {"data": {}},
        ["not", "a", "dict"],
        "string",
    ],
)
def test_other_events_are_acknowledged_and_dropped(payload):
    session = _EventSession()
    assert _record(session, payload) == "ignored"
    assert session.events == {}


def test_no_address_is_written_to_the_event_row():
    session = _EventSession(alumni={5})
    _record(session, _BOUNCE)
    assert "someone@example.org" not in json.dumps(
        session.events["msg_1"], default=str
    )
    assert not hasattr(SurveyEmailEvent, "to") and not hasattr(
        SurveyEmailEvent, "email"
    )


# ============================================================ the list ======


@pytest.fixture
def db():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    with engine.connect() as conn:
        Base.metadata.create_all(
            conn,
            tables=[
                Alumni.__table__,
                AlumniContactInfo.__table__,
                SurveySendLog.__table__,
                SurveyEmailEvent.__table__,
                AlumniStatusLabel.__table__,
                StatusLabel.__table__,
            ],
        )
        conn.commit()
        yield conn
    engine.dispose()


class _AsyncWrap:
    def __init__(self, conn):
        self._s = Session(bind=conn)

    async def execute(self, stmt):
        return self._s.execute(stmt)


_seq = iter(range(1, 10_000))


def _alum(conn, alumni_id, first, *, personal=None, work=None, archived=False,
         deceased=False, is_alumni=True, label=None):
    conn.execute(
        Alumni.__table__.insert().values(
            alumni_id=alumni_id,
            first_name=first,
            last_name="Test",
            graduation_year=_YEAR,
            is_alumni=is_alumni,
            archived=archived,
            deceased=deceased,
        )
    )
    if label:
        conn.execute(
            StatusLabel.__table__.insert().values(
                status_label_id=alumni_id, status_label_name=label
            )
        )
        conn.execute(
            AlumniStatusLabel.__table__.insert().values(
                alumni_status_label_id=alumni_id,
                alumni_id=alumni_id,
                status_label_id=alumni_id,
            )
        )
    conn.execute(
        AlumniContactInfo.__table__.insert().values(
            contact_info_id=alumni_id,
            alumni_id=alumni_id,
            personal_email=personal,
            work_email=work,
        )
    )


def _sent(conn, alumni_id, email_id, sent_to, year=_YEAR, stage=0):
    conn.execute(
        SurveySendLog.__table__.insert().values(
            survey_send_log_id=next(_seq),
            graduation_year=year,
            alumni_id=alumni_id,
            stage=stage,
            cycle_seq=1,
            reset_seq=0,
            resend_email_id=email_id,
            sent_to=sent_to,
        )
    )


def _event(conn, alumni_id, email_id, *, bounce_type="permanent", when=None,
           event_type="email.bounced", year=_YEAR, subtype="General"):
    conn.execute(
        SurveyEmailEvent.__table__.insert().values(
            survey_email_event_id=next(_seq),
            resend_email_id=email_id,
            alumni_id=alumni_id,
            graduation_year=year,
            event_type=event_type,
            bounce_type=bounce_type,
            bounce_subtype=subtype,
            occurred_at=when or datetime.datetime(2026, 10, 1, tzinfo=datetime.UTC),
            svix_id=f"msg_{next(_seq)}",
        )
    )


def _page(conn, year=_YEAR, **kw):
    conn.commit()
    return asyncio.run(survey_bounces.list_bounced(_AsyncWrap(conn), year, **kw))


def _list(conn, year=_YEAR):
    return _page(conn, year).items


def test_only_permanent_bounces_are_listed(db):
    _alum(db, 1, "Hard", personal="hard@x.org")
    _alum(db, 2, "Soft", personal="soft@x.org")
    _alum(db, 3, "Unknown", personal="u@x.org")
    _sent(db, 1, "re_1", "hard@x.org")
    _sent(db, 2, "re_2", "soft@x.org")
    _sent(db, 3, "re_3", "u@x.org")
    _event(db, 1, "re_1", bounce_type="permanent")
    _event(db, 2, "re_2", bounce_type="transient")
    _event(db, 3, "re_3", bounce_type="undetermined")
    items = _list(db)
    assert [i.alumni_id for i in items] == [1]
    assert items[0].bounced_address == "hard@x.org"
    assert items[0].bounce_subtype == "General"
    assert items[0].name == "Hard Test"


def test_the_list_uses_the_same_population_as_unreachable(db):
    """Archived, non-alumni (friends), deceased and Do Not Contact people are
    never put on a worklist to chase -- same rule as `/unreachable`."""
    _alum(db, 1, "Live", personal="l@x.org")
    _alum(db, 2, "Archived", personal="a@x.org", archived=True)
    _alum(db, 3, "Friend", personal="f@x.org", is_alumni=False)
    _alum(db, 4, "Deceased", personal="d@x.org", deceased=True)
    _alum(db, 5, "Dnc", personal="n@x.org", label="Do Not Contact")
    for i in range(1, 6):
        _event(db, i, f"re_{i}")
    assert [i.alumni_id for i in _list(db)] == [1]


def test_complaints_other_years_and_unmatched_events_are_not_listed(db):
    _alum(db, 1, "A", personal="a@x.org")
    _alum(db, 2, "B", personal="b@x.org")
    _event(db, 1, "re_c", event_type="email.complained", bounce_type=None)
    _event(db, 2, "re_y", year=_YEAR + 1)
    _event(db, None, "re_n")
    assert _list(db) == []


def test_one_row_per_alum_with_their_latest_bounce(db):
    _alum(db, 1, "A", personal="new@x.org")
    _sent(db, 1, "re_old", "old@x.org")
    _sent(db, 1, "re_new", "new@x.org", stage=1)
    _event(db, 1, "re_old", when=datetime.datetime(2026, 9, 1, tzinfo=datetime.UTC))
    _event(db, 1, "re_new", when=datetime.datetime(2026, 10, 1, tzinfo=datetime.UTC))
    items = _list(db)
    assert len(items) == 1
    assert items[0].bounced_address == "new@x.org"
    assert items[0].address_still_on_file is True


def test_it_says_when_the_address_has_already_been_changed(db):
    _alum(db, 1, "A", personal="fixed@x.org", work="also@x.org")
    _sent(db, 1, "re_1", "Broken@X.org")
    _event(db, 1, "re_1")
    assert _list(db)[0].address_still_on_file is False


def test_address_match_ignores_case(db):
    _alum(db, 1, "A", work="Same@X.org")
    _sent(db, 1, "re_1", "same@x.org")
    _event(db, 1, "re_1")
    assert _list(db)[0].address_still_on_file is True


def test_a_tag_matched_bounce_lists_without_an_address(db):
    _alum(db, 1, "A", personal="a@x.org")
    _event(db, 1, None)
    items = _list(db)
    assert items[0].bounced_address is None
    assert items[0].address_still_on_file is None


# ====================================================== endpoint / auth =====


def _ctx(*roles):
    return UserContext(
        user_id=9,
        auth_user_id=uuid.UUID("11111111-1111-1111-1111-111111111111"),
        email="staff@byu.edu",
        roles=list(roles),
    )


def test_the_bounced_route_uses_the_same_gate_as_unreachable():
    def guards(path):
        route = _route(path, "GET")
        seen, stack = set(), list(route.dependant.dependencies)
        while stack:
            dep = stack.pop()
            seen.add(dep.call)
            stack.extend(dep.dependencies)
        return seen

    bounced = guards("/survey/campaigns/{grad_year}/bounced")
    assert auth_deps.require_surveys_manage in bounced
    assert auth_deps.require_surveys_manage in guards(
        "/survey/campaigns/{grad_year}/unreachable"
    )


def test_view_only_cannot_read_the_bounced_list():
    with pytest.raises(AuthorizationError):
        asyncio.run(
            auth_deps.require_surveys_manage(
                _ctx(RoleName.VIEW_ONLY.value), dict(DEFAULT_GRANTS)
            )
        )


def test_unauthenticated_callers_get_401():
    app.dependency_overrides.clear()
    client = TestClient(app, raise_server_exceptions=False)
    r = client.get(f"/survey/campaigns/{_YEAR}/bounced")
    assert r.status_code == 401


def test_the_list_is_capped_and_reports_the_uncapped_total(db):
    for i in range(1, 6):
        _alum(db, i, f"N{i}", personal=f"n{i}@x.org")
        _event(db, i, f"re_{i}")
    page = _page(db, limit=2)
    assert page.total == 5 and page.limit == 2
    assert [i.alumni_id for i in page.items] == [1, 2]  # name order, first two
    full = _page(db)
    assert full.total == 5 and len(full.items) == 5
    assert full.limit == survey_bounces.BOUNCED_PAGE_DEFAULT


class _AuditSession:
    def __init__(self, *, commit_raises=False):
        self.added = []
        self._commit_raises = commit_raises

    def add(self, obj):
        self.added.append(obj)

    async def commit(self):
        if self._commit_raises:
            raise RuntimeError("audit_logs is unavailable")

    async def rollback(self):
        pass


def _bounced_get(monkeypatch, session, path, seen_limits=None):
    from app.schemas.survey import SurveyBouncedAlum, SurveyBouncedPage

    async def fake_list(session, year, *, limit):
        if seen_limits is not None:
            seen_limits.append(limit)
        return SurveyBouncedPage(
            graduation_year=year,
            total=1,
            limit=limit,
            items=[
                SurveyBouncedAlum(
                    alumni_id=3,
                    name="Zelda Quux",
                    bounced_address="zq@x.org",
                    bounce_subtype="General",
                    bounced_at=datetime.datetime(2026, 10, 1, tzinfo=datetime.UTC),
                    address_still_on_file=True,
                )
            ],
        )

    monkeypatch.setattr(survey_bounces, "list_bounced", fake_list)

    async def _override():
        yield session

    app.dependency_overrides[get_session] = _override
    app.dependency_overrides[auth_deps.require_surveys_manage] = lambda: _ctx(
        RoleName.FULL_ACCESS.value
    )
    try:
        return TestClient(app).get(path)
    finally:
        app.dependency_overrides.clear()


def test_the_read_is_audited_without_naming_anyone(monkeypatch):
    session = _AuditSession()
    r = _bounced_get(monkeypatch, session, f"/survey/campaigns/{_YEAR}/bounced")
    assert r.status_code == 200
    assert r.json()["items"][0]["bounced_address"] == "zq@x.org"
    assert r.json()["total"] == 1
    rows = [a for a in session.added if isinstance(a, AuditLog)]
    assert len(rows) == 1
    assert rows[0].action_type == "read_survey_bounced"
    assert rows[0].entity_id == _YEAR
    assert "Zelda" not in (rows[0].new_value or "")
    assert "zq@x.org" not in (rows[0].new_value or "")


def test_limit_defaults_to_200_and_is_bounded(monkeypatch):
    seen: list[int] = []
    base = f"/survey/campaigns/{_YEAR}/bounced"
    assert _bounced_get(monkeypatch, _AuditSession(), base, seen).status_code == 200
    assert _bounced_get(
        monkeypatch, _AuditSession(), base + "?limit=1000", seen
    ).status_code == 200
    assert seen == [200, 1000]
    for bad in ("0", "1001", "-1"):
        r = _bounced_get(monkeypatch, _AuditSession(), f"{base}?limit={bad}", seen)
        assert r.status_code == 422
    assert seen == [200, 1000]


def test_a_failed_audit_write_still_returns_the_list_and_logs_a_warning(
    monkeypatch, caplog
):
    session = _AuditSession(commit_raises=True)
    with caplog.at_level("WARNING", logger="app.api.routes.survey"):
        r = _bounced_get(monkeypatch, session, f"/survey/campaigns/{_YEAR}/bounced")
    assert r.status_code == 200
    warnings = [m for m in caplog.records if "read-audit write failed" in m.getMessage()]
    assert len(warnings) == 1
    text = warnings[0].getMessage()
    assert "read_survey_bounced" in text
    assert "Zelda" not in text and "zq@x.org" not in text
    assert "graduation_year=" not in text  # the scope is not logged either


# ============================================================ send path =====


class _SendSettings:
    survey_token_secret = "bounce-secret"
    survey_from_email = "test@example.com"
    survey_from_name = "BYU Finance Alumni"
    survey_app_base_url = "https://finance.alumni.byu.edu"
    resend_api_key = "re_test_key"


@pytest.fixture
def send_settings(monkeypatch):
    monkeypatch.setattr(survey_email, "get_settings", lambda: _SendSettings())


async def _no_sleep(_s):
    return None


def _rcpts(ids):
    return [
        survey_email.Recipient(i, f"A{i}", f"a{i}@example.com", (("Company", "X"),))
        for i in ids
    ]


class _IdSession(SendLogSession):
    """The shared send-log fake, plus the id write-back UPDATE."""

    def __init__(self, *, fail_update=False):
        super().__init__()
        self.updates: list[dict] = []
        self.rollbacks = 0
        self._fail_update = fail_update

    async def execute(self, stmt, params=None):
        if isinstance(stmt, Update):
            if self._fail_update:
                raise RuntimeError("column resend_email_id does not exist")
            self.updates.extend(params or [])
            return None
        return await super().execute(stmt)

    async def rollback(self):
        self.rollbacks += 1


def _send(session, monkeypatch, batch):
    monkeypatch.setattr(survey_email, "_send_batch", batch)
    monkeypatch.setattr(survey_email.asyncio, "sleep", _no_sleep)
    return asyncio.run(
        survey_email._send_and_log(
            session,
            _rcpts([1, 2, 3]),
            graduation_year=_YEAR,
            stage=0,
            cycle_seq=1,
            base_url="https://finance.alumni.byu.edu",
            from_field="BYU <test@example.com>",
            message=SurveyMessage(subject="S", intro="I", closing="C", on_file_fields=()),
        )
    )


def test_the_send_path_stores_each_id_and_address_on_its_row(
    send_settings, monkeypatch
):
    sent_emails: list[dict] = []

    async def batch(emails):
        sent_emails.extend(emails)
        return (10, 1, [f"re_{e['to'][0]}" for e in emails])

    session = _IdSession()
    sent, retry, error = _send(session, monkeypatch, batch)
    assert (sent, retry, error) == (3, None, None)
    assert {(u["b_alumni_id"], u["b_email_id"], u["b_sent_to"]) for u in session.updates} == {
        (1, "re_a1@example.com", "a1@example.com"),
        (2, "re_a2@example.com", "a2@example.com"),
        (3, "re_a3@example.com", "a3@example.com"),
    }
    assert all(u["b_reset_seq"] == 0 for u in session.updates)
    # Every email carries the fallback tags.
    tags = {t["name"]: t["value"] for t in sent_emails[0]["tags"]}
    assert tags == {"alumni_id": "1", "graduation_year": str(_YEAR), "stage": "0"}


def test_the_send_survives_the_id_bookkeeping_failing(send_settings, monkeypatch):
    async def batch(emails):
        return (10, 1, [f"re_{i}" for i in range(len(emails))])

    session = _IdSession(fail_update=True)
    sent, retry, error = _send(session, monkeypatch, batch)
    assert (sent, retry, error) == (3, None, None)
    assert session.logged(_YEAR, 0) == {1, 2, 3}  # the claim stands
    assert session.rollbacks == 1


def test_a_sender_that_reports_no_ids_still_sends_and_writes_nothing(
    send_settings, monkeypatch
):
    async def batch(emails):
        return (10, 1)  # the pre-#858 shape

    session = _IdSession()
    sent, _, error = _send(session, monkeypatch, batch)
    assert sent == 3 and error is None
    assert session.updates == []


def test_a_mismatched_id_count_is_never_paired_by_position(send_settings, monkeypatch):
    async def batch(emails):
        return (10, 1, ["re_only_one"])

    session = _IdSession()
    sent, _, _ = _send(session, monkeypatch, batch)
    assert sent == 3
    assert session.updates == []


def _resp(status, body):
    return httpx.Response(status, json=body, request=httpx.Request("POST", "http://x"))


def test_the_real_batch_sender_returns_resend_ids_in_order(send_settings, monkeypatch):
    async def post_json(url, *, api_key, payload, timeout):
        return _resp(200, {"data": [{"id": "re_a"}, {"id": "re_b"}]})

    monkeypatch.setattr(survey_email.mailer, "post_json", post_json)
    result = asyncio.run(survey_email._send_batch([{}, {}]))
    assert result[2] == ["re_a", "re_b"]


@pytest.mark.parametrize(
    "body",
    [
        {"data": [{"id": "re_a"}]},  # fewer than sent
        {"data": [{"id": "re_a"}, {"nope": 1}]},
        {"data": "x"},
        {},
        ["weird"],
    ],
)
def test_unreadable_ids_are_none_and_the_send_still_succeeds(
    send_settings, monkeypatch, body
):
    async def post_json(url, *, api_key, payload, timeout):
        return _resp(200, body)

    monkeypatch.setattr(survey_email.mailer, "post_json", post_json)
    result = asyncio.run(survey_email._send_batch([{}, {}]))
    assert result[2] is None


# ======================================================= schema / RLS =======


def test_the_new_table_is_in_the_rls_lockdown_sweep():
    from pathlib import Path

    repo = Path(__file__).resolve().parents[1]
    rls = (repo / "database" / "rls_lockdown.sql").read_text(encoding="utf-8")
    assert "ALTER TABLE survey_email_events ENABLE ROW LEVEL SECURITY;" in rls
    mig = (
        repo / "database" / "migrations" / "2026-10-07_survey_email_bounces.sql"
    ).read_text(encoding="utf-8")
    assert "ALTER TABLE survey_email_events ENABLE ROW LEVEL SECURITY;" in mig
    assert "UNIQUE (svix_id)" in mig
    schema = (repo / "database" / "schema.sql").read_text(encoding="utf-8")
    assert "CREATE TABLE survey_email_events" in schema
    assert "resend_email_id     varchar(100)" in schema
