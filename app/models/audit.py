"""Audit log model (audit_logs table)."""

import datetime

from sqlalchemy import BigInteger, DateTime, ForeignKey, String, Text, event, func
from sqlalchemy.orm import Mapped, Session, mapped_column

from app.core.audit_context import audit_suppressed
from app.core.database import Base
from app.models.engineer_action import EngineerActionLog


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
    # Groups the rows written by ONE save into one version (#45). A save that
    # changes five fields writes five rows; without this they can only be grouped
    # by created_at, which is transaction-start time and therefore identical
    # across every record in a bulk import's single transaction. NULL on rows
    # written before this column existed, and on paths that write a single row
    # (nothing to group). See app/core/audit_context.new_change_set_id.
    change_set_id: Mapped[str | None] = mapped_column(String(36))
    # Where the write came from: 'manual' | 'import' (#45). Hand edits and bulk
    # CSV updates both flow through alumni_service.update_alumni, so without this
    # the trail cannot tell a spreadsheet correction from a typed one — which a
    # later restore feature needs, so it doesn't revert good imported data. NULL
    # on audit rows from paths that don't carry provenance.
    source: Mapped[str | None] = mapped_column(String(20))
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


@event.listens_for(Session, "before_flush")
def _reroute_engineer_audit_rows(session, flush_context, instances):
    """Reroute an engineer actor's ``audit_logs`` writes into the tamper-resistant
    ``engineer_action_log`` (#199, and the #199/#200 forensic blind spot).

    The engineer is a maintenance / super-user role whose actions must not clutter
    the FERPA record-change trail. When the current request's actor is an engineer
    -- recorded in a request-scoped contextvar by the auth layer (see
    ``app/core/audit_context``) -- every pending AuditLog INSERT is:

      1. mirrored into an equivalent ``EngineerActionLog`` row, so the action is
         still recorded in an append-only log the engineer cannot delete or
         disable (there is no purge route and only super_admin can read it), then
      2. expunged from the flush, so no ``audit_logs`` row is written and the
         record-change UI stays uncluttered.

    Originally (#199) the AuditLog was simply DROPPED; combined with the
    engineer-only ``DELETE /admin/logins`` purge (#200) that left ZERO forensic
    trace of engineer user-admin actions. Rerouting preserves the trail while
    keeping the audit UI clean. Non-engineer actors are unaffected.

    ``before_flush`` is the SQLAlchemy-sanctioned place to add objects: rows added
    to the session here are picked up by the SAME flush and persist (verified in
    ``tests/test_audit_engineer_suppression.py``). We iterate a snapshot
    (``list(session.new)``) so adding EngineerActionLog rows mid-iteration is safe.

    Registering on the base ``Session`` (which the AsyncSession drives) makes the
    guard central: it covers every callsite that adds an AuditLog, present and
    future, without any per-callsite change. The default (not suppressed) means
    the trail is preserved for everyone else.
    """
    if not audit_suppressed():
        return
    for obj in list(session.new):
        if isinstance(obj, AuditLog):
            # Mirror the suppressed audit row into the append-only engineer log so
            # the engineer's action leaves a tamper-resistant trace, then drop the
            # AuditLog so it never reaches audit_logs / the record-change UI.
            # actor_email is snapshotted by a DB trigger on INSERT (it is still
            # NULL on the pending AuditLog here), so we carry the actor's user_id.
            session.add(
                EngineerActionLog(
                    actor_user_id=obj.user_id,
                    actor_email=obj.actor_email,
                    action_type=obj.action_type,
                    entity_type=obj.entity_type,
                    entity_id=obj.entity_id,
                    field_name=obj.field_name,
                    old_value=obj.old_value,
                    new_value=obj.new_value,
                    # Carry the grouping key and provenance across too (#45), so a
                    # suppressed engineer save is still readable as ONE change set
                    # in the oversight trail rather than N unrelated rows.
                    change_set_id=obj.change_set_id,
                    source=obj.source,
                )
            )
            session.expunge(obj)
