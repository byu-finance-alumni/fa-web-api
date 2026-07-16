"""Login-failure model (``login_failures`` table).

One row per FAILED sign-in attempt — the per-attempt security log behind the
engineer "Login failures" tab. This is distinct from ``login_attempts`` (the
rolling per-email counter that drives the cooldown/lock) and from
``login_events`` (one row per SUCCESSFUL sign-in): those don't preserve who
failed, when, or from what IP. This table does.

``email`` is the attempted address, snapshotted at insert. Intentionally NOT
linked by foreign key to ``users``: a failure may be for an email that has no
account at all (a probe / typo), and that is still a meaningful thing to log —
so there is nothing to reference. Mirrors ``login_attempt``'s deliberate lack of
a FK for the same reason.
"""

import datetime

from sqlalchemy import BigInteger, DateTime, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class LoginFailure(Base):
    __tablename__ = "login_failures"

    login_failure_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    # The attempted email, snapshotted at insert. No FK — the address may not
    # belong to any account (a probe). Stored lowercased to match the throttle's
    # case-insensitive keying (see app/services/login_lockout.py / login_attempt).
    email: Mapped[str] = mapped_column(String(255), nullable=False)
    occurred_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    # Client IP + approximate (IP-based) location, forwarded by the Next.js login
    # action from the incoming request. All nullable (absent in local dev / when
    # the client forwards no context).
    ip_address: Mapped[str | None] = mapped_column(String(64))
    city: Mapped[str | None] = mapped_column(String(128))
    region: Mapped[str | None] = mapped_column(String(128))
    country: Mapped[str | None] = mapped_column(String(64))
    # A coarse failure reason (e.g. a Supabase auth error code) if the client
    # forwards one; purely informational and optional.
    reason: Mapped[str | None] = mapped_column(String(64))
