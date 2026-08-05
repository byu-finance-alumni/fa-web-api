"""Enforcement + preservation tests for the #379 capability split.

#379 did two things:

  * pulled logging an interaction out of the "Edit alumni" description into its
    own ``interactions.create`` capability, seeded to EVERY role;
  * dissolved the blanket ``alumni.full`` ("Manage alumni & data") capability
    into twelve per-section codes, so an engineer can delegate one of them
    without delegating all of them.

The tests here answer the only two questions that matter about that:

  1. **Is each new capability actually ENFORCED?** For every code, a role that
     holds everything EXCEPT it must be refused, and a role that holds nothing
     but it (plus ``view``) must get past the guard. A capability the permission
     editor advertises but no endpoint checks is worse than no capability at all.
  2. **Did anybody's access change?** ``test_no_role_gained_or_lost_access`` pins
     each role's effective set against the pre-#379 model, computed from the old
     grants rather than restated by hand — so the check cannot drift into
     agreeing with a mistake.

Offline: auth, permission-config, and session dependencies are overridden, so no
DATABASE_URL is required.
"""

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
    Capability,
    effective_capabilities,
    expand_legacy_grants,
)
from app.core.database import get_session
from app.core.roles import ROLE_ORDER, RoleName
from app.main import app
from app.schemas.auth import UserContext

MIGRATION = (
    Path(__file__).resolve().parents[1]
    / "database"
    / "migrations"
    / "2026-08-04_permission_capability_split.sql"
)


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
    # raise_server_exceptions=False keeps these tests about AUTHORIZATION: there
    # is no database behind the overridden session, so a request that clears the
    # guard usually blows up in the handler. We want that surfaced as a 500
    # response (i.e. "not 403") rather than propagated out of the client.
    with TestClient(app, raise_server_exceptions=False) as test_client:
        yield test_client
    app.dependency_overrides.clear()
    rate_limit.reset()


def _use_config(**grants: set) -> None:
    """Install DEFAULT_GRANTS with the named roles' capability sets replaced."""
    config = dict(DEFAULT_GRANTS)
    for role, caps in grants.items():
        config[role] = frozenset(caps)
    app.dependency_overrides[get_permission_config] = lambda: config


def _as(role: str) -> None:
    app.dependency_overrides[get_current_db_user] = lambda: _ctx(role)


# --- the probe table ----------------------------------------------------------
#
# One representative route per new capability. Each probe is chosen so the GUARD
# is the only thing that can produce a 403: the payloads are intentionally
# minimal, so a request that gets past the guard fails later (422/404/500) — a
# distinguishable outcome. Hence the two assertions are "== 403" (revoked) and
# "!= 403" (granted), never "== 200", which would make the test about the
# handler rather than about authorization.

