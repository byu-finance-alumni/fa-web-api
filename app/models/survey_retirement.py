"""Retired survey campaigns (#398, revised 2026-08-05).

One row per campaign an engineer has DELETED. The table exists for exactly one
reason: ``survey_schedule`` is the sole holder of a graduation year's
``cycle_seq``, and ``survey_send_log`` rows are scoped by it. Delete the schedule
row and the year has no cycle any more — it reads as cycle 1, the existing log
rows become the current campaign's, and the next campaign for that year finds
everyone already emailed and sends to nobody. That is #357, and it fails
silently.

So the cycle number outlives the campaign here. ``current_cycle_seq`` resolves a
year with no schedule to ``max(cycle_seq) + 1`` over this table, which puts every
new campaign for that year ABOVE the retired rows: the cycle-scoped double-send
guard cannot see them, and the send log's unique key cannot collide with them.

Same shape as :class:`app.models.survey_reset.SurveyResetLog`, on purpose
-------------------------------------------------------------------------
Both are append-only events that SUPERSEDE rather than rewrite. A reset retires
one alumnus's sends by ``reset_seq``; a retirement retires one campaign's sends
by ``cycle_seq``. Neither deletes or updates a single ``survey_send_log`` or
``survey_responses`` row — those stay exactly as they were, keep rendering on the
profile's Surveys tab, and keep counting toward the Resend usage meter, because
the emails really were sent.

It is also the campaign's tombstone. The schedule row is gone, so without
``previous_status`` / ``start_date`` / the counts here, "a 2019 campaign was
deleted" would be the whole of what survives.

See ``database/migrations/2026-08-05_survey_campaign_retirement.sql``.
"""

import datetime

from sqlalchemy import (
    BigInteger,
    Date,
    DateTime,
    ForeignKey,
    Integer,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class SurveyCampaignRetirement(Base):
    __tablename__ = "survey_campaign_retirement"
    __table_args__ = (
        # A (year, cycle) can only be retired once — two concurrent deletes of
        # the same campaign make the loser error rather than write a second
        # tombstone for a cycle that is already retired.
        UniqueConstraint(
            "graduation_year",
            "cycle_seq",
            name="uq_survey_campaign_retirement_year_cycle",
        ),
    )

    survey_campaign_retirement_id: Mapped[int] = mapped_column(
        BigInteger, primary_key=True
    )
    graduation_year: Mapped[int] = mapped_column(Integer, nullable=False)
    # The cycle the deleted campaign was on. The next campaign for this year
    # starts above it; that is the whole mechanism.
    cycle_seq: Mapped[int] = mapped_column(Integer, nullable=False)
    retired_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    retired_by_user_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("users.user_id", ondelete="SET NULL")
    )
    # The campaign as it was — the schedule row that held these is gone.
    previous_status: Mapped[str | None] = mapped_column(String(20))
    start_date: Mapped[datetime.date | None] = mapped_column(Date)
    # What this retirement MOVED OUT OF THE WAY, never what it removed. Both
    # sets of rows are still in the database.
    sends_retired: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    responses_kept: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
