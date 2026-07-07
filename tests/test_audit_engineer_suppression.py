"""Engineer actions are rerouted out of the audit trail into a tamper-resistant
log (#199, and the #199/#200 forensic blind spot).

The engineer is a maintenance / super-user role whose actions must not clutter
the FERPA record-change trail. A ``before_flush`` guard on the AuditLog model,
when the current request's actor is an engineer (recorded in a request-scoped
contextvar by the auth layer), REROUTES each pending ``audit_logs`` INSERT into an
equivalent ``engineer_action_log`` row and then drops the AuditLog -- so the
action leaves a tamper-resistant trace (no purge route, super_admin-only read)
while staying out of the audit UI.

These tests exercise the guard against a REAL SQLAlchemy session (in-memory
SQLite) -- the route suite uses fake in-memory sessions that never flush, so the
DB-level guard can only be observed with a real flush. We create the
``audit_logs`` and ``engineer_action_log`` tables (importing the models registers
the global ``before_flush`` listener) and assert: an engineer flush persists NO
audit_logs row but DOES persist an engineer_action_log row with the mirrored
fields; a non-engineer flush persists the audit_logs row and NO engineer row.
This also verifies the key mechanism -- that a row ADDED during before_flush
actually persists in the same flush.
"""

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from app.core.audit_context import (
    audit_suppressed,
    reset_audit_actor,
    set_audit_actor,
)
from app.models.audit import AuditLog  # registers the before_flush guard
from app.models.engineer_action import EngineerActionLog


@pytest.fixture(autouse=True)
def _reset_actor():
    # Isolate the contextvar so a set in one test never leaks into another.
    reset_audit_actor()
    yield
    reset_audit_actor()


@pytest.fixture
def session():
    engine = create_engine("sqlite://")
    # Create the audit_logs + engineer_action_log tables (SQLite tolerates the
    # users FK reference without the parent table), so we avoid the
    # Postgres-specific models while still exercising the reroute.
    AuditLog.__table__.create(engine)
    EngineerActionLog.__table__.create(engine)
    with Session(engine) as s:
        yield s
    engine.dispose()


def _count(session: Session) -> int:
    return session.scalar(select(func.count()).select_from(AuditLog)) or 0


def _eng_count(session: Session) -> int:
    return (
        session.scalar(select(func.count()).select_from(EngineerActionLog)) or 0
    )


def _new_audit() -> AuditLog:
    return AuditLog(
        # Explicit PK: the audit_logs id is DB-generated in Postgres, but the
        # SQLite test DB does not autoincrement the BigInteger PK, so set it here.
        audit_log_id=1,
        user_id=1,
        action_type="deactivate_user",
        entity_type="user",
        entity_id=2,
        field_name="active",
        old_value="True",
        new_value="False",
    )


def test_engineer_action_reroutes_to_engineer_log(session):
    set_audit_actor(["engineer"])
    assert audit_suppressed() is True

    session.add(_new_audit())
    session.commit()

    # No audit_logs row, but the action IS recorded in engineer_action_log.
    assert _count(session) == 0
    assert _eng_count(session) == 1


def test_engineer_reroute_mirrors_all_fields(session):
    # The rerouted row must carry the audit fields faithfully so the oversight
    # log is a complete record. Verifies a row ADDED during before_flush persists.
    set_audit_actor(["engineer"])
    session.add(_new_audit())
    session.commit()

    row = session.scalar(select(EngineerActionLog))
    assert row is not None
    assert row.actor_user_id == 1  # AuditLog.user_id -> actor_user_id
    assert row.action_type == "deactivate_user"
    assert row.entity_type == "user"
    assert row.entity_id == 2
    assert row.field_name == "active"
    assert row.old_value == "True"
    assert row.new_value == "False"
    assert row.occurred_at is not None  # server_default now()


def test_non_engineer_action_still_audits(session):
    set_audit_actor(["super_admin"])
    assert audit_suppressed() is False

    session.add(_new_audit())
    session.commit()

    # Audit row kept; nothing rerouted into the engineer log.
    assert _count(session) == 1
    assert _eng_count(session) == 0


def test_engineer_among_several_roles_is_suppressed(session):
    set_audit_actor(["view_only", "engineer"])

    session.add(_new_audit())
    session.commit()

    assert _count(session) == 0
    assert _eng_count(session) == 1


def test_default_actor_is_not_suppressed(session):
    # No actor set (default): the trail is preserved (fail toward keeping it).
    session.add(_new_audit())
    session.commit()

    assert _count(session) == 1
    assert _eng_count(session) == 0


def test_set_audit_actor_role_mapping():
    for roles, expected in (
        (["engineer"], True),
        (["super_admin"], False),
        (["full_access", "student"], False),
        ([], False),
        (None, False),
    ):
        set_audit_actor(roles)
        assert audit_suppressed() is expected
