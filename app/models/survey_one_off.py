"""One-off survey sends: the survey emailed to ONE alum from their profile (api#669).

Two tables, kept apart from ``survey_send_log`` ON PURPOSE:

* ``survey_one_off`` -- one row per "Send survey" press: who, which class year
  the link is minted for, who pressed it, and when (the day-0 that the 7- and
  14-day reminders are measured from).
* ``survey_one_off_send_log`` -- one row per email actually sent for it: stage 0
  (the send itself), 1 (day-7 reminder), 2 (day-14 reminder). UNIQUE on
  ``(survey_one_off_id, stage)`` -- the same claim-before-send guard the class
  log uses, so a cron retry can never send the same reminder twice.

WHY NOT ``survey_send_log``. A class campaign is keyed by ``cycle_seq`` and can
be sent only once; its stage rows ARE the double-send guard
(``survey_email.logged_alumni_ids``). A one-off row in that table would read as
"this person already had the class's stage 0", so the class send would silently
skip them -- exactly what Jake ruled out ("the later grad-year send does NOT skip
them"). Every read of ``survey_send_log`` stays a pure class-campaign read.

What one-off rows DO count for: the shared daily/monthly Resend budget
(``survey_email.get_send_usage`` adds them), the profile's Surveys tab, and the
"last sent" warning on the next one-off. See ``services/survey_one_off.py``.

Migration: ``database/migrations/2026-11-11_survey_one_off_sends.sql``.
"""

import datetime

from sqlalchemy import (
    BigInteger,
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


class SurveyOneOff(Base):
    __tablename__ = "survey_one_off"

    survey_one_off_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    alumni_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("alumni.alumni_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    # The year the survey link is minted for (the alum's graduation year at the
    # time of the send). Recorded, never used to pick a class campaign cycle.
    graduation_year: Mapped[int] = mapped_column(Integer, nullable=False)
    sent_by_user_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("users.user_id", ondelete="SET NULL")
    )
    # Day 0 of this one-off. Reminders are due 7 and 14 days after it.
    started_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class SurveyOneOffSendLog(Base):
    __tablename__ = "survey_one_off_send_log"
    __table_args__ = (
        UniqueConstraint(
            "survey_one_off_id", "stage", name="uq_survey_one_off_send_log_stage"
        ),
    )

    survey_one_off_send_log_id: Mapped[int] = mapped_column(
        BigInteger, primary_key=True
    )
    survey_one_off_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("survey_one_off.survey_one_off_id", ondelete="CASCADE"),
        nullable=False,
    )
    # 0 = the send itself, 1 = day-7 reminder, 2 = day-14 reminder.
    stage: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    sent_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    # Resend's message id and the address used, written best-effort after Resend
    # accepts the email (same as ``survey_send_log``).
    resend_email_id: Mapped[str | None] = mapped_column(String(100))
    sent_to: Mapped[str | None] = mapped_column(String(320))
