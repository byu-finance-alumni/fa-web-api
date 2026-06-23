"""Tests for the editable controlled-vocabulary feature (#82).

Two layers, both offline (no database):
- Route authorization: the /admin/vocabulary CRUD is engineer-only (the
  controlled-vocabulary admin role); every lesser role, INCLUDING super_admin,
  gets 403, unknown categories 422.
- Service logic: create/update/deactivate semantics (duplicate -> 409,
  reactivate-inactive, rename-collision -> 409, soft delete) driven against a
  tiny fake session.
"""

import asyncio
import uuid

import pytest
from fastapi.testclient import TestClient

from app.api.dependencies.auth import get_current_db_user
from app.core.database import get_session
from app.core.errors import ConflictError, NotFoundError
from app.core.vocabularies import VocabularyCategory
from app.main import app
from app.models.vocabulary import VocabularyTerm
from app.schemas.auth import UserContext
from app.services import vocabulary as svc


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
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


# --- route authorization ------------------------------------------------------


# Vocab admin is engineer-only: super_admin is now forbidden alongside the
# lesser roles (defense-in-depth match to the engineer-only frontend gate).
@pytest.mark.parametrize(
    "role", ["view_only", "student", "full_access", "super_admin"]
)
def test_create_vocab_forbidden_below_engineer(client, role):
    app.dependency_overrides[get_current_db_user] = lambda: _ctx(role)
    resp = client.post(
        "/admin/vocabulary", json={"category": "event_type", "value": "Gala"}
    )
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "forbidden"


@pytest.mark.parametrize(
    "role", ["view_only", "student", "full_access", "super_admin"]
)
def test_list_vocab_admin_forbidden_below_engineer(client, role):
    app.dependency_overrides[get_current_db_user] = lambda: _ctx(role)
    resp = client.get("/admin/vocabulary/event_type")
    assert resp.status_code == 403


@pytest.mark.parametrize(
    "role", ["view_only", "student", "full_access", "super_admin"]
)
def test_delete_vocab_forbidden_below_engineer(client, role):
    app.dependency_overrides[get_current_db_user] = lambda: _ctx(role)
    assert client.delete("/admin/vocabulary/5").status_code == 403


def test_engineer_may_list_vocab_admin(client, monkeypatch):
    # The engineer (controlled-vocabulary admin) passes the guard: the request
    # reaches the handler (200), proving the route is engineer-allowed and not
    # blocked at authorization. The service is patched to return no terms, so the
    # body is an empty list.
    app.dependency_overrides[get_current_db_user] = lambda: _ctx("engineer")

    async def _empty(*_args, **_kwargs):
        return []

    monkeypatch.setattr(svc, "list_terms", _empty)
    resp = client.get("/admin/vocabulary/event_type")
    assert resp.status_code == 200
    assert resp.json() == []


def test_public_vocab_unknown_category_is_422(client):
    # Path param validated against VocabularyCategory before any DB access.
    app.dependency_overrides[get_current_db_user] = lambda: _ctx("view_only")
    assert client.get("/vocabulary/wizard").status_code == 422


def test_public_vocab_requires_auth(client):
    assert client.get("/vocabulary/event_type").status_code == 401


# --- service logic ------------------------------------------------------------


class _ScalarsResult:
    def __init__(self, items):
        self._items = items

    def all(self):
        return self._items


class _Sess:
    """Fake session: returns preset values for scalar/scalars/get; records
    add/flush."""

    def __init__(self, *, scalar=None, get=None, scalars=None):
        self._scalar = scalar
        self._get = get
        self._scalars = scalars or []
        self.added: list = []
        self.flushed = 0

    async def scalar(self, _stmt):
        return self._scalar

    async def scalars(self, _stmt):
        return _ScalarsResult(self._scalars)

    async def get(self, _model, _id):
        return self._get

    def add(self, obj):
        self.added.append(obj)

    async def flush(self):
        self.flushed += 1


def _term(value="Networking", *, active=True, category="event_type", term_id=1):
    t = VocabularyTerm(
        category=category, value=value, sort_order=0, active=active
    )
    t.term_id = term_id
    return t


def test_create_term_new():
    session = _Sess(scalar=None)
    term, reactivated = asyncio.run(
        svc.create_term(session, VocabularyCategory.EVENT_TYPE, "Gala", 3)
    )
    assert reactivated is False
    assert term.value == "Gala" and term.active is True
    assert term in session.added and session.flushed == 1


def test_create_term_active_duplicate_conflicts():
    session = _Sess(scalar=_term("Gala", active=True))
    with pytest.raises(ConflictError):
        asyncio.run(
            svc.create_term(session, VocabularyCategory.EVENT_TYPE, "Gala")
        )


def test_create_term_case_insensitive_duplicate_conflicts():
    # "networking" collides with an existing active "Networking" — the
    # case-insensitive lookup is what the service relies on (the fake returns
    # the existing term regardless of case), so a near-duplicate is a 409.
    session = _Sess(scalar=_term("Networking", active=True))
    with pytest.raises(ConflictError):
        asyncio.run(
            svc.create_term(session, VocabularyCategory.EVENT_TYPE, "networking")
        )


def test_list_active_values_returns_ordered_strings():
    session = _Sess(
        scalars=[_term("Networking", term_id=1), _term("Social", term_id=2)]
    )
    values = asyncio.run(
        svc.list_active_values(session, VocabularyCategory.EVENT_TYPE)
    )
    assert values == ["Networking", "Social"]


def test_create_term_reactivates_inactive_duplicate():
    inactive = _term("Gala", active=False)
    session = _Sess(scalar=inactive)
    term, reactivated = asyncio.run(
        svc.create_term(session, VocabularyCategory.EVENT_TYPE, "Gala", 7)
    )
    assert reactivated is True
    assert term is inactive and term.active is True and term.sort_order == 7


def test_update_term_rename_collision_conflicts():
    target = _term("Networking", term_id=1)
    clash = _term("Social", term_id=2)
    session = _Sess(get=target, scalar=clash)
    with pytest.raises(ConflictError):
        asyncio.run(svc.update_term(session, 1, value="Social"))


def test_update_term_rename_ok():
    target = _term("Networking", term_id=1)
    session = _Sess(get=target, scalar=None)
    term = asyncio.run(svc.update_term(session, 1, value="Networking Mixer"))
    assert term.value == "Networking Mixer"


def test_update_term_missing_is_404():
    session = _Sess(get=None)
    with pytest.raises(NotFoundError):
        asyncio.run(svc.update_term(session, 999, active=False))


def test_deactivate_term_soft_deletes():
    target = _term("Gala", active=True, term_id=4)
    session = _Sess(get=target)
    term = asyncio.run(svc.deactivate_term(session, 4))
    assert term.active is False
