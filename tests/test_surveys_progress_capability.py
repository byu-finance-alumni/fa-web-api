"""The `surveys.progress` capability and the student export grant (#605).

"View survey progress" is the survey Progress tab as a READ: the schedule list
that draws it and the "who replied" hover. The tests that matter prove the line
is where it says it is:

  * the read routes admit `surveys.progress` on its own — and `surveys.manage`
    on its own, because managing a campaign includes watching it (and the
    console cannot load without the list);
  * `surveys.progress` unlocks NOTHING that sends, schedules, pauses, cancels,
    reviews, or lists alumni contact details — those stay on `surveys.manage`;
  * students get the view (and every export, Jake 2026-10-13); view_only gets
    neither;
  * the seed migration matches the in-code defaults and cannot break an
    unseeded database.

Offline: auth, permission config and the session are overridden, so no
DATABASE_URL is required.
"""

from __future__ import annotations

import re
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.api.dependencies.auth import get_current_db_user, get_permission_config
from app.core import rate_limit
from app.core.capabilities import (
    ALUMNI_FULL_REPLACEMENTS,
    CAPABILITIES_BY_CODE,
    DEFAULT_GRANTS,
    POST_SPLIT_CAPABILITIES,
    POST_SPLIT_ROLE_WIDENINGS,
    Capability,
    effective_capabilities,
)
from app.core.database import get_session
from app.main import app
from app.schemas.auth import UserContext

MIGRATION = (
    Path(__file__).resolve().parents[1]
    / "database"
    / "migrations"
    / "2026-10-13_surveys_progress_and_student_export.sql"
)

# The read-only progress routes (#605).
_PROGRESS_ROUTES = [
    ("get", "/survey/schedules", {}),
    ("get", "/survey/schedules/2020/responders", {}),
]
_PROGRESS_IDS = [p for _, p, _ in _PROGRESS_ROUTES]

# A representative of every kind of campaign power that must stay on
# `surveys.manage`: schedule, start a cycle, send, pause, resume, cancel, review
# responses, the message editor, and the contact-detail call sheet.
_MANAGE_ROUTES = [
    ("post", "/survey/schedules", {"json": {}}),
    ("post", "/survey/schedules/bulk", {"json": {}}),
    ("get", "/survey/schedules/2020/new-cycle/preview", {}),
    ("post", "/survey/schedules/2020/new-cycle", {"json": {}}),
    ("post", "/survey/campaigns/2020/send", {}),
    ("post", "/survey/schedules/2020/pause", {}),
    ("post", "/survey/schedules/2020/resume", {}),
    ("post", "/survey/schedules/2020/cancel", {}),
    ("get", "/survey/campaigns/2020/responses", {}),
    ("post", "/survey/responses/1/apply", {}),
    ("post", "/survey/responses/1/reject", {}),
    ("put", "/survey/message", {"json": {}}),
    ("post", "/survey/send-config", {"json": {}}),
    ("get", "/survey/schedules/2020/non-responders", {}),
]
_MANAGE_IDS = [f"{m}:{p}" for m, p, _ in _MANAGE_ROUTES]


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
    rate_limit.reset()
    # Not raising server errors keeps these about AUTHORIZATION: past the guard
    # there is no database, so a handler fails — "not 403" is what is proved.
    with TestClient(app, raise_server_exceptions=False) as test_client:
        yield test_client
    app.dependency_overrides.clear()
    rate_limit.reset()


def _as(role: str) -> None:
    app.dependency_overrides[get_current_db_user] = lambda: _ctx(role)


def _use_config(**grants: set) -> None:
    config = dict(DEFAULT_GRANTS)
    for role, caps in grants.items():
        config[role] = frozenset(caps)
    app.dependency_overrides[get_permission_config] = lambda: config


# --- the capability itself ----------------------------------------------------


def test_the_capability_is_registered_and_assignable():
    spec = CAPABILITIES_BY_CODE[Capability.SURVEYS_PROGRESS]
    assert spec.code == "surveys.progress"
    assert spec.label == "View survey progress"
    assert spec.assignable is True
    text = spec.description.lower()
    # Written for an administrator: says it is read-only and what it is not.
    assert "read-only" in text
    assert "sending" in text and "pausing" in text and "cancelling" in text
    assert "/survey" not in spec.description


def test_it_is_a_declared_post_split_addition():
    assert Capability.SURVEYS_PROGRESS in POST_SPLIT_CAPABILITIES
    assert Capability.SURVEYS_PROGRESS not in ALUMNI_FULL_REPLACEMENTS


@pytest.mark.parametrize("role", ["super_admin", "full_access", "student", "engineer"])
def test_every_manager_and_student_holds_it_by_default(role):
    assert Capability.SURVEYS_PROGRESS in effective_capabilities(DEFAULT_GRANTS, [role])


def test_every_role_with_surveys_manage_holds_it_by_default():
    for role, caps in DEFAULT_GRANTS.items():
        if Capability.SURVEYS_MANAGE in caps:
            assert Capability.SURVEYS_PROGRESS in caps, role


def test_view_only_does_not_hold_it():
    assert Capability.SURVEYS_PROGRESS not in effective_capabilities(DEFAULT_GRANTS, ["view_only"])


def test_students_may_export_and_it_is_declared():
    caps = effective_capabilities(DEFAULT_GRANTS, ["student"])
    assert Capability.ALUMNI_EXPORT in caps
    assert Capability.ALUMNI_EXPORT in POST_SPLIT_ROLE_WIDENINGS["student"]
    # ...and that is the only #379 code a student gained.
    assert caps & ALUMNI_FULL_REPLACEMENTS == {Capability.ALUMNI_EXPORT}


