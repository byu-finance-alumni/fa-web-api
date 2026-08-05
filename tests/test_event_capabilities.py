"""Route-level enforcement of the event authoring capabilities (#378).

``events.create`` and ``events.import`` are editable capabilities, so the tests
that matter are the ones proving each ROUTE checks the CAPABILITY rather than a
frozen role list. Every case here does one of two things:

* strip the capability from a role that otherwise holds everything — the route
  must 403 (a capability the UI advertises but no endpoint enforces is worse
  than no capability at all);
* grant it to a role that holds nothing else — the route must let it through
  (otherwise the toggle is decorative and the split bought nothing).

Offline: the auth, permission-config, and session dependencies are overridden,
so no DATABASE_URL is required.
"""

import uuid

import pytest
from fastapi.testclient import TestClient

from app.api.dependencies.auth import get_current_db_user, get_permission_config
from app.core.capabilities import (
    DEFAULT_GRANTS,
    Capability,
    effective_capabilities,
)
from app.core.database import get_session
from app.main import app
from app.schemas.auth import UserContext


def _ctx(*roles: str) -> UserContext:
    return UserContext(
        user_id=1,
        auth_user_id=uuid.UUID("11111111-1111-1111-1111-111111111111"),
        roles=list(roles),
    )


async def _no_db_session():
    yield None


@pytest.fixture
def client():
    app.dependency_overrides[get_session] = _no_db_session
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


def _config(**grants: set) -> dict:
    """DEFAULT_GRANTS with the named roles' capability sets replaced."""
    config = dict(DEFAULT_GRANTS)
    for role, caps in grants.items():
        config[role] = frozenset(caps)
    return config


def _use_config(config: dict) -> None:
    app.dependency_overrides[get_permission_config] = lambda: config


def _as(role: str) -> None:
    app.dependency_overrides[get_current_db_user] = lambda: _ctx(role)


class _CreateSession:
    """Captures added rows and assigns a PK on flush, mirroring a real insert."""

    def __init__(self):
        self.added: list = []
        self.committed = False

    def add(self, obj):
        self.added.append(obj)

    async def flush(self):
        for obj in self.added:
            if getattr(obj, "event_id", None) is None and hasattr(obj, "event_name"):
                obj.event_id = 123

    async def commit(self):
        self.committed = True

    async def refresh(self, _obj):
        pass


def _with_session(session):
    async def _override():
        yield session

    return _override


_VALID_EVENT = {"event_name": "Spring Mixer", "event_date": "2026-04-10"}

# A file whose header set is wrong. Both import routes reject it BEFORE any
# database access, so the response cleanly separates "passed the guard" (200
# carrying a header error) from "blocked by the guard" (403) without a session.
_BAD_HEADER_CSV = {"file": ("a.csv", b"Nope,Wrong\nx,y\n", "text/csv")}

_IMPORT_ROUTES = [
    ("get", "/events/import/template", {}),
    (
        "post",
        "/events/import/preview",
        {"data": {"event_name": "Banquet"}, "files": _BAD_HEADER_CSV},
    ),
    (
        "post",
        "/events/import",
        {"data": {"event_name": "Banquet"}, "files": _BAD_HEADER_CSV},
    ),
]


# --- defaults -----------------------------------------------------------------


def test_event_capabilities_default_to_the_alumni_full_roles():
    # Seed set == the roles that held alumni.full before the split (#378), which
    # #379 then dissolved entirely; both new codes still land on the same roles.
    for role in ("full_access", "super_admin"):
        caps = effective_capabilities(DEFAULT_GRANTS, [role])
        assert Capability.EVENTS_CREATE in caps
        assert Capability.EVENTS_IMPORT in caps
    for role in ("student", "view_only"):
        caps = effective_capabilities(DEFAULT_GRANTS, [role])
        assert Capability.EVENTS_CREATE not in caps
        assert Capability.EVENTS_IMPORT not in caps
    # The engineer hard-override still covers both new codes, config or not.
    assert {
        Capability.EVENTS_CREATE,
        Capability.EVENTS_IMPORT,
    } <= effective_capabilities({}, ["engineer"])


# --- POST /events is gated on events.create -----------------------------------


def test_create_event_403_when_role_lacks_events_create(client):
    # full_access keeps every other capability it holds — ONLY events.create is
    # revoked, so a 403 can only come from the new capability check.
    _use_config(
        _config(
            full_access=DEFAULT_GRANTS["full_access"] - {Capability.EVENTS_CREATE}
        )
    )
    _as("full_access")
    response = client.post("/events", json=_VALID_EVENT)
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "forbidden"


