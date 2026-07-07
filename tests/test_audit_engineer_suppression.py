"""Engineer actions are not written to the audit trail (#199).

The engineer is a maintenance / super-user role whose actions must not clutter
the FERPA audit trail. A ``before_flush`` guard on the AuditLog model drops any
pending ``audit_logs`` INSERT when the current request's actor is an engineer,
recorded in a request-scoped contextvar by the auth layer.

These tests exercise the guard against a REAL SQLAlchemy session (in-memory
SQLite) -- the route suite uses fake in-memory sessions that never flush, so the
DB-level guard can only be observed with a real flush. We create just the
``audit_logs`` table (the model registers the global ``before_flush`` listener on
import) and assert an engineer flush persists nothing while a non-engineer flush
persists the row.
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


@pytest.fixture(autouse=True)
def _reset_actor():
    # Isolate the contextvar so a set in one test never leaks into another.
    reset_audit_actor()
    yield
    reset_audit_actor()


@pytest.fixture
def session():
    engine = create_engine("sqlite://")
    # Create ONLY the audit_logs table (SQLite tolerates the users FK reference
    # without the parent table), so we avoid the Postgres-specific models.
    AuditLog.__table__.create(engine)
    with Session(engine) as s:
        yield s
    engine.dispose()


def _count(session: Session) -> int:
    return session.scalar(select(func.count()).select_from(AuditLog)) or 0


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


def test_engineer_action_writes_no_audit_row(session):
    set_audit_actor(["engineer"])
    assert audit_suppressed() is True

    session.add(_new_audit())
    session.commit()

    assert _count(session) == 0


def test_non_engineer_action_still_audits(session):
    set_audit_actor(["super_admin"])
    assert audit_suppressed() is False

    session.add(_new_audit())
    session.commit()

    assert _count(session) == 1


def test_engineer_among_several_roles_is_suppressed(session):
    set_audit_actor(["view_only", "engineer"])

    session.add(_new_audit())
    session.commit()

    assert _count(session) == 0


def test_default_actor_is_not_suppressed(session):
    # No actor set (default): the trail is preserved (fail toward keeping it).
    session.add(_new_audit())
    session.commit()

    assert _count(session) == 1


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
