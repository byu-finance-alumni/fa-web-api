"""Login-event model (``login_events`` table).

One row per successful sign-in. Logins happen client-side via Supabase, so the
backend records them when the frontend calls ``POST /auth/login`` after a
successful sign-in (which also stamps ``users.last_login_at``).

Kept DELIBERATELY SEPARATE from ``audit_logs``: sign-in events are a security
log, not the record-change audit trail. ``email`` is snapshotted at insert and
``user_id`` is ``ON DELETE SET NULL`` so the history survives a later user
deletion with its attribution intact (mirrors the audit actor snapshot).
"""

import datetime

from sqlalchemy import BigInteger, DateTime, ForeignKey, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class LoginEvent(Base):
    __tablename__ = "login_events"

    login_event_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    user_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("users.user_id", ondelete="SET NULL")
    )
    # Snapshotted at insert so the row keeps who signed in even after the user
    # is deleted (user_id -> NULL).
    email: Mapped[str] = mapped_column(String(255), nullable=False)
    occurred_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    # Client IP + approximate (IP-based) location, forwarded by the Next.js login
    # action from the incoming request. All nullable (absent in local dev / on
    # rows recorded before this was added).
    ip_address: Mapped[str | None] = mapped_column(String(64))
    city: Mapped[str | None] = mapped_column(String(128))
    region: Mapped[str | None] = mapped_column(String(128))
    country: Mapped[str | None] = mapped_column(String(64))
