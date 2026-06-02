"""Smoke tests for the basic API: root, health, and DB health endpoints."""

from fastapi.testclient import TestClient

from app import __version__
from app.main import app

client = TestClient(app)


def test_root():
    response = client.get("/")
    assert response.status_code == 200
    body = response.json()
    assert body["service"] == "fa-web-api"
    assert body["status"] == "ok"


def test_health():
    response = client.get("/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["version"] == __version__
    assert "environment" in body


def test_health_db_reports_status_without_crashing():
    """The DB check must respond cleanly whether or not a database is configured.

    - No/unreachable DB  -> 503 with the structured error envelope.
    - Reachable DB        -> 200 with {"database": "connected"}.
    """
    response = client.get("/health/db")
    assert response.status_code in (200, 503)
    body = response.json()
    if response.status_code == 503:
        assert body["error"]["code"] in (
            "database_not_configured",
            "database_unavailable",
        )
        # Never leak internal details.
        assert "Traceback" not in body["error"]["message"]
    else:
        assert body["database"] == "connected"
