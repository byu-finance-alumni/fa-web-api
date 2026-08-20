"""Deleting one login-abuse CAMPAIGN: DELETE /admin/login-campaigns/{ip} (#457
follow-up).

Proving the automatic block actually refuses people on production meant driving
real failed sign-ins at the real API, which left synthetic rows in three tables.
This endpoint clears them from the console instead of from a psql session
pointed at production.

WHAT THIS FILE PINS, and why each one is here rather than left to a reviewer:

  1. THE COUNTS ARE REAL. Every statement carries ``RETURNING`` and the response
     reports what Postgres removed. A destructive route that reports an assumed
     success is worse than one that reports nothing.
  2. THE ENVIRONMENT SCOPE. A dev deployment must never delete a production row
     — preview deployments share the dev database, which is why the column
     exists at all. The fake below really filters on it, so the assertion fails
     if the predicate is dropped.
  3. THE ENGINEER GATE. Below engineer is 403, unauthenticated is 401.
  4. NO EMAIL ADDRESS CAN APPEAR IN THE RESPONSE. The rows being deleted are the
     one place in this feature where attempted addresses live. They are
     unverified strings a stranger typed, some belong to real people, and a list
     of them is an enumeration oracle. The fake seeds real-looking addresses so
     the assertion has something to catch.
  5. THE SQL IS SQLALCHEMY-LEGAL. ``text()`` does not bind ``:name::type``; the
     cast swallows the parameter and the statement is a syntax error only
     against a REAL database, while every faked test here passes. Same structural
     guard as tests/test_login_auto_block.py, applied to the new module.

Fake sessions and dependency overrides, in the style of test_login_failures.py —
no database. The fake is a real (tiny) implementation of the three tables rather
than a stub, so the filtering assertions mean something.
"""

import re
import uuid
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.sql.elements import TextClause

from app.api.dependencies.auth import get_current_db_user
from app.core.database import get_session
from app.main import app
from app.schemas.auth import UserContext
from app.services import login_campaign

ENV = "development"
OTHER_ENV = "production"
ATTACKER = "134.82.68.139"


def _ctx(*roles: str, user_id: int = 7) -> UserContext:
    return UserContext(
        user_id=user_id,
        auth_user_id=uuid.UUID("11111111-1111-1111-1111-111111111111"),
        roles=list(roles),
    )


class _Result:
    """Just enough of a SQLAlchemy result for ``.all()`` / ``.mappings().all()``."""

    def __init__(self, rows: list[dict]):
        self._rows = rows

    def all(self) -> list[dict]:
        return list(self._rows)

    def mappings(self) -> "_Result":
        return self


