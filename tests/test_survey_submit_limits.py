"""Size caps on the PUBLIC survey submit payload (#426).

`POST /survey/respond/{token}` is token-gated but needs no login, and the
service behind it stages whatever it is handed as JSON. These tests pin that an
over-cap submission is refused at the route with a clear 413 and stages
nothing — the failure mode being guarded against is a multi-megabyte row
persisted by anyone holding a link, not a mistyped answer.

No DB and no network: the service layer is stubbed, so only the route's own
guard is exercised.
"""

import pytest

from app.api.routes.survey import (
    _SUBMIT_MAX_FIELD_BYTES,
    _SUBMIT_MAX_TOTAL_BYTES,
)
from app.core import rate_limit
from app.schemas.survey import SurveySubmitResult

# What the platform does to a body we let get too big: Vercel rejects it at the
# edge and the browser reports a CORS error, so the alum learns nothing.
_VERCEL_BODY_CAP_BYTES = 4_500_000


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


@pytest.fixture
def staged(monkeypatch):
    """Stub the stage-it service and record what the route passed through."""
    calls = []

    async def _submit(session, token, fields, has_photo):
        calls.append({"token": token, "fields": fields, "has_photo": has_photo})
        return SurveySubmitResult(staged=True, change_count=len(fields), survey_response_id=7)

    monkeypatch.setattr("app.services.survey_responses.submit_response", _submit)
    return calls


def _realistic_submission() -> dict[str, str]:
    """What an alum who filled in the whole form actually sends."""
    return {
        "profile.first_name": "Jonathan",
        "profile.last_name": "Featherstonehaugh",
        "profile.preferred_first_name": "Jon",
        "profile.spouse_first_name": "Maria",
        "profile.spouse_last_name": "Featherstonehaugh",
        "contact.personal_email": "jon.featherstonehaugh@example.com",
        "contact.phone": "801-555-0142",
        "contact.city": "Salt Lake City",
        "contact.state": "Utah",
        "contact.country": "United States",
        "contact.linkedin_url": "https://www.linkedin.com/in/jonathan-featherstonehaugh-8a1b2c3d/",
        "employment.current_employer": "Goldman Sachs Asset Management International",
        "employment.current_title": "Vice President, Global Investment Research",
        "employment.current_industry": "Investment Banking",
        "employment.current_city": "New York",
        "employment.current_state": "New York",
        "employment.current_country": "United States",
        "employment.current_zip": "10282",
        "profile.employment_status": "Employed Full-Time",
        "profile.other_designations": "CFA, CPA, CAIA, FRM, Series 7, Series 63",
    }


# ------------------------------------------------------------ over the cap ----


def test_oversized_total_payload_is_refused(client, staged):
    # Every answer is individually fine; together they are not.
    fields = {f"profile.note_{i}": "x" * 2000 for i in range(50)}
    resp = client.post("/survey/respond/tok-total", json={"fields": fields})
    assert resp.status_code == 413
    assert resp.json()["error"]["code"] == "payload_too_large"
    assert "too large" in resp.json()["error"]["message"]
    assert staged == []  # nothing reached the stager


def test_oversized_single_field_is_refused(client, staged):
    fields = {"employment.current_title": "x" * (_SUBMIT_MAX_FIELD_BYTES + 1)}
    resp = client.post("/survey/respond/tok-field", json={"fields": fields})
    assert resp.status_code == 413
    assert resp.json()["error"]["code"] == "payload_too_large"
    assert "too long" in resp.json()["error"]["message"]
    assert staged == []


def test_many_junk_keys_are_refused_by_the_total(client, staged):
    # Unknown keys are dropped downstream, so only the total cap bounds them.
    fields = {f"junk-key-{i:06d}": "" for i in range(6000)}
    resp = client.post("/survey/respond/tok-keys", json={"fields": fields})
    assert resp.status_code == 413
    assert staged == []