_PROBES: list[tuple[str, str, str, dict]] = [
    (Capability.INTERACTIONS_CREATE, "post", "/alumni/1/interactions", {"json": {}}),
    (Capability.ALUMNI_CREATE, "post", "/alumni", {"json": {}}),
    (Capability.ALUMNI_CREATE, "post", "/alumni/preview", {"json": {}}),
    # Creating "friend of the program" records from unmatched conference
    # attendees is an alumni-CREATE path, so #379 put it on alumni.create rather
    # than events.manage — the line is drawn by what gets written.
    (
        Capability.ALUMNI_CREATE,
        "post",
        "/events/7/attendees/match/friends",
        {"json": {"rows": []}},
    ),
    (Capability.ALUMNI_ARCHIVE, "delete", "/alumni/1", {}),
    (Capability.ALUMNI_ARCHIVE, "post", "/alumni/1/restore", {}),
    (Capability.ALUMNI_IMPORT, "get", "/alumni/import/template", {}),
    (Capability.ALUMNI_IMPORT, "post", "/alumni/import", {}),
    (Capability.ALUMNI_IMPORT, "post", "/alumni/import/update", {}),
    (Capability.ALUMNI_EXPORT, "get", "/alumni/export/columns", {}),
    (Capability.ALUMNI_EXPORT, "post", "/alumni/export", {"json": {}}),
    (Capability.ALUMNI_EXPORT, "get", "/alumni/1/export", {}),
    (Capability.ALUMNI_EXPORT, "get", "/alumni/import/update/export", {}),
    # An event's attendee roster is a file of people, so it sits with the other
    # exports rather than with event management (#379).
    (Capability.ALUMNI_EXPORT, "get", "/events/7/attendees/export", {}),
    (Capability.ALUMNI_PHOTOS, "delete", "/alumni/1/headshot", {}),
    (Capability.ALUMNI_PHOTOS, "post", "/alumni/1/headshot/upload-url", {}),
    (
        Capability.ALUMNI_PHOTOS,
        "post",
        "/alumni/headshots/bulk/upload-urls",
        {"json": {"filenames": []}},
    ),
    (Capability.EVENTS_MANAGE, "patch", "/events/7", {"json": {}}),
    (Capability.EVENTS_MANAGE, "delete", "/events/7", {}),
    (Capability.EVENTS_MANAGE, "post", "/events/7/attendees", {"json": {}}),
    (Capability.EVENTS_MANAGE, "delete", "/events/7/attendees/42", {}),
    (Capability.EVENTS_MANAGE, "get", "/events/attendees/match/template", {}),
    (Capability.NOTES_MANAGE, "post", "/notes", {"json": {}}),
    (Capability.NOTES_MANAGE, "patch", "/notes/1", {"json": {}}),
    (Capability.NOTES_MANAGE, "delete", "/notes/1", {}),
    (Capability.SURVEYS_MANAGE, "get", "/survey/campaigns/2020/responses", {}),
    (Capability.SURVEYS_MANAGE, "post", "/survey/responses/1/apply", {}),
    (Capability.SURVEYS_MANAGE, "get", "/survey/schedules", {}),
    (Capability.SURVEYS_MANAGE, "post", "/survey/campaigns/2020/send", {}),
    (Capability.DONATIONS_VIEW, "get", "/donations/donors", {}),
    (Capability.DONATIONS_VIEW, "get", "/donations/summary", {}),
    (Capability.DONATIONS_VIEW, "get", "/donations/alumni/1", {}),
    (Capability.REPORTS_ADVANCED, "get", "/dashboard/activity", {}),
    (Capability.REPORTS_ADVANCED, "get", "/dashboard/data-quality", {}),
    (Capability.REPORTS_ADVANCED, "get", "/dashboard/follow-ups", {}),
    (Capability.REPORTS_ADVANCED, "get", "/dashboard/contacted-this-month", {}),
    (Capability.REPORTS_ADVANCED, "get", "/tasks", {}),
    (Capability.REPORTS_ADVANCED, "get", "/geography/states/Utah/alumni", {}),
    (Capability.REPORTS_ADVANCED, "get", "/geography/countries/Brazil/alumni", {}),
    (Capability.REPORTS_ADVANCED, "get", "/geography/radius?near=Provo&radius=25", {}),
    (Capability.REPORTS_ADVANCED, "get", "/geography/cities?city=Provo&state=Utah", {}),
]

_PROBE_IDS = [f"{cap}:{method}:{path}" for cap, method, path, _ in _PROBES]

# Every capability #379 introduced must appear in the probe table. Without this,
# adding a capability and forgetting to gate a route would pass silently.
_NEW_CODES = {Capability.INTERACTIONS_CREATE, *ALUMNI_FULL_REPLACEMENTS} - {
    # events.create / events.import arrived with #378 and have their own
    # enforcement suite in tests/test_event_capabilities.py.
    Capability.EVENTS_CREATE,
    Capability.EVENTS_IMPORT,
}


def test_every_new_capability_has_at_least_one_enforced_route():
    assert {cap for cap, *_ in _PROBES} == _NEW_CODES


def test_every_new_capability_is_in_the_editor_registry():
    # If it isn't in CAPABILITIES it never reaches the permission editor, and a
    # guard nobody can grant is a lockout, not a permission.
    for code in _NEW_CODES:
        assert code in CAPABILITIES_BY_CODE
        assert CAPABILITIES_BY_CODE[code].assignable is True


@pytest.mark.parametrize("cap,method,path,kwargs", _PROBES, ids=_PROBE_IDS)
def test_route_403s_when_the_role_lacks_the_capability(
    client, cap, method, path, kwargs
):
    # super_admin keeps EVERYTHING except the one capability under test, so a 403
    # can only have come from that capability's guard.
    _use_config(super_admin=DEFAULT_GRANTS["super_admin"] - {cap})
    _as("super_admin")
    response = getattr(client, method)(path, **kwargs)
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "forbidden"


@pytest.mark.parametrize("cap,method,path,kwargs", _PROBES, ids=_PROBE_IDS)
def test_route_is_reachable_when_the_role_is_granted_the_capability(
    client, cap, method, path, kwargs
):
    # A role holding nothing but `view` plus the capability under test gets past
    # the guard — which is the whole point of splitting it out. Asserting "not
    # 403" rather than "200" keeps this a test of authorization: with no
    # database behind it, most of these fail later, and that is fine.
    _use_config(view_only={Capability.VIEW, cap})
    _as("view_only")
    response = getattr(client, method)(path, **kwargs)
    assert response.status_code != 403


