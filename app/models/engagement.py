"""Engagement, program-engagement, and leadership models.

Maps from ``database/schema.sql``:
  * ``alumni_engagement``          — free-text interest type + notes
  * ``alumni_program_engagement``  — the willingness-flag profile (1:1 w/ alumni)
  * ``finance_society_leadership`` — student leadership roles
"""

from __future__ import annotations

from sqlalchemy import (
    BigInteger,
    Boolean,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base
from app.models.mixins import CreatedAtMixin, TimestampMixin


class AlumniEngagement(TimestampMixin, Base):
    __tablename__ = "alumni_engagement"

    engagement_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    alumni_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("alumni.alumni_id", ondelete="CASCADE"),
        nullable=False,
    )
    source_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("data_sources.source_id", ondelete="SET NULL")
    )
    engagement_interest_type: Mapped[str | None] = mapped_column(String(255))
    engagement_notes: Mapped[str | None] = mapped_column(Text)


class AlumniProgramEngagement(TimestampMixin, Base):
    __tablename__ = "alumni_program_engagement"
    __table_args__ = (
        UniqueConstraint("alumni_id", name="uq_alumni_program_engagement"),
    )

    engagement_profile_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    alumni_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("alumni.alumni_id", ondelete="CASCADE"),
        nullable=False,
    )
    source_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("data_sources.source_id", ondelete="SET NULL")
    )
    nettrek_host_willing: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    finance_conference_willing: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    mentor_willing: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    company_event_sponsor_willing: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    guest_speaker_willing: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    help_at_event_willing: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    case_competition_host_willing: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    women_in_finance_mentor_willing: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    hired_finance_intern: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    hired_finance_full_time: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    piff_donor: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    cfp_designation: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    cfa_designation: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    engagement_notes: Mapped[str | None] = mapped_column(Text)


class FinanceSocietyLeadership(CreatedAtMixin, Base):
    # schema.sql defines created_at only (no updated_at) — CreatedAtMixin.
    __tablename__ = "finance_society_leadership"

    finance_society_leadership_id: Mapped[int] = mapped_column(
        BigInteger, primary_key=True
    )
    alumni_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("alumni.alumni_id", ondelete="CASCADE"),
        nullable=False,
    )
    leadership_role: Mapped[str] = mapped_column(String(100), nullable=False)
    role_year: Mapped[int | None] = mapped_column(Integer)
