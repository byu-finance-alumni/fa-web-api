"""Duplicate-candidate model (duplicate_candidates table).

Advisory only — pairs of alumni flagged as possible duplicates. The system
never merges automatically; human review is required. Modeled here so the
alumni search can offer a "duplicate records" filter (the dashboard surfaces a
count of these candidates).
"""

from __future__ import annotations

import datetime

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class DuplicateCandidate(Base):
    __tablename__ = "duplicate_candidates"
    __table_args__ = (
        # Ordered + unique pair guard (#175): store a pair once, low id first, so
        # (a,b) and (b,a) cannot both exist.
        CheckConstraint(
            "alumni_id_1 < alumni_id_2", name="ck_duplicate_candidates_ordered"
        ),
        UniqueConstraint(
            "alumni_id_1", "alumni_id_2", name="uq_duplicate_candidates_pair"
        ),
    )

    duplicate_candidate_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    alumni_id_1: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("alumni.alumni_id", ondelete="CASCADE"),
        nullable=False,
    )
    alumni_id_2: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("alumni.alumni_id", ondelete="CASCADE"),
        nullable=False,
    )
    match_reason: Mapped[str | None] = mapped_column(Text)
    confidence_score: Mapped[float | None] = mapped_column(Float)
    duplicate_status: Mapped[str | None] = mapped_column(String(100))
    reviewed_by_user_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("users.user_id", ondelete="SET NULL")
    )
    reviewed_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    updated_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