@pytest.mark.parametrize("cap,method,path,kwargs", _PROBES, ids=_PROBE_IDS)
def test_engineer_reaches_every_route_even_with_an_empty_config(
    client, cap, method, path, kwargs
):
    # The engineer hard-override has to survive the split: an empty or corrupt
    # role_capabilities table must never lock the engineer out.
    app.dependency_overrides[get_permission_config] = lambda: {}
    _as("engineer")
    assert getattr(client, method)(path, **kwargs).status_code != 403


# --- interactions are open to everyone, and only interactions --------------


@pytest.mark.parametrize("role", [r.value for r in ROLE_ORDER])
def test_every_role_can_log_an_interaction(client, role):
    _as(role)
    response = client.post("/alumni/1/interactions", json={})
    assert response.status_code != 403


def test_logging_an_interaction_does_not_require_edit_access(client):
    # The professor case #379 is really about: interactions.create with NO
    # alumni.edit still reaches the interaction route...
    _use_config(
        view_only={Capability.VIEW, Capability.INTERACTIONS_CREATE}
    )
    _as("view_only")
    assert client.post("/alumni/1/interactions", json={}).status_code != 403


@pytest.mark.parametrize(
    "method,path,kwargs",
    [
        ("patch", "/alumni/1", {"json": {}}),
        ("post", "/alumni/1/employment", {"json": {}}),
        ("post", "/alumni/1/education", {"json": {}}),
        ("post", "/alumni/1/tags", {"json": {}}),
        ("post", "/alumni/1/tasks", {"json": {}}),
        ("post", "/alumni/1/events", {"json": {}}),
    ],
)
def test_interactions_capability_unlocks_nothing_else(client, method, path, kwargs):
    # ...and NOTHING else. Widening interactions to every role must not hand a
    # professor any other write on the record.
    _use_config(
        view_only={Capability.VIEW, Capability.INTERACTIONS_CREATE}
    )
    _as("view_only")
    assert getattr(client, method)(path, **kwargs).status_code == 403


def test_interactions_capability_is_revokable(client):
    # It is seeded to every role, not hardwired: an engineer who takes it away
    # from a role must actually take it away.
    _use_config(student=DEFAULT_GRANTS["student"] - {Capability.INTERACTIONS_CREATE})
    _as("student")
    assert client.post("/alumni/1/interactions", json={}).status_code == 403


# --- nobody gained or lost access --------------------------------------------


# The permission model as it stood BEFORE #379, restated exactly. Each role's
# pre-split capability set; `alumni.full` is the blanket code that was dissolved.
_PRE_SPLIT_GRANTS: dict[str, set[str]] = {
    RoleName.SUPER_ADMIN.value: {
        "view",
        "alumni.edit",
        "alumni.full",
        "events.create",
        "events.import",
        "user_admin",
        "donations.manage",
        "profile.completeness",
    },
    RoleName.FULL_ACCESS.value: {
        "view",
        "alumni.edit",
        "alumni.full",
        "events.create",
        "events.import",
    },
    RoleName.STUDENT.value: {"view", "alumni.edit"},
    RoleName.VIEW_ONLY.value: {"view"},
}


def test_no_role_gained_or_lost_access():
    """Every role's post-split capabilities == its pre-split capabilities, with
    ``alumni.full`` translated into the twelve codes that replaced it and
    ``interactions.create`` added (the one deliberate widening, and a no-op in
    practice — the interaction routes were already open to every view holder)."""
    for role, before in _PRE_SPLIT_GRANTS.items():
        expected = set(before)
        if "alumni.full" in expected:
            expected.discard("alumni.full")
            expected |= ALUMNI_FULL_REPLACEMENTS
        expected.add(Capability.INTERACTIONS_CREATE)
        assert effective_capabilities(DEFAULT_GRANTS, [role]) == expected, role


def test_the_split_is_a_partition_not_a_reshuffle():
    # Sanity on the translation itself: the replacements are all real, currently
    # registered, assignable capabilities — not a set that quietly includes the
    # retired code or an admin-tier one.
    assert Capability.LEGACY_ALUMNI_FULL not in ALUMNI_FULL_REPLACEMENTS
    assert Capability.USER_ADMIN not in ALUMNI_FULL_REPLACEMENTS
    assert Capability.DONATIONS_MANAGE not in ALUMNI_FULL_REPLACEMENTS
    assert Capability.VOCAB_ADMIN not in ALUMNI_FULL_REPLACEMENTS
    for code in ALUMNI_FULL_REPLACEMENTS:
        assert CAPABILITIES_BY_CODE[code].assignable is True