def test_students_still_cannot_manage_surveys():
    assert Capability.SURVEYS_MANAGE not in effective_capabilities(DEFAULT_GRANTS, ["student"])


# --- the read routes ----------------------------------------------------------


@pytest.mark.parametrize("method,path,kwargs", _PROGRESS_ROUTES, ids=_PROGRESS_IDS)
def test_students_reach_the_progress_routes(client, method, path, kwargs):
    _use_config()
    _as("student")
    assert getattr(client, method)(path, **kwargs).status_code != 403


@pytest.mark.parametrize("method,path,kwargs", _PROGRESS_ROUTES, ids=_PROGRESS_IDS)
def test_view_only_is_refused_the_progress_routes(client, method, path, kwargs):
    _use_config()
    _as("view_only")
    response = getattr(client, method)(path, **kwargs)
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "forbidden"


@pytest.mark.parametrize("method,path,kwargs", _PROGRESS_ROUTES, ids=_PROGRESS_IDS)
def test_the_capability_on_its_own_is_enough(client, method, path, kwargs):
    _use_config(view_only={Capability.VIEW, Capability.SURVEYS_PROGRESS})
    _as("view_only")
    assert getattr(client, method)(path, **kwargs).status_code != 403


@pytest.mark.parametrize("method,path,kwargs", _PROGRESS_ROUTES, ids=_PROGRESS_IDS)
def test_surveys_manage_alone_still_sees_progress(client, method, path, kwargs):
    """Managing includes watching: a manager whose `surveys.progress` row is
    missing (revoked, or the seed migration not run yet) keeps the console."""
    _use_config(full_access=DEFAULT_GRANTS["full_access"] - {Capability.SURVEYS_PROGRESS})
    _as("full_access")
    assert getattr(client, method)(path, **kwargs).status_code != 403


@pytest.mark.parametrize("method,path,kwargs", _PROGRESS_ROUTES, ids=_PROGRESS_IDS)
def test_neither_capability_is_a_403(client, method, path, kwargs):
    _use_config(
        super_admin=DEFAULT_GRANTS["super_admin"]
        - {Capability.SURVEYS_PROGRESS, Capability.SURVEYS_MANAGE}
    )
    _as("super_admin")
    assert getattr(client, method)(path, **kwargs).status_code == 403


@pytest.mark.parametrize("method,path,kwargs", _PROGRESS_ROUTES, ids=_PROGRESS_IDS)
def test_the_engineer_gets_through_even_with_a_wiped_config(client, method, path, kwargs):
    app.dependency_overrides[get_permission_config] = lambda: {}
    _as("engineer")
    assert getattr(client, method)(path, **kwargs).status_code != 403


# --- nothing that changes a campaign -------------------------------------------


@pytest.mark.parametrize("method,path,kwargs", _MANAGE_ROUTES, ids=_MANAGE_IDS)
def test_students_are_still_refused_every_campaign_route(client, method, path, kwargs):
    _use_config()
    _as("student")
    assert getattr(client, method)(path, **kwargs).status_code == 403


@pytest.mark.parametrize("method,path,kwargs", _MANAGE_ROUTES, ids=_MANAGE_IDS)
def test_surveys_progress_unlocks_no_campaign_route(client, method, path, kwargs):
    # Even with export added, the read-only view is not a back door.
    _use_config(
        view_only={
            Capability.VIEW,
            Capability.SURVEYS_PROGRESS,
            Capability.ALUMNI_EXPORT,
        }
    )
    _as("view_only")
    assert getattr(client, method)(path, **kwargs).status_code == 403


# --- the seed migration -------------------------------------------------------


def _statements() -> str:
    """The SQL, upper-cased, with comment lines stripped."""
    return " ".join(
        line
        for line in MIGRATION.read_text(encoding="utf-8").upper().splitlines()
        if not line.strip().startswith("--")
    )


def test_the_migration_grants_progress_to_managers_student_and_engineer():
    sql = _statements()
    # Derived from whoever holds surveys.manage now, plus the named roles.
    assert "WHERE RC.CAPABILITY_CODE = 'SURVEYS.MANAGE'" in sql
    assert sql.count("'SURVEYS.PROGRESS'") == 3
    assert set(re.findall(r"ROLE_NAME = '([A-Z_]+)'", sql)) == {"STUDENT", "ENGINEER"}
    assert "VIEW_ONLY" not in sql


def test_the_migration_grants_export_to_student_only():
    sql = _statements()
    assert sql.count("'ALUMNI.EXPORT'") == 1
    export_stmt = next(
        s for s in sql.split("INSERT INTO ROLE_CAPABILITIES") if "'ALUMNI.EXPORT'" in s
    )
    assert "ROLE_NAME = 'STUDENT'" in export_stmt


def test_the_migration_never_revokes_and_is_idempotent():
    sql = _statements()
    assert "DELETE FROM" not in sql
    assert "DROP" not in sql
    assert "UPDATE " not in sql
    assert sql.count("INSERT INTO ROLE_CAPABILITIES") == 4
    assert sql.count("ON CONFLICT ON CONSTRAINT UQ_ROLE_CAPABILITIES DO NOTHING") == 4


def test_the_migration_cannot_break_an_unseeded_database():
    """Every INSERT is EXISTS-guarded, so a brand-new database stays empty and
    keeps the DEFAULT_GRANTS fallback (which already has both grants)."""
    sql = _statements()
    unguarded = [
        stmt
        for stmt in sql.split("INSERT INTO ROLE_CAPABILITIES")[1:]
        if "EXISTS (SELECT 1 FROM ROLE_CAPABILITIES)" not in stmt
    ]
    assert unguarded == []
