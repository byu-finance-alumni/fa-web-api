"""The `links.delete` capability and the bulk delete route (#441 follow-up).

Deleting an opportunity link was carved OUT of ``surveys.manage`` rather than
added alongside it. The distinction this suite exists to defend is
REVERSIBILITY: rejecting a link takes it out of circulation and can be undone,
deleting destroys the row and leaves only an audit snapshot. So the capability is
narrower than the one it came from, and the tests that matter are the ones that
prove the narrowing is real rather than decorative:

  * super_admin and the engineer CAN delete;
  * full_access CANNOT — even though it holds ``surveys.manage`` and can approve
    and reject all day. A capability the matrix advertises but that some other
    capability quietly implies is not a capability;
  * holding ``links.delete`` alone unlocks deletion and NOTHING else;
  * both delete routes — the single one and the bulk one — check the SAME rule,
    so a role refused the multi-select cannot loop the single endpoint instead;
  * bulk delete removes exactly the ids asked for, audits each row before it
    goes, reports the ids it could not find, and refuses an oversized list.

Offline: auth, permission config and the session are overridden, so no
DATABASE_URL is required.
"""

from __future__ import annotations

import asyncio
import datetime
import re
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.api.dependencies.auth import get_current_db_user, get_permission_config
from app.core.capabilities import (
    ALUMNI_FULL_REPLACEMENTS,
    CAPABILITIES_BY_CODE,
    DEFAULT_GRANTS,
    POST_SPLIT_CAPABILITIES,
    Capability,
    effective_capabilities,
)
from app.core.database import get_session
from app.main import app
from app.models.audit import AuditLog
from app.models.opportunity_link import OpportunityLink
from app.schemas.auth import UserContext
from app.schemas.opportunity_link import MAX_LINKS_PER_BULK_DELETE
from app.services import opportunity_links as service

MIGRATION = (
    Path(__file__).resolve().parents[1]
    / "database"
    / "migrations"
    / "2026-08-17_links_delete_capability.sql"
)

BULK_PATH = "/opportunity-links/bulk-delete"
GOOD_URL = "https://careers.acme-capital.example/jobs/analyst-2027"


def _run(coro):
    """House convention for this suite: a plain ``asyncio.run`` per test (there
    is no async plugin — see tests/test_opportunity_links.py)."""
    return asyncio.run(coro)


def _ctx(*roles: str) -> UserContext:
    return UserContext(
        user_id=9,
        auth_user_id=uuid.UUID("11111111-1111-1111-1111-111111111111"),
        roles=list(roles),
    )


def _link(link_id: int, **kw) -> OpportunityLink:
    base = dict(
        opportunity_link_id=link_id,
        alumni_id=100 + link_id,
        is_own_company=False,
        company_name=f"Acme {link_id}",
        url=f"{GOOD_URL}?id={link_id}",
        location_city="Provo",
        location_state="Utah",
        role_type="internship",
        application_deadline=datetime.date(2026, 11, 1),
        details="Summer analyst programme.",
        status="pending",
        source="survey",
        submitted_at=datetime.datetime(2026, 8, 17, 12, 0, tzinfo=datetime.UTC),
        updated_at=datetime.datetime(2026, 8, 17, 12, 0, tzinfo=datetime.UTC),
        created_by_user_id=None,
        reviewed_by_user_id=None,
        reviewed_at=None,
    )
    base.update(kw)
    return OpportunityLink(**base)


# --------------------------------------------------------------- fake session --


class _FakeResult:
    def __init__(self, rows):
        self._rows = list(rows)

    def scalars(self):
        return self

    def all(self):
        return list(self._rows)


