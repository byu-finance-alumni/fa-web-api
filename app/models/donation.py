"""Donation model (donations table) — Pay It Forward Fund (#161).

A per-alumnus ledger: each row is one gift of ``amount`` tied to a
``donation_year`` (required) and optional ``donation_month``. Per-year and
lifetime totals are rolled up in the API. Dollar amounts are gated to
full_access+ at the API layer; this model stores them unconditionally.
"""

from __future__ import annotations

from decimal import Decimal

from sqlalchemy import BigInteger, ForeignKey, Numeric, SmallInteger, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base
from app.models.mixins import TimestampMixin


class Donation(TimestampMixin, Base):
    __tablename__ = "donations"

    donation_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    alumni_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("alumni.alumni_id", ondelete="CASCADE"),
        nullable=False,
    )
    amount: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    donation_month: Mapped[int | None] = mapped_column(SmallInteger)
    donation_year: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    notes: Mapped[str | None] = mapped_column(Text)
    logged_by_user_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("users.user_id", ondelete="SET NULL")
    )