def test_create_event_allowed_when_role_is_granted_events_create(client):
    # student holds neither alumni.full nor events.import — events.create alone
    # is enough, which is the entire point of splitting it out.
    _use_config(_config(student={Capability.VIEW, Capability.EVENTS_CREATE}))
    _as("student")
    session = _CreateSession()
    app.dependency_overrides[get_session] = _with_session(session)
    response = client.post("/events", json=_VALID_EVENT)
    assert response.status_code == 201
    assert session.committed is True


@pytest.mark.parametrize("role", ["student", "view_only"])
def test_create_event_403_for_default_student_and_view_only(client, role):
    _as(role)
    assert client.post("/events", json=_VALID_EVENT).status_code == 403


def test_create_event_allowed_for_engineer_with_empty_config(client):
    # The engineer hard-override must survive the split — even a wiped config.
    _use_config({})
    _as("engineer")
    app.dependency_overrides[get_session] = _with_session(_CreateSession())
    assert client.post("/events", json=_VALID_EVENT).status_code == 201


# --- the bulk-upload routes are gated on events.import ------------------------


@pytest.mark.parametrize("method,path,kwargs", _IMPORT_ROUTES)
def test_import_routes_403_when_role_lacks_events_import(
    client, method, path, kwargs
):
    # full_access keeps everything else, including events.create — only
    # events.import is revoked.
    _use_config(
        _config(
            full_access=DEFAULT_GRANTS["full_access"] - {Capability.EVENTS_IMPORT}
        )
    )
    _as("full_access")
    response = getattr(client, method)(path, **kwargs)
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "forbidden"


@pytest.mark.parametrize("method,path,kwargs", _IMPORT_ROUTES)
def test_import_routes_allowed_when_role_is_granted_events_import(
    client, method, path, kwargs
):
    # A role with nothing but view + events.import gets through the guard. The
    # bad-header file makes the handler bail out before any DB access, so a 200
    # here means "authorized", not "imported".
    _use_config(_config(student={Capability.VIEW, Capability.EVENTS_IMPORT}))
    _as("student")
    assert getattr(client, method)(path, **kwargs).status_code == 200


@pytest.mark.parametrize("method,path,kwargs", _IMPORT_ROUTES)
def test_import_routes_403_for_default_student(client, method, path, kwargs):
    _as("student")
    assert getattr(client, method)(path, **kwargs).status_code == 403


@pytest.mark.parametrize("method,path,kwargs", _IMPORT_ROUTES)
def test_import_routes_allowed_for_engineer_with_empty_config(
    client, method, path, kwargs
):
    _use_config({})
    _as("engineer")
    assert getattr(client, method)(path, **kwargs).status_code == 200


# --- the two capabilities are genuinely independent ---------------------------


def test_events_create_does_not_grant_bulk_upload(client):
    _use_config(_config(student={Capability.VIEW, Capability.EVENTS_CREATE}))
    _as("student")
    assert client.get("/events/import/template").status_code == 403


def test_events_import_does_not_grant_single_create(client):
    _use_config(_config(student={Capability.VIEW, Capability.EVENTS_IMPORT}))
    _as("student")
    assert client.post("/events", json=_VALID_EVENT).status_code == 403


# --- the split did NOT widen the other event routes ---------------------------


@pytest.mark.parametrize(
    "method,path,kwargs",
    [
        ("patch", "/events/7", {"json": {"event_name": "Renamed"}}),
        ("delete", "/events/7", {}),
        ("post", "/events/7/attendees", {"json": {"alumni_id": 42}}),
        ("delete", "/events/7/attendees/42", {}),
        ("get", "/events/7/attendees/export", {}),
    ],
)
def test_edit_and_roster_routes_still_require_alumni_full(
    client, method, path, kwargs
):
    # Holding BOTH authoring capabilities must not unlock editing, deleting, or
    # the attendee roster. #378 scoped the new toggles to authoring (create +
    # bulk upload) only; #379 moved the rest onto events.manage / alumni.export,
    # which this role deliberately does not hold.
    _use_config(
        _config(
            student={
                Capability.VIEW,
                Capability.EVENTS_CREATE,
                Capability.EVENTS_IMPORT,
            }
        )
    )
    _as("student")
    assert getattr(client, method)(path, **kwargs).status_code == 403