class _BulkSession:
    """A no-DB session that answers the bulk delete's single ``IN`` query.

    ``execute`` honours the statement's WHERE clause the cheap way — by reading
    the bound ids out of the compiled parameters — so a test can assert that the
    service asked for exactly the ids it was given and that rows it did not ask
    for are never touched.
    """

    def __init__(self, rows: list[OpportunityLink]):
        self._rows = {r.opportunity_link_id: r for r in rows}
        self.added: list = []
        self.deleted: list[OpportunityLink] = []
        self.committed = 0
        self.queried_ids: list[list[int]] = []

    async def execute(self, stmt):
        # An `IN` clause compiles to a single EXPANDING bindparam, so the value
        # is one list rather than a scalar per id. Flatten both shapes so the
        # fake reads the ids the service actually asked for.
        wanted: list[int] = []
        for value in stmt.compile().params.values():
            if isinstance(value, (list, tuple)):
                wanted.extend(v for v in value if isinstance(v, int))
            elif isinstance(value, int):
                wanted.append(value)
        self.queried_ids.append(sorted(wanted))
        return _FakeResult(
            [self._rows[i] for i in sorted(wanted) if i in self._rows]
        )

    async def get(self, model, pk):
        if model is OpportunityLink:
            return self._rows.get(pk)
        return None

    def add(self, obj):
        self.added.append(obj)

    async def delete(self, obj):
        self.deleted.append(obj)

    async def flush(self):
        return None

    async def commit(self):
        self.committed += 1

    async def refresh(self, _obj):
        return None

    @property
    def audits(self) -> list[AuditLog]:
        return [o for o in self.added if isinstance(o, AuditLog)]


def _with_session(session):
    async def _override():
        yield session

    return _override


@pytest.fixture
def client():
    async def _no_db_session():
        yield None

    app.dependency_overrides[get_session] = _no_db_session
    with TestClient(app, raise_server_exceptions=False) as test_client:
        yield test_client
    app.dependency_overrides.clear()


def _as(role: str) -> None:
    app.dependency_overrides[get_current_db_user] = lambda: _ctx(role)


def _use_config(**grants: set) -> None:
    """DEFAULT_GRANTS with the named roles' capability sets replaced."""
    config = dict(DEFAULT_GRANTS)
    for role, caps in grants.items():
        config[role] = frozenset(caps)
    app.dependency_overrides[get_permission_config] = lambda: config


# =============================================================================
# 1. The capability exists and reaches the permission editor
# =============================================================================


def test_the_capability_is_registered_and_assignable():
    spec = CAPABILITIES_BY_CODE[Capability.LINKS_DELETE]
    # Not assignable would mean the engineer console renders it locked and the
    # toggle endpoint refuses it — i.e. the owner could never delegate it, which
    # is the opposite of "its own row in the permission editor".
    assert spec.assignable is True
    assert spec.code == "links.delete"
    assert spec.label
    assert spec.description


def test_the_capability_is_written_for_a_human_not_a_developer():
    """The description is what an administrator reads before ticking a box, so
    it has to say what the toggle does and how it differs from the neighbouring
    one — not name endpoints."""
    spec = CAPABILITIES_BY_CODE[Capability.LINKS_DELETE]
    text = spec.description.lower()
    assert "cannot be undone" in text
    assert "reject" in text  # the reversible alternative is named
    assert "/opportunity-links" not in spec.description
    assert "surveys.manage" not in spec.description


def test_it_is_not_a_re_drawing_of_the_379_split():
    # It is genuinely new, so it must not be smuggled into the set that describes
    # what `alumni.full` dissolved into...
    assert Capability.LINKS_DELETE not in ALUMNI_FULL_REPLACEMENTS
    # ...and it must be declared as a post-split addition, which is what keeps
    # the #379 preservation test honest instead of edited.
    assert Capability.LINKS_DELETE in POST_SPLIT_CAPABILITIES


# =============================================================================
# 2. Default grants: super_admin + engineer only
# =============================================================================


def test_super_admin_holds_it_by_default():
    assert Capability.LINKS_DELETE in effective_capabilities(
        DEFAULT_GRANTS, ["super_admin"]
    )


def test_the_engineer_holds_it_even_with_an_empty_config():
    # The engineer is IMPLICIT — the hard override in `effective_capabilities`
    # grants every registered code, so adding the code to the registry is the
    # whole of "grant it to the engineer".
    assert Capability.LINKS_DELETE in effective_capabilities({}, ["engineer"])


@pytest.mark.parametrize("role", ["full_access", "student", "view_only"])
def test_no_other_role_holds_it_by_default(role):
    assert Capability.LINKS_DELETE not in effective_capabilities(
        DEFAULT_GRANTS, [role]
    )


