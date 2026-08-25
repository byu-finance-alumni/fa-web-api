"""Survey response model (survey_responses table).

An alum's staged "confirm your info" submission, awaiting admin review. `payload`
holds the submitted values keyed by survey field keys (`table.column`).

Together with `survey_send_log` and `survey_schedule`, these rows are the SOURCE
OF TRUTH for an alum's survey history — `profile._derive_survey_history` builds
the profile's Surveys tab from them. The legacy `surveys` table is read-only and
must not be written to; see `models.crm.Survey`.
"""

import datetime

from sqlalchemy import (
    BigInteger,
    DateTime,
    ForeignKey,
    Integer,
    SmallInteger,
    String,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class SurveyResponse(Base):
    __tablename__ = "survey_responses"

    survey_response_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    alumni_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("alumni.alumni_id", ondelete="CASCADE"), nullable=False
    )
    graduation_year: Mapped[int | None] = mapped_column(Integer)
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False)
    # FOUR values, and the DB CHECK is the authority (see `schema.sql`). Three
    # describe a submission that carried CHANGES — `pending` / `applied` /
    # `rejected`. The fourth, `confirmed` (#755), is "yes, everything is correct":
    # a reply that changed nothing, with an EMPTY `payload` and nothing for staff
    # to review. It counts as a reply everywhere the sender and the console ask
    # "have they answered?" (`survey_email.RESPONDED_STATUSES`) and appears in
    # NONE of the review-outcome columns, which keep meaning exactly what they
    # meant. `survey_email` names all four; no query should spell one as a bare
    # string.
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="pending")
    # Staging key of a NEW profile photo uploaded with this response (headshots
    # bucket, `survey-pending/<id>`), pending admin review. None when no photo.
    staged_photo_path: Mapped[str | None] = mapped_column(String(255))
    # WHICH campaign this response answers (#497), and which email in it the alum
    # had most recently been sent when they replied (0 = initial, 1 = 1-week
    # reminder, 2 = 2-week reminder). Both are written ONCE, at submit time, from
    # the `survey_send_log` row recording the email that was actually sent — see
    # `survey_email.sent_cycle_and_stage`.
    #
    # NULLABLE ON PURPOSE, and never backfilled. Every row that predates #497 has
    # no knowable cycle, and so does any submission that cannot be matched to a
    # logged send (a hand-issued link, or an alum whose graduation year changed
    # after they were emailed). NULL says "we do not know"; a guessed number
    # would be indistinguishable from an observed one in a report, which defeats
    # the reason for storing it. Readers must treat NULL as "exclude", not as
    # cycle 1.
    #
    # NEVER derive `cycle_seq` from `submitted_at` or any other date (#357). It
    # is an opaque counter: a campaign starting in late December sends its
    # reminders in January, and resume shifts `start_date` forward by the paused
    # duration, so a date-derived cycle flips mid-campaign and splits one
    # campaign's responses across two.
    #
    # CAPTURE ONLY as of #497 — nothing reads these yet. The per-cycle reporting
    # path is separate work; this exists so the history it will need starts
    # accumulating now, since it cannot be reconstructed later.
    cycle_seq: Mapped[int | None] = mapped_column(Integer)
    stage: Mapped[int | None] = mapped_column(SmallInteger)
    submitted_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    reviewed_by_user_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("users.user_id", ondelete="SET NULL")
    )
    reviewed_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True))
