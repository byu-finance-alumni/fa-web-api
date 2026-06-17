"""Data access for the audit log.

Thin query layer. ``build_audit_query`` is a pure function (no IO) so the filter
logic can be unit-tested by compiling the statement — mirroring
``repositories.alumni.build_alumni_query``.

All filtering happens in PostgreSQL (never client-side): action type, entity
type, acting-user email (substring, case-insensitive, via the joined ``users``
row), and a ``created_at`` date range. Rows are returned newest-first.
"""

import datetime

from sqlalchemy import Select, and_, func, or_, select

from app.models.audit import AuditLog
from app.models.user import User
from app.utils.sql import escape_like


def build_audit_query(
    *,
    action_type: str | None = None,
    entity_type: str | None = None,
    user: str | None = None,
    date_from: datetime.datetime | None = None,
    date_to: datetime.datetime | None = None,
) -> Select:
    """Build the filtered ``SELECT AuditLog, actor_email`` statement.

    Left-joins ``users`` so events whose actor was deleted (``user_id`` set
    NULL) still appear, and COALESCEs the live join with the per-row
    ``actor_email`` snapshot — so a deleted actor's email is still shown (and
    still filterable). ``user`` matches that email (live OR snapshot) with a
    case-insensitive substring. ``date_from`` / ``date_to`` bound ``created_at``
    inclusively. Ordered newest-first; no limit/offset applied.
    """
    # The actor's email, preferring the live users row but falling back to the
    # snapshot captured at write time (which survives the actor's deletion).
    actor_email = func.coalesce(User.email, AuditLog.actor_email)

    conditions = []
    if action_type:
        conditions.append(AuditLog.action_type == action_type)
    if entity_type:
        conditions.append(AuditLog.entity_type == entity_type)
    if user:
        like = f"%{escape_like(user)}%"
        conditions.append(
            or_(
                User.email.ilike(like, escape="\\"),
                AuditLog.actor_email.ilike(like, escape="\\"),
            )
        )
    if date_from is not None:
        conditions.append(AuditLog.created_at >= date_from)
    if date_to is not None:
        conditions.append(AuditLog.created_at <= date_to)

    stmt = select(AuditLog, actor_email).outerjoin(
        User, AuditLog.user_id == User.user_id
    )
    if conditions:
        stmt = stmt.where(and_(*conditions))
    return stmt.order_by(AuditLog.created_at.desc())