def test_full_access_keeps_surveys_manage():
    """The narrowing must take away deletion and NOTHING else — full_access is
    still the moderation role."""
    caps = effective_capabilities(DEFAULT_GRANTS, ["full_access"])
    assert Capability.SURVEYS_MANAGE in caps
    assert Capability.LINKS_DELETE not in caps


# =============================================================================
# 3. Route gating — both delete routes, one rule
# =============================================================================


_DELETE_ROUTES = [
    ("delete", "/opportunity-links/1", {}),
    ("post", BULK_PATH, {"json": {"opportunity_link_ids": [1]}}),
]
_DELETE_IDS = [f"{m}:{p}" for m, p, _ in _DELETE_ROUTES]


@pytest.mark.parametrize("method,path,kwargs", _DELETE_ROUTES, ids=_DELETE_IDS)
@pytest.mark.parametrize("role", ["full_access", "student", "view_only"])
def test_delete_routes_403_without_the_capability(
    client, method, path, kwargs, role
):
    _as(role)
    response = getattr(client, method)(path, **kwargs)
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "forbidden"


@pytest.mark.parametrize("method,path,kwargs", _DELETE_ROUTES, ids=_DELETE_IDS)
def test_surveys_manage_alone_does_not_unlock_deletion(
    client, method, path, kwargs
):
    """The crux. A role holding `surveys.manage` — and only that plus view — can
    approve and reject, and must still be refused both delete routes. If this
    ever passes, the split has silently collapsed back into one capability."""
    _use_config(student={Capability.VIEW, Capability.SURVEYS_MANAGE})
    _as("student")
    assert getattr(client, method)(path, **kwargs).status_code == 403


@pytest.mark.parametrize("method,path,kwargs", _DELETE_ROUTES, ids=_DELETE_IDS)
@pytest.mark.parametrize("role", ["super_admin", "engineer"])
def test_delete_routes_are_reachable_for_super_admin_and_engineer(
    client, method, path, kwargs, role
):
    """`!= 403`, not `== 200`: there is no database behind the overridden
    session, so this is a test about the guard, not about the handler."""
    _as(role)
    assert getattr(client, method)(path, **kwargs).status_code != 403


@pytest.mark.parametrize("method,path,kwargs", _DELETE_ROUTES, ids=_DELETE_IDS)
def test_the_engineer_gets_through_even_with_a_wiped_config(
    client, method, path, kwargs
):
    app.dependency_overrides[get_permission_config] = lambda: {}
    _as("engineer")
    assert getattr(client, method)(path, **kwargs).status_code != 403


@pytest.mark.parametrize("method,path,kwargs", _DELETE_ROUTES, ids=_DELETE_IDS)
def test_the_capability_on_its_own_is_enough(client, method, path, kwargs):
    # A role holding nothing but view + links.delete clears both guards —
    # otherwise the toggle would be decorative.
    _use_config(view_only={Capability.VIEW, Capability.LINKS_DELETE})
    _as("view_only")
    assert getattr(client, method)(path, **kwargs).status_code != 403


@pytest.mark.parametrize(
    "method,path,kwargs",
    [
        ("post", "/opportunity-links", {"json": {}}),
        ("patch", "/opportunity-links/1", {"json": {}}),
        ("post", "/opportunity-links/1/approve", {}),
        ("post", "/opportunity-links/1/reject", {}),
    ],
)
def test_links_delete_unlocks_nothing_else(client, method, path, kwargs):
    """The reverse direction: `links.delete` must not become a back door into
    creating, editing, approving or rejecting. Those stay on `surveys.manage`."""
    _use_config(view_only={Capability.VIEW, Capability.LINKS_DELETE})
    _as("view_only")
    assert getattr(client, method)(path, **kwargs).status_code == 403


def test_bulk_delete_requires_a_token(client):
    app.dependency_overrides.pop(get_current_db_user, None)
    response = client.post(BULK_PATH, json={"opportunity_link_ids": [1]})
    assert response.status_code == 401


# =============================================================================
# 4. The id-list cap and the rest of the request validation
# =============================================================================