def test_the_cap_is_measured_in_bytes_not_characters(client, staged):
    # A 4-byte emoji must count as 4, or a payload could be four times the size
    # its character count claims.
    fields = {"profile.first_name": "\U0001f600" * ((_SUBMIT_MAX_FIELD_BYTES // 4) + 1)}
    resp = client.post("/survey/respond/tok-utf8", json={"fields": fields})
    assert resp.status_code == 413
    assert staged == []


# ---------------------------------------------------------- under the cap ----


def test_a_realistic_submission_passes_through_untouched(client, staged):
    fields = _realistic_submission()
    resp = client.post(
        "/survey/respond/tok-ok", json={"fields": fields, "has_photo": True}
    )
    assert resp.status_code == 200
    assert resp.json() == {
        "staged": True,
        "change_count": len(fields),
        "survey_response_id": 7,
    }
    assert staged == [{"token": "tok-ok", "fields": fields, "has_photo": True}]


def test_a_real_submission_is_nowhere_near_either_cap(client, staged):
    # The guard is an abuse guard, not a data rule: the honest worst case has to
    # sit far below it, or it becomes a rule nobody wrote down.
    fields = _realistic_submission()
    total = sum(len(k.encode()) + len(v.encode()) for k, v in fields.items())
    assert total * 20 < _SUBMIT_MAX_TOTAL_BYTES
    longest = max(len(v.encode()) for v in fields.values())
    assert longest * 20 < _SUBMIT_MAX_FIELD_BYTES


def test_an_empty_photo_only_submission_still_stages(client, staged):
    # A photo-only submit (#537) sends no fields at all and must not be caught.
    resp = client.post(
        "/survey/respond/tok-photo", json={"fields": {}, "has_photo": True}
    )
    assert resp.status_code == 200
    assert staged[0]["has_photo"] is True


def test_a_field_exactly_at_the_cap_is_accepted(client, staged):
    key = "employment.current_title"
    value = "x" * (_SUBMIT_MAX_FIELD_BYTES - len(key.encode()))
    resp = client.post("/survey/respond/tok-edge", json={"fields": {key: value}})
    assert resp.status_code == 200
    assert staged[0]["fields"] == {key: value}


# ------------------------------------------------------- platform ceiling ----


def test_our_cap_fires_well_before_vercels_opaque_one():
    # Above Vercel's edge cap the request never reaches this app, and the browser
    # calls it a CORS error. A cap at or above that ceiling would be dead code.
    assert _SUBMIT_MAX_TOTAL_BYTES * 50 < _VERCEL_BODY_CAP_BYTES
    assert _SUBMIT_MAX_FIELD_BYTES < _SUBMIT_MAX_TOTAL_BYTES


# ---------------------------------------------- the two caps must agree ------


def test_the_route_cap_never_preempts_a_declared_column_limit():
    """The abuse guard here must sit ABOVE every column's own `max_length`.

    These two limits were written independently — the byte cap in this route,
    the per-column character caps with the field table — and they disagreed:
    a 4 KiB byte cap fired before `other_designations` (10000 characters) could
    ever be filled, so that column's declared limit was unreachable dead code
    and a long-but-legitimate answer got a 413 instead of the real rule.

    Asserted against the REAL field table rather than a copied number, so
    widening a column or tightening this cap fails here instead of silently
    re-introducing the same mismatch.
    """
    from app.services.survey_responses import _FIELDS

    declared = [f.max_length for f in _FIELDS if f.max_length is not None]
    assert declared, "the field table declares no max_length at all — check the import"

    # Four bytes is UTF-8's worst case per character, so a field filled to its
    # declared limit with astral-plane text still has to fit under the byte cap.
    worst_case_bytes = max(declared) * 4
    assert worst_case_bytes <= _SUBMIT_MAX_FIELD_BYTES, (
        f"the widest column allows {max(declared)} characters "
        f"({worst_case_bytes} bytes worst case) but the route rejects anything "
        f"over {_SUBMIT_MAX_FIELD_BYTES} bytes — the column limit is unreachable"
    )