def test_the_retired_capability_is_no_longer_grantable():
    # It must not appear in the editor registry, or an engineer could re-grant a
    # code no route checks and be misled about what it does.
    assert Capability.LEGACY_ALUMNI_FULL not in CAPABILITIES_BY_CODE


def test_lower_roles_did_not_widen():
    student = effective_capabilities(DEFAULT_GRANTS, ["student"])
    view_only = effective_capabilities(DEFAULT_GRANTS, ["view_only"])
    # Neither picked up anything from the dissolved capability.
    assert not student & ALUMNI_FULL_REPLACEMENTS
    assert not view_only & ALUMNI_FULL_REPLACEMENTS
    assert Capability.ALUMNI_EDIT not in view_only


# --- the deploy-ordering safety net -------------------------------------------


def test_unmigrated_alumni_full_rows_still_resolve_the_new_codes():
    """The ordering trap: `load_grants` only falls back to DEFAULT_GRANTS on an
    EMPTY table, so a database whose rows predate the seed migration would deny
    all twelve replacements. `expand_legacy_grants` reads such a row set as
    holding them, keeping the deploy/migration gap invisible."""
    stale = frozenset({"view", "alumni.edit", "alumni.full"})
    resolved = expand_legacy_grants(stale)
    assert ALUMNI_FULL_REPLACEMENTS <= resolved


def test_the_legacy_expansion_stops_once_the_migration_has_run():
    """...and it must stop firing afterwards, or a revoke in the permission
    editor would be silently undone by the stale row."""
    migrated = frozenset(
        {"view", "alumni.edit", "alumni.full", *ALUMNI_FULL_REPLACEMENTS}
    ) - {Capability.ALUMNI_IMPORT}
    assert Capability.ALUMNI_IMPORT not in expand_legacy_grants(migrated)


def test_a_role_without_the_legacy_code_gains_nothing():
    assert expand_legacy_grants(frozenset({"view"})) == {"view"}


def test_revoking_through_the_config_beats_the_legacy_expansion(client):
    # End-to-end version of the above, through a real route.
    _use_config(
        super_admin=(DEFAULT_GRANTS["super_admin"] | {"alumni.full"})
        - {Capability.ALUMNI_IMPORT}
    )
    _as("super_admin")
    assert client.get("/alumni/import/template").status_code == 403


# --- the seed migration agrees with the in-code defaults ----------------------


def _migration_sql() -> str:
    return MIGRATION.read_text(encoding="utf-8")


def test_the_migration_seeds_exactly_the_replacements():
    """A brand-new database is seeded from DEFAULT_GRANTS (the empty-table
    fallback) while an existing one is seeded by the migration. If the two
    disagree, dev and prod drift apart silently — so pin that the migration
    grants precisely the twelve replacement codes off `alumni.full`."""
    sql = _migration_sql()
    derived_block = sql.split("WHERE rc.capability_code = 'alumni.full'")[0]
    seeded = set(re.findall(r"\('([a-z_]+\.[a-z_]+)'\)", derived_block))
    assert seeded == set(ALUMNI_FULL_REPLACEMENTS)


def test_the_migration_seeds_interactions_create_for_every_role():
    sql = _migration_sql()
    assert "SELECT r.role_id, 'interactions.create'\nFROM roles r" in sql


def test_the_migration_cannot_break_an_unseeded_database():
    """The trap this project has already hit: making `role_capabilities`
    non-empty switches OFF the DEFAULT_GRANTS fallback. Every statement that
    inserts without deriving from an existing row must therefore be guarded by
    an EXISTS check, so a fresh database stays empty and keeps the fallback."""
    sql = _migration_sql()
    unguarded = [
        stmt
        for stmt in sql.split("INSERT INTO role_capabilities")[1:]
        if "FROM role_capabilities rc" not in stmt
        and "EXISTS (SELECT 1 FROM role_capabilities)" not in stmt
    ]
    assert unguarded == []


def test_the_migration_never_revokes():
    # Comment lines stripped first — the header prose legitimately talks about
    # dropping and updating rows.
    sql = " ".join(
        line
        for line in _migration_sql().upper().splitlines()
        if not line.strip().startswith("--")
    )
    assert "DELETE" not in sql
    assert "DROP" not in sql
    assert "UPDATE" not in sql
    # Three INSERTs, each conflict-tolerant, so a re-run is a no-op.
    assert sql.count("INSERT INTO ROLE_CAPABILITIES") == 3
    assert sql.count("ON CONFLICT ON CONSTRAINT UQ_ROLE_CAPABILITIES DO NOTHING") == 3
