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


# --- Production never trusts a localhost origin (#597) ------------------------
# The production built-in default (CORS_ORIGINS unset) holds only the
# production frontends; other environments add the dev frontend and localhost.
# An explicit CORS_ORIGINS in production still has its loopback origins dropped
# (with a warning) — never a refusal to start, which could take prod down over a
# stray entry.

PROD_FRONTENDS = [
    "https://finance.alumni.byu.edu",
    "https://finance-alumni-database.vercel.app",
    "https://dev-fa-web-app.vercel.app",
]


def _settings(**overrides):
    from app.core.config import Settings

    return Settings(_env_file=None, **overrides)


def test_production_default_is_production_frontends_only(monkeypatch, caplog):
    # CORS_ORIGINS unset in production: neither localhost nor the dev frontend.
    monkeypatch.delenv("CORS_ORIGINS", raising=False)
    monkeypatch.delenv("CORS_ORIGIN", raising=False)
    with caplog.at_level("WARNING", logger="app.core.config"):
        origins = _settings(environment="production").cors_origins_list
    assert origins == [
        "https://finance.alumni.byu.edu",
        "https://finance-alumni-database.vercel.app",
    ]
    assert caplog.text == ""


def test_production_still_drops_an_explicit_localhost(monkeypatch, caplog):
    monkeypatch.setenv("CORS_ORIGINS", ",".join(["http://localhost:3000", *PROD_FRONTENDS]))
    with caplog.at_level("WARNING", logger="app.core.config"):
        origins = _settings(environment="production").cors_origins_list
    assert origins == PROD_FRONTENDS
    assert "localhost" in caplog.text


def test_production_drops_every_loopback_spelling_from_explicit_env(monkeypatch):
    monkeypatch.setenv(
        "CORS_ORIGINS",
        "http://localhost:3000, http://127.0.0.1:3000,http://[::1]:3000,"
        "http://app.localhost:3000,https://finance.alumni.byu.edu",
    )
    origins = _settings(environment="production").cors_origins_list
    assert origins == ["https://finance.alumni.byu.edu"]


def test_production_without_localhost_logs_nothing(monkeypatch, caplog):
    monkeypatch.setenv("CORS_ORIGINS", ",".join(PROD_FRONTENDS))
    with caplog.at_level("WARNING", logger="app.core.config"):
        origins = _settings(environment="production").cors_origins_list
    assert origins == PROD_FRONTENDS
    assert caplog.text == ""


def test_development_keeps_localhost(monkeypatch):
    monkeypatch.delenv("CORS_ORIGINS", raising=False)
    monkeypatch.delenv("CORS_ORIGIN", raising=False)
    origins = _settings(environment="development").cors_origins_list
    assert ALLOWED_ORIGIN in origins
    assert "https://dev-fa-web-app.vercel.app" in origins

