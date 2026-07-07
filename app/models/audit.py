"""Audit log model (audit_logs table)."""

import datetime

from sqlalchemy import BigInteger, DateTime, ForeignKey, String, Text, event, func
from sqlalchemy.orm import Mapped, Session, mapped_column

from app.core.audit_context import audit_suppressed
from app.core.database import Base


class AuditLog(Base):
    __tablename__ = "audit_logs"

    audit_log_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    user_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("users.user_id", ondelete="SET NULL")
    )
    action_type: Mapped[str] = mapped_column(String(100), nullable=False)
    entity_type: Mapped[str] = mapped_column(String(100), nullable=False)
    entity_id: Mapped[int | None] = mapped_column(BigInteger)
    field_name: Mapped[str | None] = mapped_column(String(255))
    old_value: Mapped[str | None] = mapped_column(Text)
    new_value: Mapped[str | None] = mapped_column(Text)
    # Actor identity snapshotted at INSERT time by a DB trigger (see migration
    # 2026-06-17_audit_actor_snapshot.sql). Survives the actor's later deletion
    # (user_id -> NULL), so the trail never loses who performed an action.
    actor_email: Mapped[str | None] = mapped_column(String(255))
    actor_name: Mapped[str | None] = mapped_column(String(255))
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


@event.listens_for(Session, "before_flush")
def _drop_engineer_audit_rows(session, flush_context, instances):
    """Suppress ``audit_logs`` writes performed by an engineer actor (#199).

    The engineer is a maintenance / super-user role whose actions must not
    clutter the FERPA audit trail. When the current request's actor is an
    engineer -- recorded in a request-scoped contextvar by the auth layer (see
    ``app/core/audit_context``) -- every pending AuditLog INSERT is expunged from
    the flush, so no row is written. Non-engineer actors are unaffected.

    Registering on the base ``Session`` (which the AsyncSession drives) makes the
    guard central: it covers every callsite that adds an AuditLog, present and
    future, without any per-callsite change. The default (not suppressed) means
    the trail is preserved for everyone else.
    """
    if not audit_suppressed():
        return
    for obj in list(session.new):
        if isinstance(obj, AuditLog):
            session.expunge(obj)