class FakeCampaignDb:
    """The three tables, small enough to read and real enough to filter.

    Deliberately NOT a stub that returns canned counts: the properties under test
    are "only this source's rows go" and "only this environment's rows go", and a
    stub would pass with either predicate deleted. Rows carry an ``email`` so the
    no-address assertion has something to leak if the service ever selects it.
    """

    def __init__(self):
        self.failures: list[dict] = []
        self.incidents: list[dict] = []
        self.blocks: list[dict] = []
        self.added: list = []
        self.commits = 0
        self.rollbacks = 0

    # --- seeding -------------------------------------------------------------

    def add_failures(self, ip: str | None, emails: list[str]) -> None:
        for email in emails:
            self.failures.append(
                {
                    "login_failure_id": len(self.failures) + 1,
                    "ip_address": ip,
                    "email": email,
                }
            )

    def add_incident(self, ip: str, environment: str = ENV) -> None:
        self.incidents.append(
            {
                "abuse_incident_id": len(self.incidents) + 1,
                "environment": environment,
                "ip_address": ip,
            }
        )

    def add_block(
        self, ip: str, environment: str = ENV, active: bool = True
    ) -> None:
        self.blocks.append(
            {
                "block_id": len(self.blocks) + 1,
                "environment": environment,
                "ip_address": ip,
                "was_active": active,
            }
        )

    # --- session surface -----------------------------------------------------

    async def execute(self, stmt, params=None):
        params = params or {}
        if stmt is login_campaign._SQL_DELETE_FAILURES:
            # NO environment predicate: login_failures has no such column (see
            # the module docstring). Scoped by ip only, and a NULL ip never
            # matches.
            gone = [
                r for r in self.failures if r["ip_address"] == params["ip"]
            ]
            self.failures = [r for r in self.failures if r not in gone]
            return _Result([{"login_failure_id": r["login_failure_id"]} for r in gone])
        if stmt is login_campaign._SQL_DELETE_INCIDENTS:
            gone = self._match(self.incidents, params)
            self.incidents = [r for r in self.incidents if r not in gone]
            return _Result(
                [{"abuse_incident_id": r["abuse_incident_id"]} for r in gone]
            )
        if stmt is login_campaign._SQL_DELETE_BLOCKS:
            gone = self._match(self.blocks, params)
            self.blocks = [r for r in self.blocks if r not in gone]
            return _Result(
                [
                    {"block_id": r["block_id"], "was_active": r["was_active"]}
                    for r in gone
                ]
            )
        raise AssertionError(f"unexpected statement: {stmt}")

    @staticmethod
    def _match(rows: list[dict], params: dict) -> list[dict]:
        return [
            r
            for r in rows
            if r["ip_address"] == params["ip"]
            and r["environment"] == params["environment"]
        ]

    def add(self, obj) -> None:
        self.added.append(obj)

    async def commit(self) -> None:
        self.commits += 1

    async def rollback(self) -> None:
        self.rollbacks += 1

    # --- assertions helper ---------------------------------------------------

    @property
    def audit(self):
        return next(a for a in self.added if type(a).__name__ == "AuditLog")


@pytest.fixture
def db(monkeypatch):
    """A seeded campaign: the attacker's trail, plus rows that must survive."""
    store = FakeCampaignDb()
    # The campaign being cleaned up.
    store.add_failures(
        ATTACKER, ["dean@byu.edu", "jake@byu.edu", "nobody@example.com"]
    )
    store.add_incident(ATTACKER)
    store.add_block(ATTACKER, active=True)
    # Bystanders that must NOT be touched: another source, an attempt with no
    # forwarded address, and the SAME source recorded in the other environment.
    store.add_failures("203.0.113.9", ["someone@byu.edu"])
    store.add_failures(None, ["local@byu.edu"])
    store.add_incident(ATTACKER, environment=OTHER_ENV)
    store.add_block(ATTACKER, environment=OTHER_ENV, active=True)
    monkeypatch.setattr(
        login_campaign, "get_settings", lambda: SimpleNamespace(environment=ENV)
    )
    return store


@pytest.fixture
def client(db):
    """Engineer-authenticated client over the seeded fake."""

    async def _session():
        yield db

    app.dependency_overrides[get_session] = _session
    app.dependency_overrides[get_current_db_user] = lambda: _ctx("engineer")
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


# ================================================ 1. THE COUNTS ARE REAL ======


def test_deleting_a_campaign_reports_the_real_per_table_counts(client, db):
    resp = client.delete(f"/admin/login-campaigns/{ATTACKER}")

    assert resp.status_code == 200
    body = resp.json()
    assert body == {
        "ip_address": ATTACKER,
        "failures_deleted": 3,
        "incidents_deleted": 1,
        "blocks_deleted": 1,
        "active_blocks_deleted": 1,
    }
    # And the rows really went.
    assert [f["ip_address"] for f in db.failures] == ["203.0.113.9", None]
    assert db.commits == 1


def test_the_counts_are_not_an_assumed_success(client, db):
    """An address that matches nothing is a clean 200 of zeros, not a 404 and not
    a cheerful "deleted". Zeros are how an engineer learns they mistyped it."""
    resp = client.delete("/admin/login-campaigns/198.51.100.4")

    assert resp.status_code == 200
    assert resp.json() == {
        "ip_address": "198.51.100.4",
        "failures_deleted": 0,
        "incidents_deleted": 0,
        "blocks_deleted": 0,
        "active_blocks_deleted": 0,
    }
    # Nothing was removed from any table.
    assert len(db.failures) == 5
    assert len(db.incidents) == 2
    assert len(db.blocks) == 2


