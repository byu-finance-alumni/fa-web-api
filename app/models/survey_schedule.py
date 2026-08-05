"""Survey send-scheduler models (#542).

Two tables drive the auto-send of the annual "confirm your info" survey:

* ``survey_schedule`` — one row per graduation year: when the campaign starts and
  what state it's in. The daily Vercel cron scans these for due campaigns.
* ``survey_send_log`` — an append-only record of every (year, alumni, stage,
  cycle) email actually delivered. Its UNIQUE
  ``(graduation_year, alumni_id, stage, cycle_seq)`` is the guardrail that stops
  a cron run from re-emailing anyone across runs — even if a previous run crashed
  or was throttled part-way through — while still letting the NEXT annual cycle
  reach the same cohort (#357).

See migrations ``database/migrations/2026-07-29_survey_scheduler.sql`` and
``2026-08-03_survey_campaign_cycle.sql``.

These two tables, with ``survey_responses``, are the SOURCE OF TRUTH for an
alum's survey history — ``profile._derive_survey_history`` builds the profile's
Surveys tab from them (send log for what went out, schedule's ``start_date`` for
the due date). The legacy ``surveys`` table is read-only and must not be written
to; see ``models.crm.Survey``.
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
    # 'scheduled' -> 'active' (first send done) -> 'completed' (all stages sent),
    # or 'paused' (reversible stop) / 'cancelled' (terminal). CHECK constraint
    # mirrors these in the DB.
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
    # When the campaign was paused — NULL unless status == 'paused'. Load-bearing,
    # not just an audit stamp: the send stage is derived from
    # ``today - start_date``, so resume shifts ``start_date`` forward by the
    # paused duration to keep the cadence. See ``survey_schedule.resume_schedule``.
    paused_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    # The status the campaign held when it was paused ('scheduled' or 'active'),
    # so resume restores it exactly rather than guessing. Cleared on resume.
    paused_from_status: Mapped[str | None] = mapped_column(String(20))
    # Which campaign this year is on (#357). 1 for the first, incremented only by
    # `start_new_cycle` — the annual re-run. Editing a campaign's start date
    # leaves it alone, which is what makes correcting a typo safe.
    #
    # Deliberately an opaque counter, NOT a date: a campaign starting in late
    # December sends its reminders in January, so a year-derived cycle would flip
    # mid-campaign and re-send the initial to the whole cohort. Resume shifts
    # `start_date` forward too, so it can cross a year boundary on its own.
    cycle_seq: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default="1"
    )
    # created_at / updated_at come from TimestampMixin.


class SurveySendLog(Base):
    """Append-only log of delivered survey emails.

    The UNIQUE ``(graduation_year, alumni_id, stage, cycle_seq)`` is what
    prevents double-emailing: the scheduler inserts a row per recipient right
    after each successful Resend batch, then excludes anyone already logged on
    later runs.

    ``cycle_seq`` is in that key for #357. Without it the guard was an ALL-TIME
    question — nothing deletes from this table — so a year's second campaign
    selected zero targets at every stage and "completed" having emailed nobody.
    Every read of this table must be scoped to a cycle; an unscoped one silently
    reverts to that bug.
    """

    __tablename__ = "survey_send_log"
    __table_args__ = (
        UniqueConstraint(
            "graduation_year",
            "alumni_id",
            "stage",
            "cycle_seq",
            "reset_seq",
            name="uq_survey_send_log_year_alumni_stage",
        ),
    )

    survey_send_log_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    graduation_year: Mapped[int] = mapped_column(Integer, nullable=False)
    # The campaign this email belonged to — see `SurveySchedule.cycle_seq`.
    # Backfilled to 1 for every row that predates #357.
    cycle_seq: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default="1"
    )
    alumni_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("alumni.alumni_id", ondelete="CASCADE"),
        nullable=False,
    )
    # 0 = initial, 1 = 1-week reminder, 2 = 2-week reminder.
    stage: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    # Which reset generation this email belongs to (#395, 2026-08-05). 0 = the
    # alumnus had never been reset when it went out, which is every row that
    # existed before that change.
    #
    # It is in the unique key above because an engineer reset must let the SAME
    # (year, alumni, stage, cycle) be emailed again WITHOUT deleting the row that
    # recorded the first one. Ignoring the old row in the reads is not enough —
    # the constraint refuses the insert, `_claim_batch`'s ON CONFLICT DO NOTHING
    # swallows it, and the recipient is silently never emailed while the console
    # still counts them as eligible.
    #
    # Written once, at claim time, from the alumnus's current reset count; never
    # updated afterwards, so this table stays append-only.
    reset_seq: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    sent_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
