"""#594 — the export can't widen past what the list would show.

Two holes, both closed server-side:

  * ``is_alumni: null`` in the export body used to drop the friends/alumni
    predicate (``exclude_unset`` counts an explicit null as SET), so an "alumni"
    export also carried friends of the program. Null now means the default
    (alumni only); "both" needs the explicit ``kind: "all"``.
  * ``include_archived`` and the exact-``email`` filter were honoured for ANY
    ``alumni.export`` holder, while ``GET /alumni`` limits both to full_access and
    up. The export now drops them below that tier, as the list does.
"""

import uuid

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.api.dependencies import auth as auth_deps
from app.core.database import get_session
from app.main import app
from app.schemas.alumni_export import AlumniExportFilters
from app.schemas.auth import UserContext
from app.services import alumni_export
from app.services.alumni_export import _filters_dict


def _ctx(*roles: str) -> UserContext:
    return UserContext(
        user_id=1,
        auth_user_id=uuid.UUID("11111111-1111-1111-1111-111111111111"),
        roles=list(roles),
    )


# --- kind / is_alumni resolution ---------------------------------------------


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        ({}, True),
        ({"is_alumni": None}, True),  # the widening bug: null is now the default
        ({"kind": None}, True),
        ({"is_alumni": True}, True),
        ({"is_alumni": False}, False),
        ({"kind": "alumni"}, True),
        ({"kind": "friend"}, False),
        ({"kind": "all"}, None),  # the ONLY way to ask for both
        ({"kind": "friend", "is_alumni": False}, False),
        ({"kind": "alumni", "is_alumni": True}, True),
    ],
)
def test_population_resolves_to_the_query_builder(body, expected):
    filters = AlumniExportFilters(**body)
    assert filters.effective_is_alumni is expected
    # And it is ALWAYS passed, never left to exclude_unset.
    out = _filters_dict(filters)
    assert out["is_alumni"] is expected
    assert "kind" not in out


@pytest.mark.parametrize(
    "body",
    [
        {"kind": "all", "is_alumni": True},
        {"kind": "friend", "is_alumni": True},
        {"kind": "alumni", "is_alumni": False},
    ],
)
def test_contradictory_kind_and_is_alumni_is_rejected(body):
    with pytest.raises(ValidationError):
        AlumniExportFilters(**body)


def test_unknown_kind_is_rejected():
    with pytest.raises(ValidationError):
        AlumniExportFilters(kind="everyone")


# --- include_archived / email tier gate --------------------------------------


@pytest.fixture
def export_capture(monkeypatch):
    seen: dict = {}

    async def _count(session, filters, *, match_ids=True):
        seen["count_filters"] = filters
        return 0

    async def _csv(session, *, columns, filters, actor_user_id, match_ids=True):
        seen["filters"] = filters
        return "x\n"

    monkeypatch.setattr(alumni_export, "count_matching", _count)
    monkeypatch.setattr(alumni_export, "export_csv", _csv)
    return seen


@pytest.fixture
def client():
    async def _no_db_session():
        yield None

    app.dependency_overrides[get_session] = _no_db_session
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


def _as(role: str) -> None:
    app.dependency_overrides[auth_deps.require_alumni_export] = lambda: _ctx(role)


_BODY = {
    "columns": ["first_name"],
    "filters": {"include_archived": True, "email": "jane@example.org", "kind": "all"},
}


@pytest.mark.parametrize("role", ["student", "view_only"])
def test_below_full_access_export_drops_archived_and_email(client, export_capture, role):
    _as(role)
    resp = client.post("/alumni/export", json=_BODY)
    assert resp.status_code == 200, resp.text
    filters = export_capture["filters"]
    assert filters.include_archived is False
    assert filters.email is None
    # Dropped, not overwritten: an explicit null would count as SET.
    assert "include_archived" not in filters.model_fields_set
    assert "email" not in filters.model_fields_set
    # The rest of the body survives the re-validation.
    assert filters.kind == "all"
    assert export_capture["count_filters"] is filters


@pytest.mark.parametrize("role", ["full_access", "super_admin", "engineer"])
def test_full_access_export_keeps_archived_and_email(client, export_capture, role):
    _as(role)
    resp = client.post("/alumni/export", json=_BODY)
    assert resp.status_code == 200, resp.text
    filters = export_capture["filters"]
    assert filters.include_archived is True
    assert filters.email == "jane@example.org"


def test_null_is_alumni_from_the_wire_exports_alumni_only(client, export_capture):
    _as("full_access")
    resp = client.post(
        "/alumni/export",
        json={"columns": ["first_name"], "filters": {"is_alumni": None}},
    )
    assert resp.status_code == 200, resp.text
    assert _filters_dict(export_capture["filters"])["is_alumni"] is True
