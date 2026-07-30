"""Survey send-scheduler models (#542).

Two tables drive the auto-send of the annual "confirm your info" survey:

* ``survey_schedule`` — one row per graduation year: when the campaign starts and
  what state it's in. The daily Vercel cron scans these for due campaigns.
* ``survey_send_log`` — an append-only record of every (year, alumni, stage)
  email actually delivered. Its UNIQUE ``(graduation_year, alumni_id, stage)`` is
  the guardrail that stops a cron run from re-emailing anyone across runs — even
  if a previous run crashed or was throttled part-way through.

See migration ``database/migrations/2026-07-29_survey_scheduler.sql``.
"""

import datetime

from sqlalchemy import (
    BigInteger,
    Date,
    DateTime,
    ForeignKey,
    Integer,
    SmallInteger,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base
from app.models.mixins import TimestampMixin


class SurveySchedule(TimestampMixin, Base):
    __tablename__ = "survey_schedule"

    survey_schedule_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    # One schedule per graduation year (the survey is per-cohort).
    graduation_year: Mapped[int] = mapped_column(
        Integer, nullable=False, unique=True
    )
    # The initial send date. Stage advances weekly from here (0 / 1 / 2).
    start_date: Mapped[datetime.date] = mapped_column(Date, nullable=False)
    # 'scheduled' -> 'active' (first send done) -> 'completed' (all stages sent)
    # or 'cancelled'. CHECK constraint mirrors these in the DB.
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default="scheduled"
    )
    created_by_user_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("users.user_id", ondelete="SET NULL")
    )
    # Stamp of the last cron run that touched this schedule.
    last_run_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    # created_at / updated_at come from TimestampMixin.


class SurveySendLog(Base):
    """Append-only log of delivered survey emails.

    The UNIQUE ``(graduation_year, alumni_id, stage)`` is what prevents
    double-emailing: the scheduler inserts a row per recipient right after each
    successful Resend batch, then excludes anyone already logged on later runs.
    """

    __tablename__ = "survey_send_log"
    __table_args__ = (
        UniqueConstraint(
            "graduation_year",
            "alumni_id",
            "stage",
            name="uq_survey_send_log_year_alumni_stage",
        ),
    )

    survey_send_log_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    graduation_year: Mapped[int] = mapped_column(Integer, nullable=False)
    alumni_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("alumni.alumni_id", ondelete="CASCADE"),
        nullable=False,
    )
    # 0 = initial, 1 = 1-week reminder, 2 = 2-week reminder.
    stage: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    sent_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
