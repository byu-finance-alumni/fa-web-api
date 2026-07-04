"""Login-attempt model (``login_attempts`` table).

Rolling per-email failed-login counter that backs the pre-login throttling /
lockout flow in ``app/services/login_lockout.py``. Keyed by the lowercased email
so it is case-insensitive. Intentionally NOT linked by foreign key to ``users``:
the cooldown applies to arbitrary (possibly non-existent) emails so the throttle
cannot be used to enumerate which emails are registered.
"""

import datetime

from sqlalchemy import CheckConstraint, DateTime, Integer, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class LoginAttempt(Base):
    __tablename__ = "login_attempts"
    __table_args__ = (
        # email_lc must already be lowercased by the writer (#176).
        CheckConstraint(
            "email_lc = lower(email_lc)", name="ck_login_attempts_email_lc_lower"
        ),
    )

    email_lc: Mapped[str] = mapped_column(Text, primary_key=True)
    failed_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    first_failed_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    last_failed_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    cooldown_until: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    updated_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
