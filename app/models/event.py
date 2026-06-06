"""Event + attendance models (events, event_attendance tables)."""

from __future__ import annotations

import datetime

from sqlalchemy import (
    BigInteger,
    Date,
    ForeignKey,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base
from app.models.mixins import TimestampMixin


class Event(TimestampMixin, Base):
    __tablename__ = "events"

    event_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    logged_by_user_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("users.user_id", ondelete="SET NULL")
    )
    event_name: Mapped[str] = mapped_column(String(255), nullable=False)
    event_type: Mapped[str | None] = mapped_column(String(100))
    event_date: Mapped[datetime.date | None] = mapped_column(Date)
    event_location: Mapped[str | None] = mapped_column(String(255))
    event_notes: Mapped[str | None] = mapped_column(Text)


class EventAttendance(TimestampMixin, Base):
    __tablename__ = "event_attendance"
    __table_args__ = (
        UniqueConstraint("event_id", "alumni_id", name="uq_event_attendance"),
    )

    event_attendance_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    event_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("events.event_id", ondelete="CASCADE"), nullable=False
    )
    alumni_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("alumni.alumni_id", ondelete="CASCADE"),
        nullable=False,
    )
    attendance_status: Mapped[str | None] = mapped_column(String(100))
    attendance_notes: Mapped[str | None] = mapped_column(Text)