@pytest.mark.parametrize(
    "ids",
    [
        [],  # nothing to do is a malformed request, not a no-op success
        list(range(1, MAX_LINKS_PER_BULK_DELETE + 2)),  # one over the cap
        [1, 0],  # ids are 1-based, mirroring IdPath's ge=1
        [1, -5],
    ],
)
def test_the_id_list_is_validated(client, ids):
    _as("super_admin")
    response = client.post(BULK_PATH, json={"opportunity_link_ids": ids})
    assert response.status_code == 422


def test_the_cap_is_the_boundary_not_an_approximation(client):
    """Exactly the cap is allowed; the test above pins that one more is not. The
    cap is what stops a single call being an unbounded row-destruction
    primitive, so it is worth pinning from both sides."""
    _as("super_admin")
    ids = list(range(1, MAX_LINKS_PER_BULK_DELETE + 1))
    session = _BulkSession([])
    app.dependency_overrides[get_session] = _with_session(session)
    response = client.post(BULK_PATH, json={"opportunity_link_ids": ids})
    assert response.status_code == 200
    assert response.json()["requested"] == MAX_LINKS_PER_BULK_DELETE


def test_unknown_body_keys_are_refused(client):
    _as("super_admin")
    response = client.post(
        BULK_PATH, json={"opportunity_link_ids": [1], "force": True}
    )
    assert response.status_code == 422


# =============================================================================
# 5. Bulk delete behaviour (service level)
# =============================================================================


def test_it_deletes_exactly_the_requested_rows():
    async def _body():
        rows = [_link(1), _link(2), _link(3)]
        session = _BulkSession(rows)
        deleted, missing = await service.delete_links(
            session, [1, 3], actor_user_id=9
        )
        assert deleted == [1, 3]
        assert missing == []
        # Row 2 was never asked for and must be untouched.
        assert [r.opportunity_link_id for r in session.deleted] == [1, 3]
        assert session.queried_ids == [[1, 3]]
        # One transaction for the whole batch, rows and audit rows together.
        assert session.committed == 1

    _run(_body())


def test_every_deleted_row_gets_its_own_audit_snapshot():
    async def _body():
        rows = [_link(1), _link(2)]
        session = _BulkSession(rows)
        await service.delete_links(session, [1, 2], actor_user_id=9)
        audits = session.audits
        assert len(audits) == 2
        for audit, row in zip(audits, rows, strict=True):
            assert audit.action_type == "delete_opportunity_link"
            # Audited against the OWNING alumnus, so it surfaces on that alum's
            # profile Audit tab — the same convention the single delete uses.
            assert audit.entity_type == "alumni"
            assert audit.entity_id == row.alumni_id
            assert audit.user_id == 9
            # The snapshot is what makes a deleted link reconstructible: the URL
            # is the whole substance of the row, and it is gone from the table.
            assert row.url in audit.old_value
            assert row.company_name in audit.old_value

    _run(_body())


def test_the_snapshot_matches_what_the_single_delete_writes():
    """A bulk delete must be indistinguishable from N single deletes in the
    trail, or a reviewer reading the audit log would have to know which button
    was pressed to interpret it."""

    async def _body():
        one = _BulkSession([_link(1)])
        await service.delete_link(one, 1, actor_user_id=9)
        many = _BulkSession([_link(1)])
        await service.delete_links(many, [1], actor_user_id=9)
        a, b = one.audits[0], many.audits[0]
        assert (a.action_type, a.entity_type, a.entity_id, a.old_value) == (
            b.action_type,
            b.entity_type,
            b.entity_id,
            b.old_value,
        )

    _run(_body())


def test_missing_ids_are_reported_not_raised():
    """Best-effort, deliberately. The commonest reason an id is stale is that
    somebody else already deleted it — i.e. the row is already in the state the
    caller asked for. Failing the batch over that would make the button less
    reliable the more rows you select."""

    async def _body():
        session = _BulkSession([_link(1), _link(3)])
        deleted, missing = await service.delete_links(
            session, [1, 2, 3, 99], actor_user_id=9
        )
        assert deleted == [1, 3]
        assert missing == [2, 99]
        # A missing id must not produce a phantom audit row.
        assert len(session.audits) == 2

    _run(_body())


