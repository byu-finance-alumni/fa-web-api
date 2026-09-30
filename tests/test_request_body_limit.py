"""Ingress request-body size guard (defense-in-depth).

The per-route caps (`_read_capped` for uploads, the survey field-byte caps) only
apply after the body has been read and, for JSON, fully parsed. In production
Vercel's ~4.5 MB edge cap rejects an oversized body before the function runs, but
that cap does not exist off-Vercel. `request_body_size_limit_middleware` closes
that gap by refusing on Content-Length before the route ever reads the body.

The real limit sits above the single largest legitimate body (a 20 MiB
headshot), so these tests shrink it via monkeypatch rather than shipping 24 MiB
of payload.
"""

import pytest
from fastapi.testclient import TestClient

from app import main
from app.core.database import get_session


@pytest.fixture
def client():
    async def _no_db_session():
        yield None

    main.app.dependency_overrides[get_session] = _no_db_session
    with TestClient(main.app) as test_client:
        yield test_client
    main.app.dependency_overrides.clear()


def test_oversized_body_is_refused_before_the_route(client, monkeypatch):
    monkeypatch.setattr(main, "_MAX_REQUEST_BODY_BYTES", 16)
    # 100 bytes > the 16-byte test limit. The token is irrelevant: a valid route
    # would 404 it, but the guard must fire FIRST, so we see 413, not 404.
    resp = client.post(
        "/survey/respond/whatever",
        content=b'{"fields":{"a":"' + b"x" * 80 + b'"}}',
        headers={"content-type": "application/json"},
    )
    assert resp.status_code == 413
    assert resp.json()["error"]["code"] == "payload_too_large"
    assert resp.headers.get("x-content-type-options") == "nosniff"


def test_body_within_the_limit_passes_through(client, monkeypatch):
    # A generous limit: the guard must NOT interfere; the request reaches the
    # route and gets the route's own answer (404 for an unusable token), never a
    # 413 from the guard.
    monkeypatch.setattr(main, "_MAX_REQUEST_BODY_BYTES", 24 * 1024 * 1024)
    resp = client.post(
        "/survey/respond/whatever",
        json={"fields": {"contact.city": "Provo"}},
    )
    assert resp.status_code != 413


def test_missing_content_length_is_not_blocked(client, monkeypatch):
    # A request with no numeric Content-Length (or a malformed one) slips past
    # this single check by design — it is still bounded downstream — and must not
    # be turned into a 413 here.
    monkeypatch.setattr(main, "_MAX_REQUEST_BODY_BYTES", 16)
    resp = client.get("/health")
    assert resp.status_code == 200