def test_deleting_the_same_campaign_twice_is_a_harmless_no_op(client):
    """Idempotent by design: a double-click, a retry, or a second engineer
    clearing the same address must not be an error."""
    first = client.delete(f"/admin/login-campaigns/{ATTACKER}")
    second = client.delete(f"/admin/login-campaigns/{ATTACKER}")

    assert first.json()["failures_deleted"] == 3
    assert second.status_code == 200
    assert second.json()["failures_deleted"] == 0


def test_an_attempt_with_no_forwarded_address_is_never_swept_up(client, db):
    """``ip_address = :ip`` never matches NULL. A failure recorded without a
    forwarded address belongs to no source, so no campaign delete may remove it —
    it stays visible on the per-attempt list."""
    client.delete(f"/admin/login-campaigns/{ATTACKER}")

    assert any(f["ip_address"] is None for f in db.failures)


def test_only_the_named_source_is_deleted(client, db):
    client.delete(f"/admin/login-campaigns/{ATTACKER}")

    assert [f["ip_address"] for f in db.failures if f["ip_address"]] == [
        "203.0.113.9"
    ]


# ================================== 2. THE BLOCK COUNT IS THE HUMAN FACT ======


def test_a_lapsed_block_is_removed_but_reported_as_not_in_force(client, db):
    """"Blocks removed" alone cannot tell an engineer whether anybody's access
    changed. ``active_blocks_deleted`` is the field that can, so a lapsed or
    already-lifted row must not inflate it."""
    db.blocks.clear()
    db.add_block(ATTACKER, active=False)
    db.add_block(ATTACKER, active=False)

    body = client.delete(f"/admin/login-campaigns/{ATTACKER}").json()

    assert body["blocks_deleted"] == 2
    assert body["active_blocks_deleted"] == 0


def test_deleting_an_active_block_is_reported_as_an_unblock(client, db):
    """The consequence the console has to state before the engineer confirms."""
    body = client.delete(f"/admin/login-campaigns/{ATTACKER}").json()

    assert body["active_blocks_deleted"] == 1
    assert not [b for b in db.blocks if b["environment"] == ENV]


# ================================================ 3. ENVIRONMENT SCOPING ======


def test_a_dev_deployment_cannot_delete_a_production_row(client, db):
    """Preview deployments share the dev database, which is the whole reason the
    ``environment`` column exists. The same source recorded under the other
    environment must survive untouched."""
    client.delete(f"/admin/login-campaigns/{ATTACKER}")

    assert [i["environment"] for i in db.incidents] == [OTHER_ENV]
    assert [b["environment"] for b in db.blocks] == [OTHER_ENV]


def test_the_scoped_statements_actually_carry_the_predicate():
    """The mutation check for the test above: it would still pass if the fake
    stopped filtering, so pin the predicate in the statement itself.

    ``login_failures`` is deliberately absent — it has no ``environment`` column
    (see app/models/login_failure.py), and its scope is the database it lives in,
    exactly as ``login_abuse._SQL_MEASURE`` and ``_SQL_SOURCES`` already assume.
    """
    for stmt in (
        login_campaign._SQL_DELETE_INCIDENTS,
        login_campaign._SQL_DELETE_BLOCKS,
    ):
        assert "environment = :environment" in str(stmt)
    assert "environment" not in str(login_campaign._SQL_DELETE_FAILURES)


def test_the_service_passes_the_running_environment(monkeypatch):
    """And the value bound is the deployment's own, never anything client-sent."""
    import asyncio

    seen: list[dict] = []

    class _Spy(FakeCampaignDb):
        async def execute(self, stmt, params=None):
            seen.append(dict(params or {}))
            return await super().execute(stmt, params)

    monkeypatch.setattr(
        login_campaign,
        "get_settings",
        lambda: SimpleNamespace(environment="staging-preview"),
    )
    asyncio.run(login_campaign.delete_campaign(_Spy(), ip_address=ATTACKER))

    scoped = [p for p in seen if "environment" in p]
    assert len(scoped) == 2
    assert {p["environment"] for p in scoped} == {"staging-preview"}


