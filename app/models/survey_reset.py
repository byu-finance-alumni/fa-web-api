"""Survey campaign reset log (#395, revised 2026-08-05).

One row per "make this alumnus surveyable again" action. This table is the WHOLE
mechanism: a reset writes a row here and changes nothing else.

Why an event table rather than a flag on the rows it unblocks
-------------------------------------------------------------
The reset used to DELETE the alum's ``survey_responses`` and ``survey_send_log``
rows. Jake, 2026-08-05: "when you reset the campaign the responses should not be
reset, they should still be in the db." The submitted answers are the point of
the survey; an eligibility problem is no reason to destroy them.

Recording the event and having the exclusion queries ignore everything that
predates it leaves ``survey_responses`` **completely untouched** — no new column,
no UPDATE, not one byte rewritten. A ``superseded_at`` stamp on each response row
would work equally well for the queries, but it means writing to the very rows
the requirement is about, and it can only ever answer "is this superseded?",
never "who reset this person, when, and what did they think they were doing?".
This table answers both, and the response rows stay exactly as the alum left
them.

The one place that could not be done with a timestamp alone
-----------------------------------------------------------
``survey_send_log`` is UNIQUE on ``(graduation_year, alumni_id, stage,
cycle_seq)``. Teaching the READS to ignore an old row does not help: the
constraint physically refuses the new one, and ``_claim_batch``'s
``ON CONFLICT DO NOTHING`` would silently drop that recipient — the console would
call them eligible and the sender would skip them, which is the exact
count-vs-send disagreement this codebase keeps re-inventing. So the send log
carries ``reset_seq`` (0 = sent before this alumnus was ever reset, N = sent
after their Nth reset) and the unique key includes it. Old rows are never
updated; the log stays append-only.

See ``database/migrations/2026-08-05_survey_reset_log.sql``.
"""

import datetime

from sqlalchemy import BigInteger, DateTime, ForeignKey, Integer, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class SurveyResetLog(Base):
    __tablename__ = "survey_reset_log"
    __table_args__ = (
        UniqueConstraint(
            "alumni_id", "reset_seq", name="uq_survey_reset_log_alumni_seq"
        ),
    )

    survey_reset_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    alumni_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("alumni.alumni_id", ondelete="CASCADE"), nullable=False
    )
    # Per-alumnus counter starting at 1 — also the value new send-log rows carry
    # (see :class:`app.models.survey_schedule.SurveySendLog.reset_seq`).
    reset_seq: Mapped[int] = mapped_column(Integer, nullable=False)
    # The moment everything earlier stops counting. Responses are compared
    # against this (they carry no reset column of their own, deliberately).
    reset_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    reset_by_user_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("users.user_id", ondelete="SET NULL")
    )
    # What this reset moved out of the way — NOT what it removed. Kept next to
    # the event so "what did that button do?" is answerable without re-deriving
    # it from tables that have since changed.
    sends_superseded: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    responses_superseded: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
