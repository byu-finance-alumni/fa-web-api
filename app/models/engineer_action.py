"""Engineer-action log model (``engineer_action_log`` table).

Append-only, tamper-resistant record of actions taken by an *engineer* actor.

Background (security review, High): since #199 an engineer's ``audit_logs`` writes
are suppressed so engineer maintenance actions don't clutter the FERPA
record-change trail (a ``before_flush`` guard drops the pending AuditLog — see
``app/models/audit.py``). Dropping them OUTRIGHT, combined with the engineer-only
``DELETE /admin/logins`` purge (#200), left ZERO forensic trace of engineer
create / assign-role / delete-user actions — an engineer could act invisibly.

This table closes that blind spot. The same ``before_flush`` guard now REROUTES
each suppressed engineer AuditLog into an equivalent row here instead of
discarding it, so the action is still recorded — just out of the record-change
UI. It is APPEND-ONLY and tamper-resistant BY DESIGN:

* there is deliberately NO delete / purge route for this table, and no view-gate
  an engineer can flip;
* ``DELETE /admin/logins`` (#200) does NOT touch it;
* only the ``super_admin`` role can READ it (``GET /admin/engineer-actions``);
  the engineer — the audited party — cannot read, view-gate, delete, or disable
  their own oversight trail.

Columns mirror ``audit_logs``. ``actor_email`` is snapshotted at INSERT by a DB
trigger (``trg_engineer_action_log_snapshot_actor``, mirroring the audit actor
snapshot) so the row survives the actor's later deletion (``actor_user_id`` ->
NULL) with attribution intact.
"""

import datetime

from sqlalchemy import BigInteger, DateTime, ForeignKey, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class EngineerActionLog(Base):
    __tablename__ = "engineer_action_log"

    # BigInteger in Postgres (the real table is GENERATED ALWAYS AS IDENTITY, see
    # migration 2026-07-07_engineer_action_log.sql). The ``sqlite`` variant renders
    # INTEGER PRIMARY KEY so the in-memory SQLite test DB autoincrements it -- the
    # reroute in app/models/audit.py adds this row mid-flush with no PK set, so it
    # relies on the DB generating one (as Postgres does). No effect on Postgres.
    engineer_action_log_id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"), primary_key=True
    )
    actor_user_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("users.user_id", ondelete="SET NULL")
    )
    # Actor identity snapshotted at INSERT time by a DB trigger (see migration
    # 2026-07-07_engineer_action_log.sql), so it survives the actor's later
    # deletion (actor_user_id -> NULL) — the oversight trail never loses who acted.
    actor_email: Mapped[str | None] = mapped_column(String(255))
    action_type: Mapped[str] = mapped_column(String(100), nullable=False)
    entity_type: Mapped[str] = mapped_column(String(100), nullable=False)
    entity_id: Mapped[int | None] = mapped_column(BigInteger)
    field_name: Mapped[str | None] = mapped_column(String(255))
    old_value: Mapped[str | None] = mapped_column(Text)
    new_value: Mapped[str | None] = mapped_column(Text)
    occurred_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
