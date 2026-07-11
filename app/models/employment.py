"""Employment + education models.

Maps three tables from ``database/schema.sql``:
  * ``current_employment`` — the single current job (Career tab)
  * ``employment_history``  — prior roles (Employment tab)
  * ``education_history``    — degrees (Education data on the profile)
"""

from __future__ import annotations

import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base
from app.models.mixins import TimestampMixin


class CurrentEmployment(TimestampMixin, Base):
    __tablename__ = "current_employment"
    __table_args__ = (
        # One current-employment row per alum (#171).
        UniqueConstraint("alumni_id", name="uq_current_employment_alumni_id"),
    )

    current_employment_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    alumni_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("alumni.alumni_id", ondelete="CASCADE"),
        nullable=False,
    )
    source_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("data_sources.source_id", ondelete="SET NULL")
    )
    current_employer: Mapped[str | None] = mapped_column(String(255))
    current_title: Mapped[str | None] = mapped_column(String(255))
    current_industry: Mapped[str | None] = mapped_column(String(255))
    current_industry_secondary: Mapped[str | None] = mapped_column(String(255))
    # Company street address (the "Company Address" line on the profile, #366).
    # City/state/country/zip below are the finer-grained location fields.
    company_address: Mapped[str | None] = mapped_column(String(255))
    current_city: Mapped[str | None] = mapped_column(String(100))
    current_state: Mapped[str | None] = mapped_column(String(100))
    current_country: Mapped[str | None] = mapped_column(String(100))
    current_zip: Mapped[str | None] = mapped_column(String(20))
    seniority_level: Mapped[str | None] = mapped_column(String(100))
    last_verified_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True)
    )


class EmploymentHistory(TimestampMixin, Base):
    __tablename__ = "employment_history"

    employment_history_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    alumni_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("alumni.alumni_id", ondelete="CASCADE"),
        nullable=False,
    )
    source_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("data_sources.source_id", ondelete="SET NULL")
    )
    employer_name: Mapped[str | None] = mapped_column(String(255))
    employment_title: Mapped[str | None] = mapped_column(String(255))
    employment_industry: Mapped[str | None] = mapped_column(String(255))
    city: Mapped[str | None] = mapped_column(String(100))
    state: Mapped[str | None] = mapped_column(String(100))
    start_year: Mapped[int | None] = mapped_column(Integer)
    end_year: Mapped[int | None] = mapped_column(Integer)
    is_current: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )


class EducationHistory(TimestampMixin, Base):
    __tablename__ = "education_history"

    education_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    alumni_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("alumni.alumni_id", ondelete="CASCADE"),
        nullable=False,
    )
    source_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("data_sources.source_id", ondelete="SET NULL")
    )
    university: Mapped[str | None] = mapped_column(String(255))
    college: Mapped[str | None] = mapped_column(String(255))
    department: Mapped[str | None] = mapped_column(String(255))
    degree: Mapped[str | None] = mapped_column(String(255))
    major: Mapped[str | None] = mapped_column(String(255))
    degree_status: Mapped[str | None] = mapped_column(String(100))
    degree_year: Mapped[int | None] = mapped_column(Integer)
