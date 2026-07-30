"""Survey send-cap config model (#542 follow-up).

A SINGLE-ROW table (``id`` pinned to 1) holding the account-wide send budget the
scheduler paces against: when ``enabled`` the daily cron spends at most
``daily_limit`` emails per UTC day and ``monthly_limit`` per calendar month
across every graduation year, so a large cohort trickles out over several days
instead of all at once. Turn ``enabled`` off (e.g. after upgrading the Resend
plan) to drop the internal cap entirely — sends are then limited only by Resend.

See migration ``database/migrations/2026-07-29_survey_send_config.sql``.
"""

from sqlalchemy import BigInteger, Boolean, ForeignKey, Integer
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base
from app.models.mixins import TimestampMixin


class SurveySendConfig(TimestampMixin, Base):
    __tablename__ = "survey_send_config"

    # Pinned to 1 by a CHECK constraint — there is only ever one config row.
    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    # When true, the scheduler enforces the daily/monthly budget below. When
    # false there is no internal cap (sends are limited only by Resend).
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    # Emails/day and emails/month budgets (defaults = Resend Free tier).
    daily_limit: Mapped[int] = mapped_column(Integer, nullable=False, default=100)
    monthly_limit: Mapped[int] = mapped_column(
        Integer, nullable=False, default=3000
    )
    updated_by_user_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("users.user_id", ondelete="SET NULL")
    )
    # created_at / updated_at come from TimestampMixin.
