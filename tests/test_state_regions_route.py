"""Tests for GET /vocabulary/state-regions — the state -> region crosswalk (#283).

The point of this endpoint is that the frontend does NOT keep its own copy of the
region map, so the tests that matter are the ones that pin the served payload to
:mod:`app.services.state_regions` itself. If someone edits the map, these pass
unchanged; if someone reshapes or hand-edits the *endpoint*, they fail.

Offline — the route touches no database (the payload is built from code at
import), so the session dependency is stubbed out like the vocabulary tests do.
"""

import uuid

import pytest
from fastapi.testclient import TestClient

from app.api.dependencies.auth import get_current_db_user
from app.core.database import get_session
from app.core.us_states import STATE_NAME_BY_CODE
from app.main import app
from app.schemas.auth import UserContext
from app.services.state_regions import REGIONS, STATES_BY_REGION, region_for_state


def _ctx(*roles: str, user_id: int = 1) -> UserContext:
    return UserContext(
        user_id=user_id,
        auth_user_id=uuid.UUID("11111111-1111-1111-1111-111111111111"),
        roles=list(roles),
    )


async def _no_db_session():
    yield None


@pytest.fixture
def client():
    app.dependency_overrides[get_session] = _no_db_session
    app.dependency_overrides[get_current_db_user] = lambda: _ctx("view_only")
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


def _expected_map() -> dict[str, str]:
    """The flat state -> region map, rebuilt from the service module's own
    region -> [states] grouping (the shape a human reviews)."""
    return {
        state: region
        for region, states in STATES_BY_REGION.items()
        for state in states
    }


# --- the anti-drift assertions ------------------------------------------------


def test_payload_matches_state_regions_module_exactly(client):
    """The served map IS app.services.state_regions' map — no extras, none
    missing, no divergent values."""
    resp = client.get("/vocabulary/state-regions")
    assert resp.status_code == 200
    assert resp.json()["region_by_state"] == _expected_map()


def test_payload_covers_all_51_states_and_nothing_else(client):
    """All 50 states + DC, and only those — keys are the canonical full names
    from app.core.us_states, which is what the hygiene cleaner normalizes to."""
    region_by_state = client.get("/vocabulary/state-regions").json()["region_by_state"]
    assert len(region_by_state) == 51
    assert set(region_by_state) == set(STATE_NAME_BY_CODE.values())


def test_every_state_agrees_with_region_for_state(client):
    """Equivalence with the LOOKUP the write path actually calls — not merely
    with the literal it was built from. This is what guarantees the region the
    form shows is the region the server persists."""
    region_by_state = client.get("/vocabulary/state-regions").json()["region_by_state"]
    for state, region in region_by_state.items():
        assert region_for_state(state) == region, state


def test_regions_list_matches_module_and_covers_every_value(client):
    body = client.get("/vocabulary/state-regions").json()
    assert body["regions"] == list(REGIONS)
    # "Mountain West" et al. must never leak in via a stray map entry.
    assert set(body["region_by_state"].values()) <= set(REGIONS)


# --- contract / posture -------------------------------------------------------


def test_literal_path_is_not_shadowed_by_the_category_route(client):
    """/vocabulary/{category} is declared after this route; if the two are ever
    reordered, "state-regions" gets parsed as a VocabularyCategory and 422s."""
    assert client.get("/vocabulary/state-regions").status_code == 200


def test_response_is_cacheable(client):
    resp = client.get("/vocabulary/state-regions")
    assert resp.headers["Cache-Control"] == "public, max-age=3600"


def test_requires_authentication(client):
    """Same read gate as GET /vocabulary/{category}: provisioned roles only."""
    app.dependency_overrides.pop(get_current_db_user)
    assert client.get("/vocabulary/state-regions").status_code == 401


def test_has_response_model_in_openapi():
    """An explicit response_model is what puts this in the frontend's generated
    types — a bare dict return would leave the map untyped on the client, which
    is exactly the drift this endpoint exists to prevent."""
    schema = app.openapi()
    responses = schema["paths"]["/vocabulary/state-regions"]["get"]["responses"]
    ref = responses["200"]["content"]["application/json"]["schema"]["$ref"]
    assert ref.endswith("/StateRegionMap")

    props = schema["components"]["schemas"]["StateRegionMap"]["properties"]
    assert set(props) == {"regions", "region_by_state"}
