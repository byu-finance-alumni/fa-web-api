"""CRM activity models: interactions, follow-up tasks, surveys, attachments.

Maps ``interactions``, ``follow_up_tasks``, ``surveys``, and ``attachments``
from ``database/schema.sql``. ``attachments`` has only an ``uploaded_at`` column
(no created_at/updated_at), so it declares that timestamp directly.
"""

from __future__ import annotations

import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base
from app.models.mixins import TimestampMixin


class Interaction(TimestampMixin, Base):
    __tablename__ = "interactions"

    interaction_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    alumni_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("alumni.alumni_id", ondelete="CASCADE"),
        nullable=False,
    )
    user_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("users.user_id", ondelete="SET NULL")
    )
    interaction_type: Mapped[str | None] = mapped_column(String(100))
    interaction_date_time: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    interaction_notes: Mapped[str | None] = mapped_column(Text)


class FollowUpTask(TimestampMixin, Base):
    __tablename__ = "follow_up_tasks"

    follow_up_task_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    alumni_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("alumni.alumni_id", ondelete="CASCADE"),
        nullable=False,
    )
    assigned_to_user_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("users.user_id", ondelete="SET NULL")
    )
    task_title: Mapped[str | None] = mapped_column(String(255))
    due_date: Mapped[datetime.date | None] = mapped_column(Date)
    completed: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    completed_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    task_notes: Mapped[str | None] = mapped_column(Text)


class Survey(TimestampMixin, Base):
    __tablename__ = "surveys"

    survey_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    alumni_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("alumni.alumni_id", ondelete="CASCADE"),
        nullable=False,
    )
    survey_year: Mapped[int | None] = mapped_column(Integer)
    survey_due_date: Mapped[datetime.date | None] = mapped_column(Date)
    completed: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    completed_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    survey_status: Mapped[str | None] = mapped_column(String(100))
    survey_notes: Mapped[str | None] = mapped_column(Text)


class Attachment(Base):
    __tablename__ = "attachments"

    attachment_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    alumni_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("alumni.alumni_id", ondelete="CASCADE"),
        nullable=False,
    )
    uploaded_by_user_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("users.user_id", ondelete="SET NULL")
    )
    file_name: Mapped[str] = mapped_column(String(255), nullable=False)
    storage_key: Mapped[str] = mapped_column(String(500), nullable=False)
    file_type: Mapped[str | None] = mapped_column(String(100))
    attachment_notes: Mapped[str | None] = mapped_column(Text)
    uploaded_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
