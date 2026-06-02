"""Tests for Supabase JWT verification and the /auth/me endpoint.

These use HS256 tokens signed with a known test secret so verification can run
fully offline (no JWKS network calls).
"""

import time

import jwt
import pytest
from fastapi.testclient import TestClient

# >= 32 bytes to satisfy HS256 key-length recommendations.
TEST_SECRET = "test-secret-0123456789-abcdefghij-klmno"


@pytest.fixture
def client(monkeypatch):
    # Configure HS256 verification with a known secret and no Supabase URL
    # (so issuer/JWKS verification is skipped).
    monkeypatch.setenv("JWT_SECRET", TEST_SECRET)
    monkeypatch.setenv("SUPABASE_URL", "")

    from app.core.config import get_settings

    get_settings.cache_clear()

    from app.main import app

    yield TestClient(app)

    get_settings.cache_clear()


def _make_token(secret: str = TEST_SECRET, **overrides) -> str:
    payload = {
        "sub": "11111111-1111-1111-1111-111111111111",
        "aud": "authenticated",
        "email": "student.worker@byu.edu",
        "role": "authenticated",
        "exp": int(time.time()) + 3600,
    }
    payload.update(overrides)
    return jwt.encode(payload, secret, algorithm="HS256")


def _auth_header(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def test_me_requires_token(client):
    response = client.get("/auth/me")
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "unauthorized"


def test_me_rejects_garbage_token(client):
    response = client.get("/auth/me", headers=_auth_header("not-a-jwt"))
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "unauthorized"


def test_me_rejects_expired_token(client):
    token = _make_token(exp=int(time.time()) - 60)
    response = client.get("/auth/me", headers=_auth_header(token))
    assert response.status_code == 401


def test_me_rejects_wrong_signature(client):
    token = _make_token(secret="wrong-secret-0123456789-abcdefghij-klmno")
    response = client.get("/auth/me", headers=_auth_header(token))
    assert response.status_code == 401


def test_me_rejects_wrong_audience(client):
    token = _make_token(aud="some-other-audience")
    response = client.get("/auth/me", headers=_auth_header(token))
    assert response.status_code == 401


def test_me_accepts_valid_token(client):
    token = _make_token()
    response = client.get("/auth/me", headers=_auth_header(token))
    assert response.status_code == 200
    body = response.json()
    assert body["auth_user_id"] == "11111111-1111-1111-1111-111111111111"
    assert body["email"] == "student.worker@byu.edu"