# ===================================================== 4. THE ENGINEER GATE ===


def test_super_admin_cannot_delete_a_campaign(db):
    """Engineer-gated like every other control on these screens. A super_admin is
    the highest non-engineer role, so it is the interesting negative."""

    async def _session():
        yield db

    app.dependency_overrides[get_session] = _session
    app.dependency_overrides[get_current_db_user] = lambda: _ctx("super_admin")
    with TestClient(app) as c:
        resp = c.delete(f"/admin/login-campaigns/{ATTACKER}")
    app.dependency_overrides.clear()

    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "forbidden"
    # And absolutely nothing was deleted on the way to the refusal.
    assert len(db.failures) == 5
    assert len(db.blocks) == 2


def test_an_unauthenticated_caller_cannot_delete_a_campaign(db):
    async def _session():
        yield db

    app.dependency_overrides[get_session] = _session
    with TestClient(app) as c:
        resp = c.delete(f"/admin/login-campaigns/{ATTACKER}")
    app.dependency_overrides.clear()

    assert resp.status_code == 401
    assert len(db.failures) == 5


def test_the_delete_is_rate_limited(client):
    """Destructive and irreversible, so it is braked like the other destructive
    engineer actions: ten per ten minutes, then 429."""
    codes = [
        client.delete(f"/admin/login-campaigns/198.51.100.{n}").status_code
        for n in range(1, 13)
    ]

    assert codes[:10] == [200] * 10
    assert codes[10:] == [429, 429]


# ============================== 5. NO ATTEMPTED ADDRESS MAY EVER LEAVE ========


def test_the_response_contains_no_email_address(client, db):
    """The rows being deleted are the one place in this feature where attempted
    addresses live. They are unverified strings a stranger typed, some belong to
    real people, and a list of them is an enumeration oracle. Counts only — the
    same rule the attack table, the block list and the Slack alert hold."""
    raw = client.delete(f"/admin/login-campaigns/{ATTACKER}").text

    assert "@" not in raw
    assert "byu.edu" not in raw
    assert "example.com" not in raw
    # The counts really are there — this is not passing because the body is empty.
    assert '"failures_deleted":3' in raw.replace(" ", "")


def test_no_statement_selects_the_email_column():
    """The mutation check for the test above. Nothing can leak an address if no
    statement ever reads one, so pin that rather than only the rendered body —
    a later ``RETURNING email`` "so the engineer can see what they removed" is
    exactly the change this guard exists to stop."""
    for stmt in (
        login_campaign._SQL_DELETE_FAILURES,
        login_campaign._SQL_DELETE_INCIDENTS,
        login_campaign._SQL_DELETE_BLOCKS,
    ):
        assert "email" not in str(stmt).lower()


def test_the_response_model_declares_only_counts():
    """No field on the wire can hold an address, whatever the statements do."""
    from app.api.routes.admin import LoginCampaignDeleted

    assert set(LoginCampaignDeleted.model_fields) == {
        "ip_address",
        "failures_deleted",
        "incidents_deleted",
        "blocks_deleted",
        "active_blocks_deleted",
    }


# ============================================================ 6. THE AUDIT ====


def test_the_delete_is_audited_with_the_ip_and_the_counts(client, db):
    """This is a destructive route and the forensic trail is the point. The
    before_flush guard in app/models/audit.py reroutes an engineer's AuditLog
    into the append-only engineer_action_log, so adding an AuditLog here is what
    puts the action beyond the acting engineer's reach."""
    client.delete(f"/admin/login-campaigns/{ATTACKER}")

    audit = db.audit
    assert audit.action_type == "delete_login_campaign"
    assert audit.entity_type == "login_campaign"
    assert audit.user_id == 7
    assert ATTACKER in audit.field_name
    assert audit.old_value == (
        "failures=3;incidents=1;blocks=1;active_blocks=1"
    )
    # One transaction: the rows and the record of who removed them land together.
    assert db.commits == 1