def test_duplicate_ids_collapse():
    async def _body():
        session = _BulkSession([_link(1)])
        deleted, missing = await service.delete_links(
            session, [1, 1, 1], actor_user_id=9
        )
        assert deleted == [1]
        assert missing == []
        assert len(session.deleted) == 1
        assert len(session.audits) == 1

    _run(_body())


def test_a_wholly_stale_batch_is_a_success_that_deleted_nothing():
    async def _body():
        session = _BulkSession([])
        deleted, missing = await service.delete_links(
            session, [7, 8], actor_user_id=9
        )
        assert deleted == []
        assert missing == [7, 8]
        assert session.audits == []

    _run(_body())


# =============================================================================
# 6. The bulk route's response says exactly what happened
# =============================================================================


def test_the_response_accounts_for_every_requested_id(client):
    _as("super_admin")
    session = _BulkSession([_link(1), _link(3)])
    app.dependency_overrides[get_session] = _with_session(session)
    response = client.post(
        BULK_PATH, json={"opportunity_link_ids": [3, 1, 2]}
    )
    assert response.status_code == 200
    body = response.json()
    assert body == {
        "requested": 3,
        "deleted_ids": [1, 3],
        "missing_ids": [2],
    }
    # The contract the frontend can rely on to render "2 deleted, 1 already gone".
    assert len(body["deleted_ids"]) + len(body["missing_ids"]) == body["requested"]


def test_requested_counts_distinct_ids(client):
    _as("super_admin")
    session = _BulkSession([_link(1)])
    app.dependency_overrides[get_session] = _with_session(session)
    response = client.post(BULK_PATH, json={"opportunity_link_ids": [1, 1]})
    assert response.json() == {
        "requested": 1,
        "deleted_ids": [1],
        "missing_ids": [],
    }


def test_the_actor_on_the_audit_row_is_the_caller(client):
    _as("super_admin")
    session = _BulkSession([_link(1)])
    app.dependency_overrides[get_session] = _with_session(session)
    client.post(BULK_PATH, json={"opportunity_link_ids": [1]})
    assert [a.user_id for a in session.audits] == [9]


# =============================================================================
# 7. The seed migration agrees with the in-code defaults
# =============================================================================


def _migration_sql() -> str:
    return MIGRATION.read_text(encoding="utf-8")


def _statements() -> str:
    """The SQL with comment lines stripped — the header prose legitimately talks
    about revoking, dropping, and full_access."""
    return " ".join(
        line
        for line in _migration_sql().upper().splitlines()
        if not line.strip().startswith("--")
    )


def test_the_migration_seeds_only_super_admin_and_engineer():
    sql = _statements()
    roles = set(re.findall(r"ROLE_NAME = '([A-Z_]+)'", sql))
    assert roles == {"SUPER_ADMIN", "ENGINEER"}
    # Seeding full_access would hand deletion to exactly the role the split is
    # meant to exclude, and no test above would catch it — DEFAULT_GRANTS is only
    # the empty-table fallback, so on a real database the migration IS the model.
    assert "FULL_ACCESS" not in sql


def test_the_migration_seeds_the_same_code_the_registry_defines():
    assert _statements().count("'LINKS.DELETE'") == 2


def test_the_migration_never_revokes_and_is_idempotent():
    sql = _statements()
    assert "DELETE FROM" not in sql
    assert "DROP" not in sql
    assert "UPDATE " not in sql
    assert sql.count("INSERT INTO ROLE_CAPABILITIES") == 2
    assert sql.count("ON CONFLICT ON CONSTRAINT UQ_ROLE_CAPABILITIES DO NOTHING") == 2


def test_the_migration_cannot_break_an_unseeded_database():
    """The trap this project has already hit: making `role_capabilities`
    non-empty switches OFF the DEFAULT_GRANTS fallback, stripping every other
    capability from every role. Every INSERT here must be EXISTS-guarded so a
    brand-new database stays empty and keeps the fallback — which already grants
    links.delete to super_admin."""
    sql = _statements()
    unguarded = [
        stmt
        for stmt in sql.split("INSERT INTO ROLE_CAPABILITIES")[1:]
        if "EXISTS (SELECT 1 FROM ROLE_CAPABILITIES)" not in stmt
    ]
    assert unguarded == []
