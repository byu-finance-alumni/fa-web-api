"""Data access for the audit log.

Thin query layer. ``build_audit_query`` is a pure function (no IO) so the filter
logic can be unit-tested by compiling the statement — mirroring
``repositories.alumni.build_alumni_query``.

All filtering happens in PostgreSQL (never client-side): action type, entity
type, acting-user email (substring, case-insensitive, via the joined ``users``
row), and a ``created_at`` date range. Rows are returned newest-first.
"""

import datetime

from sqlalchemy import Select, and_, select

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
    """Build the filtered ``SELECT AuditLog, User.email`` statement.

    Left-joins ``users`` so events whose actor was deleted (``user_id`` set
    NULL) still appear. ``user`` filters on the actor's email with a
    case-insensitive substring match. ``date_from`` / ``date_to`` bound
    ``created_at`` inclusively. Ordered newest-first; no limit/offset applied.
    """
    conditions = []
    if action_type:
        conditions.append(AuditLog.action_type == action_type)
    if entity_type:
        conditions.append(AuditLog.entity_type == entity_type)
    if user:
        conditions.append(
            User.email.ilike(f"%{escape_like(user)}%", escape="\\")
        )
    if date_from is not None:
        conditions.append(AuditLog.created_at >= date_from)
    if date_to is not None:
        conditions.append(AuditLog.created_at <= date_to)

    stmt = select(AuditLog, User.email).outerjoin(
        User, AuditLog.user_id == User.user_id
    )
    if conditions:
        stmt = stmt.where(and_(*conditions))
    return stmt.order_by(AuditLog.created_at.desc())
