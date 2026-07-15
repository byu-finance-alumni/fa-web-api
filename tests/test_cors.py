"""Tests for CORS configuration."""

from fastapi.testclient import TestClient

from app.core.config import get_settings
from app.main import app

client = TestClient(app)

ALLOWED_ORIGIN = "http://localhost:3000"
DISALLOWED_ORIGIN = "https://evil.example.com"


def test_default_origins_include_local_and_frontend():
    origins = get_settings().cors_origins_list
    assert ALLOWED_ORIGIN in origins
    assert "https://finance.alumni.byu.edu" in origins
    assert "https://finance-alumni-database.vercel.app" in origins


def test_preflight_request_from_allowed_origin():
    response = client.options(
        "/health",
        headers={
            "Origin": ALLOWED_ORIGIN,
            "Access-Control-Request-Method": "GET",
        },
    )
    assert response.status_code in (200, 204)
    assert response.headers.get("access-control-allow-origin") == ALLOWED_ORIGIN


def test_simple_request_from_allowed_origin_gets_cors_header():
    response = client.get("/health", headers={"Origin": ALLOWED_ORIGIN})
    assert response.status_code == 200
    assert response.headers.get("access-control-allow-origin") == ALLOWED_ORIGIN


def test_disallowed_origin_is_not_reflected():
    response = client.get("/health", headers={"Origin": DISALLOWED_ORIGIN})
    # Request still succeeds, but the browser-enforced CORS header must not
    # echo the disallowed origin.
    assert response.headers.get("access-control-allow-origin") != DISALLOWED_ORIGIN
