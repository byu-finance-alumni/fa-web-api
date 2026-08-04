"""Survey link expiry + public-route rate limiting (#360).

The survey link is a STATELESS HMAC — there is no token row to look up and none
to revoke — so its lifetime has to be carried inside the signed payload. These
tests pin both halves of that: the 7-day rule itself, and the fact that the
issued-at is covered by the signature (an alum who edits the timestamp in their
own URL must be indistinguishable from someone posting garbage).

No DB, no network: token verification is pure, and the route tests stub the
service layer so only the dependency chain is exercised.
"""

import base64
import datetime

import pytest

from app.core import rate_limit
from app.services import survey_email
from app.services.survey_email import (
    SURVEY_TOKEN_TTL_SECONDS,
    make_survey_token,
    verify_survey_token,
)

UTC = datetime.UTC


class _FakeSettings:
    survey_token_secret = "unit-test-secret"
    survey_from_email = "test@jakegunnell.com"
    survey_from_name = "BYU Finance Alumni"
    survey_app_base_url = "https://finance.alumni.byu.edu"


@pytest.fixture
def fake_settings(monkeypatch):
    settings = _FakeSettings()
    monkeypatch.setattr(survey_email, "get_settings", lambda: settings)
    return settings


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _repack(token: str, payload: str) -> str:
    """Swap a token's payload while KEEPING its original signature — exactly what
    a recipient editing their own URL can do."""
    _, sig_b64 = token.split(".", 1)
    return f"{_b64(payload.encode())}.{sig_b64}"


# ----------------------------------------------------------------- expiry ----


def test_fresh_token_is_accepted(fake_settings):
    assert verify_survey_token(make_survey_token(42, 1900)) == 42


def test_token_just_inside_seven_days_is_accepted(fake_settings):
    now = datetime.datetime.now(UTC)
    token = make_survey_token(42, 1900, issued_at=now - datetime.timedelta(days=6, hours=23))
    assert verify_survey_token(token, now=now) == 42


def test_token_older_than_seven_days_is_rejected(fake_settings):
    now = datetime.datetime.now(UTC)
    token = make_survey_token(42, 1900, issued_at=now - datetime.timedelta(days=7, seconds=1))
    assert verify_survey_token(token, now=now) is None


def test_token_expires_exactly_at_the_next_reminder(fake_settings):
    # The whole point of 7 days: a link dies as the next stage's link is minted.
    assert SURVEY_TOKEN_TTL_SECONDS == survey_email._STAGE_WINDOW_DAYS * 86400
    issued = datetime.datetime(2026, 1, 1, tzinfo=UTC)
    token = make_survey_token(42, 1900, issued_at=issued)
    assert verify_survey_token(token, now=issued + datetime.timedelta(days=7)) == 42
    later = issued + datetime.timedelta(days=7, minutes=1)
    assert verify_survey_token(token, now=later) is None


def test_minted_link_carries_the_issue_time(fake_settings):
    # The link the email actually contains is minted at send time, so its
    # issued-at IS its sent-at.
    link = survey_email._survey_link("https://x.test", 42, 1900)
    token = link.rsplit("/", 1)[-1]
    payload = base64.urlsafe_b64decode(
        token.split(".")[0] + "=" * (-len(token.split(".")[0]) % 4)
    ).decode()
    alumni_id, grad_year, issued = payload.split(".")
    assert (alumni_id, grad_year) == ("42", "1900")
    age = datetime.datetime.now(UTC).timestamp() - int(issued)
    assert 0 <= age < 60


# ------------------------------------------------------------- tampering -----


def test_tampered_issued_at_fails_the_signature(fake_settings):
    """Editing the timestamp to buy more time is a signature failure, not a
    longer life — the issued-at is INSIDE the signed payload."""
    now = datetime.datetime.now(UTC)
    old = now - datetime.timedelta(days=30)
    token = make_survey_token(42, 1900, issued_at=old)
    assert verify_survey_token(token, now=now) is None
    # Rewrite the payload's issued-at to "now" but keep the original signature.
    forged = _repack(token, f"42.1900.{int(now.timestamp())}")
    assert verify_survey_token(forged, now=now) is None
    # Even shifting it into the future is rejected.
    future = _repack(token, f"42.1900.{int((now + datetime.timedelta(days=365)).timestamp())}")
    assert verify_survey_token(future, now=now) is None


def test_tampered_alumni_id_still_fails(fake_settings):
    now = datetime.datetime.now(UTC)
    token = make_survey_token(42, 1900, issued_at=now)
    forged = _repack(token, f"43.1900.{int(now.timestamp())}")
    assert verify_survey_token(forged, now=now) is None


