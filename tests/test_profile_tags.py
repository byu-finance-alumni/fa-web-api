"""Tests for tag + status-label vocabulary and the assign/remove routes (#41).

Covers the canonical-vocabulary validators (incl. the #41-added values) and the
auth/validation gating on the alumni tag / status-label endpoints. Happy-path
assignment is exercised end-to-end elsewhere; here we lock the vocab + guards
without needing a DB (CI has none).
"""

import uuid

import pytest
from fastapi.testclient import TestClient

from app.api.dependencies.auth import get_current_db_user
from app.core.database import get_session
from app.core.dropdowns import (
    STATUS_LABELS,
    TAGS,
    validate_status_label,
    validate_tag,
)
from app.main import app
from app.schemas.auth import UserContext


def _ctx(*roles: str) -> UserContext:
    return UserContext(
        user_id=1,
        auth_user_id=uuid.UUID("11111111-1111-1111-1111-111111111111"),
        roles=list(roles),
    )


async def _no_db():
    yield None


# --- vocabulary validators ----------------------------------------------------


def test_tag_vocabulary_includes_prd_values():
    # The #41 PRD set; "Inactive" is a status label, not a tag (doc typo).
    for tag in (
        "Mentor",
        "Speaker",
        "Recruiter",
        "Donor",
        "Highly Engaged",
        "Warm Contact",
        "High Value",
        "Club/Recruiting",
        "Finance Orgs",
        "Advisory Boards",
    ):
        assert tag in TAGS, tag
        assert validate_tag(tag) == tag
    assert validate_tag("  Mentor  ") == "Mentor"  # trims


def test_status_label_vocabulary_includes_retired():
    for label in ("Inactive", "Deceased", "Lost Contact", "Retired", "Do Not Contact"):
        assert label in STATUS_LABELS, label
        assert validate_status_label(label) == label


@pytest.mark.parametrize("bad", ["", "   ", "Bogus", "mentor", "VIP"])
def test_validate_tag_rejects_noncanonical(bad):
    with pytest.raises(ValueError):
        validate_tag(bad)


@pytest.mark.parametrize("bad", ["", "Active", "retired", "Unknown"])
def test_validate_status_label_rejects_noncanonical(bad):
    with pytest.raises(ValueError):
        validate_status_label(bad)


# --- route auth / validation gating -------------------------------------------


@pytest.fixture
def client():
    app.dependency_overrides[get_session] = _no_db
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


def test_add_tag_requires_auth(client):
    resp = client.post("/alumni/1/tags", json={"tag": "Mentor"})
    assert resp.status_code == 401


def test_add_tag_forbidden_for_view_only(client):
    app.dependency_overrides[get_current_db_user] = lambda: _ctx("view_only")
    resp = client.post("/alumni/1/tags", json={"tag": "Mentor"})
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "forbidden"


def test_add_tag_rejects_noncanonical_value(client):
    app.dependency_overrides[get_current_db_user] = lambda: _ctx("full_access")
    resp = client.post("/alumni/1/tags", json={"tag": "NotARealTag"})
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "validation_error"


def test_add_status_label_requires_auth(client):
    resp = client.post("/alumni/1/status-labels", json={"label": "Retired"})
    assert resp.status_code == 401


def test_add_status_label_forbidden_for_view_only(client):
    app.dependency_overrides[get_current_db_user] = lambda: _ctx("view_only")
    resp = client.post("/alumni/1/status-labels", json={"label": "Retired"})
    assert resp.status_code == 403


def test_add_status_label_rejects_noncanonical_value(client):
    app.dependency_overrides[get_current_db_user] = lambda: _ctx("full_access")
    resp = client.post("/alumni/1/status-labels", json={"label": "Nope"})
    assert resp.status_code == 422
