"""Tags + status labels and their alumni join tables.

Maps ``tags`` / ``alumni_tags`` and ``status_labels`` / ``alumni_status_labels``
from ``database/schema.sql``. The lookup tables (``tags``, ``status_labels``)
carry created_at + updated_at; the join tables are append-only (created_at only).
"""

from __future__ import annotations

from sqlalchemy import BigInteger, ForeignKey, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base
from app.models.mixins import CreatedAtMixin, TimestampMixin


class Tag(TimestampMixin, Base):
    __tablename__ = "tags"

    tag_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    tag_name: Mapped[str] = mapped_column(String(100), nullable=False, unique=True)
    tag_description: Mapped[str | None] = mapped_column(Text)


class AlumniTag(CreatedAtMixin, Base):
    __tablename__ = "alumni_tags"
    __table_args__ = (UniqueConstraint("alumni_id", "tag_id", name="uq_alumni_tags"),)

    alumni_tag_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    alumni_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("alumni.alumni_id", ondelete="CASCADE"),
        nullable=False,
    )
    tag_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("tags.tag_id", ondelete="CASCADE"), nullable=False
    )


class StatusLabel(TimestampMixin, Base):
    __tablename__ = "status_labels"

    status_label_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    status_label_name: Mapped[str] = mapped_column(
        String(100), nullable=False, unique=True
    )
    status_label_description: Mapped[str | None] = mapped_column(Text)


class AlumniStatusLabel(CreatedAtMixin, Base):
    __tablename__ = "alumni_status_labels"
    __table_args__ = (
        UniqueConstraint(
            "alumni_id", "status_label_id", name="uq_alumni_status_labels"
        ),
    )

    alumni_status_label_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    alumni_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("alumni.alumni_id", ondelete="CASCADE"),
        nullable=False,
    )
    status_label_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("status_labels.status_label_id", ondelete="CASCADE"),
        nullable=False,
    )