def test_a_token_signed_with_another_secret_is_rejected(fake_settings, monkeypatch):
    token = make_survey_token(42, 1900)
    other = _FakeSettings()
    other.survey_token_secret = "a-different-secret"
    monkeypatch.setattr(survey_email, "get_settings", lambda: other)
    assert verify_survey_token(token) is None


def test_garbage_and_malformed_payloads_are_rejected(fake_settings):
    assert verify_survey_token("") is None
    assert verify_survey_token("not-a-token") is None
    # A correctly signed payload with the wrong number of fields is still out.
    payload = "42.1900.123.456"
    import hashlib
    import hmac as _hmac

    sig = _hmac.new(
        _FakeSettings.survey_token_secret.encode(), payload.encode(), hashlib.sha256
    ).digest()
    assert verify_survey_token(f"{_b64(payload.encode())}.{_b64(sig)}") is None


# ---------------------------------------------------------------- legacy -----


def _legacy_token(alumni_id: int, graduation_year: int) -> str:
    """A pre-#360 token: two payload fields, no issued-at."""
    import hashlib
    import hmac as _hmac

    payload = f"{alumni_id}.{graduation_year}".encode()
    sig = _hmac.new(
        _FakeSettings.survey_token_secret.encode(), payload, hashlib.sha256
    ).digest()
    return f"{_b64(payload)}.{_b64(sig)}"


def test_legacy_token_does_not_crash_the_verifier(fake_settings):
    # The one thing that must never happen: a 500 on an old link.
    before = survey_email._LEGACY_TOKEN_VALID_UNTIL - datetime.timedelta(days=1)
    assert verify_survey_token(_legacy_token(42, 1900), now=before) == 42


def test_legacy_token_dies_at_the_cutover_deadline(fake_settings):
    after = survey_email._LEGACY_TOKEN_VALID_UNTIL + datetime.timedelta(seconds=1)
    assert verify_survey_token(_legacy_token(42, 1900), now=after) is None


def test_legacy_grace_is_a_fixed_instant_not_a_rolling_one(fake_settings):
    # Anchoring the grace to process start would restart it on every serverless
    # cold start, i.e. never expire. Two far-apart "now"s must agree.
    tok = _legacy_token(42, 1900)
    way_after = survey_email._LEGACY_TOKEN_VALID_UNTIL + datetime.timedelta(days=365)
    assert verify_survey_token(tok, now=way_after) is None


# ------------------------------------------------------------ rate limits ----


@pytest.fixture
def client():
    from fastapi.testclient import TestClient

    from app.core.database import get_session
    from app.main import app

    async def _no_db_session():
        yield None

    rate_limit.reset()
    app.dependency_overrides[get_session] = _no_db_session
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()
    rate_limit.reset()


def test_respond_read_is_rate_limited_per_token(client, monkeypatch):
    async def none_resp(session, token):
        return None

    monkeypatch.setattr(survey_email, "get_respondent", none_resp)
    statuses = [client.get("/survey/respond/tok-a").status_code for _ in range(35)]
    assert 429 in statuses  # SURVEY_RESPOND_READ_LIMITER allows 30 per token
    assert statuses[:30] == [404] * 30
    # A different token has its own budget — one abused link cannot lock out the
    # rest of the cohort.
    assert client.get("/survey/respond/tok-b").status_code == 404


def test_submit_is_rate_limited_per_token(client, monkeypatch):
    from app.schemas.survey import SurveySubmitResult

    async def staged(session, token, fields, has_photo):
        return SurveySubmitResult(staged=True, change_count=1, survey_response_id=1)

    monkeypatch.setattr(survey_email, "get_respondent", staged)
    monkeypatch.setattr(
        "app.services.survey_responses.submit_response", staged
    )
    statuses = [
        client.post("/survey/respond/tok-a", json={"fields": {}}).status_code
        for _ in range(14)
    ]
    assert 429 in statuses  # SURVEY_SUBMIT_LIMITER allows 10 per token
    assert statuses[:10] == [200] * 10
    assert client.post("/survey/respond/tok-b", json={"fields": {}}).status_code == 200


def test_photo_upload_is_rate_limited_per_token(client):
    # The bytes never get as far as storage (a text/plain part is rejected as a
    # 422 by the handler), but the limiter is a ROUTE dependency, so it runs and
    # counts the attempt either way — which is exactly what a flood looks like.
    def _post():
        return client.post(
            "/survey/respond/tok-a/photo",
            data={"survey_response_id": 1},
            files={"photo": ("x.txt", b"x", "text/plain")},
        )

    responses = [_post() for _ in range(8)]
    statuses = [r.status_code for r in responses]
    assert 429 in statuses  # SURVEY_PHOTO_LIMITER allows 5 per token
    assert statuses[:5] == [422] * 5
    assert "JPEG" in responses[0].json()["error"]["message"]