def test_a_delete_that_removed_nothing_is_still_audited(client, db):
    """"I tried to clear this address" is forensically interesting whether or not
    it matched. An audit row that only appears on success is a trail with a hole
    in exactly the shape of a probe."""
    client.delete("/admin/login-campaigns/198.51.100.4")

    assert db.audit.old_value == (
        "failures=0;incidents=0;blocks=0;active_blocks=0"
    )


def test_the_audit_row_carries_no_email_address(client, db):
    client.delete(f"/admin/login-campaigns/{ATTACKER}")

    audit = db.audit
    for value in (audit.field_name, audit.old_value, audit.new_value):
        assert "@" not in (value or "")


# ================== 7. THE SQL IS SQLALCHEMY-LEGAL, NOT JUST VALID SQL ========
#
# Everything above drives a fake. That is the right trade for the policy, but it
# leaves one hole and the first run against a real Postgres falls straight into
# it: `text()` does not bind `:name::type`. SQLAlchemy's placeholder pattern
# refuses a name followed by a colon, so a parameter written with a
# Postgres-style cast stays in the statement as literal text and the statement
# dies with `syntax error at or near ":"` — against a REAL database only, while
# every faked test here stays green.
#
# Same structural guard as tests/test_login_auto_block.py, applied to this
# module: no database, no fixtures, just "did SQLAlchemy actually see every
# placeholder you wrote".

#: A ``:placeholder``, ignoring ``::casts`` (the char before is never a colon).
_PLACEHOLDER = re.compile(r"(?<![:\w]):([a-z_][a-z0-9_]*)", re.IGNORECASE)


def _statements() -> dict[str, TextClause]:
    return {
        name: value
        for name, value in vars(login_campaign).items()
        if name.startswith("_SQL_") and isinstance(value, TextClause)
    }


def test_there_are_exactly_three_statements_and_they_are_all_deletes():
    statements = _statements()
    assert len(statements) == 3
    for name, stmt in statements.items():
        assert str(stmt).strip().upper().startswith("DELETE FROM"), name
        # Property 1: every count is measured, never assumed.
        assert "RETURNING" in str(stmt), name


def test_every_placeholder_written_is_a_placeholder_sqlalchemy_bound():
    """Write ``CAST(:name AS type)``, never ``:name::type``. It is uglier and it
    works."""
    for name, stmt in _statements().items():
        sql = "\n".join(line.split("--")[0] for line in str(stmt).splitlines())
        written = set(_PLACEHOLDER.findall(sql))
        bound = set(stmt._bindparams)
        assert written == bound, (
            f"login_campaign.{name}: SQLAlchemy did not bind "
            f"{sorted(written - bound)} (a ':name::type' cast swallows the "
            f"parameter — write CAST(:name AS type))"
        )


def test_delete_campaign_passes_every_parameter_its_statements_need():
    """A missing parameter is the other way this dies at runtime and nowhere
    else: ``session.execute`` raises on an unbound parameter."""
    import inspect

    source = inspect.getsource(login_campaign.delete_campaign)
    for stmt in _statements().values():
        for name in sorted(set(stmt._bindparams)):
            assert f'"{name}"' in source, f"delete_campaign() never passes :{name}"


# ======================================================= 8. INPUT HANDLING ====


def test_a_blank_address_is_refused_before_anything_is_deleted(client, db):
    resp = client.delete("/admin/login-campaigns/%20%20")

    assert resp.status_code == 422
    assert len(db.failures) == 5


def test_an_over_long_address_is_refused(client):
    """Capped at the column width (64) so a request cannot ask the database to
    match against something the column could never hold."""
    resp = client.delete(f"/admin/login-campaigns/{'9' * 65}")

    assert resp.status_code == 422


def test_the_address_is_normalised_the_same_way_it_was_stored(client, db):
    """``login_block.apply`` strips and truncates on the way in; the delete has to
    do the same or a padded address silently matches nothing."""
    resp = client.delete(f"/admin/login-campaigns/%20{ATTACKER}%20")

    assert resp.status_code == 200
    body = resp.json()
    assert body["ip_address"] == ATTACKER
    assert body["failures_deleted"] == 3