def test_rate_limited_response_carries_the_standard_error_envelope(client, monkeypatch):
    async def none_resp(session, token):
        return None

    monkeypatch.setattr(survey_email, "get_respondent", none_resp)
    blocked = None
    for _ in range(40):
        resp = client.get("/survey/respond/tok-envelope")
        if resp.status_code == 429:
            blocked = resp
            break
    assert blocked is not None
    assert blocked.json()["error"]["code"] == "rate_limited"
    assert blocked.headers["Retry-After"] == "600"


def test_spoofed_forwarded_for_cannot_dodge_the_ip_budget(client, monkeypatch):
    """A rotating fake X-Forwarded-For must not mint a fresh IP budget each
    request — the key is the hop the EDGE added (rightmost), not the one the
    caller prepended."""

    async def none_resp(session, token):
        return None

    monkeypatch.setattr(survey_email, "get_respondent", none_resp)
    # Same real client (the last hop), a different forged leading hop each time.
    for i in range(5):
        client.get(
            f"/survey/respond/tok-{i}",
            headers={"x-forwarded-for": f"9.9.9.{i}, 203.0.113.7"},
        )
    ip_bucket = rate_limit._WINDOWS["survey:respond_read:ip"]
    assert list(ip_bucket) == ["203.0.113.7"]
    assert len(ip_bucket["203.0.113.7"]) == 5


def test_client_key_prefers_the_vercel_edge_header():
    from starlette.requests import Request as StarletteRequest

    def _req(headers: dict[str, str]) -> StarletteRequest:
        return StarletteRequest(
            {
                "type": "http",
                "headers": [(k.encode(), v.encode()) for k, v in headers.items()],
                "client": ("127.0.0.1", 1234),
            }
        )

    assert (
        rate_limit._client_key(
            _req({"x-vercel-forwarded-for": "1.1.1.1", "x-forwarded-for": "2.2.2.2"})
        )
        == "1.1.1.1"
    )
    assert rate_limit._client_key(_req({"x-real-ip": "3.3.3.3"})) == "3.3.3.3"
    assert rate_limit._client_key(_req({})) == "127.0.0.1"


def test_limiter_state_cannot_grow_without_bound(client, monkeypatch):
    """The limiter runs BEFORE the token is verified, so garbage tokens mint
    keys. Without a ceiling that is an anonymous memory-exhaustion DoS against
    every limiter in the app, not just this route."""

    async def none_resp(session, token):
        return None

    monkeypatch.setattr(survey_email, "get_respondent", none_resp)
    monkeypatch.setattr(rate_limit, "_MAX_ACTORS_PER_BUCKET", 25)
    for i in range(200):
        client.get(f"/survey/respond/junk-{i}")
    token_bucket = rate_limit._WINDOWS["survey:respond_read:token"]
    assert len(token_bucket) == 25
    # The flood's OWN per-IP window is touched every request, so it is always the
    # freshest entry and survives — a flood can never evict its own budget.
    ip_bucket = rate_limit._WINDOWS["survey:respond_read:ip"]
    assert len(ip_bucket["testclient"]) == 200


def test_a_blocked_caller_keeps_its_window(client, monkeypatch):
    # The 429 path must still re-seat the actor in the LRU, or a caller being
    # throttled would be the first thing evicted and would get a clean budget.
    async def none_resp(session, token):
        return None

    monkeypatch.setattr(survey_email, "get_respondent", none_resp)
    for _ in range(40):
        client.get("/survey/respond/tok-blocked")
    assert client.get("/survey/respond/tok-blocked").status_code == 429
    hits = rate_limit._WINDOWS["survey:respond_read:token"]
    assert len(next(iter(hits.values()))) == 30  # capped at the limit, not growing


def test_the_dead_link_message_never_says_which_kind_of_dead(client, monkeypatch):
    """Expired and never-valid must be indistinguishable to a prober, while
    still telling a real alum what to do."""

    async def none_resp(session, token):
        return None

    monkeypatch.setattr(survey_email, "get_respondent", none_resp)
    body = client.get("/survey/respond/whatever").json()
    message = body["error"]["message"]
    assert message == survey_email.LINK_DEAD_MESSAGE
    assert "seven days" in message
    assert "contact the BYU Finance department" in message
