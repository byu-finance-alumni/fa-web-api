"""Resend delivery events for survey emails (fa-web-app #858).

One row per Resend webhook delivery we accept on ``POST /webhooks/resend``:
``email.bounced`` and ``email.complained``. Everything else Resend can send is
acknowledged and dropped.

* ``svix_id`` is UNIQUE -- Svix redelivers until it sees a 2xx, so a repeat is a
  no-op rather than a second row.
* ``alumni_id`` / ``graduation_year`` are resolved at receipt: the message id is
  looked up in ``survey_send_log.resend_email_id``, falling back to the
  ``alumni_id`` / ``graduation_year`` tags the sender puts on every email. NULL
  when neither matches (e.g. an email that was not a survey email).
* NO email address and NO raw payload is stored. The address the console shows
  comes from ``survey_send_log.sent_to``.

The console lists PERMANENT bounces only and changes no alumni data -- staff fix
the address by hand. See ``database/migrations/2026-10-07_survey_email_bounces.sql``.
"""

import datetime

from sqlalchemy import BigInteger, DateTime, ForeignKey, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base

EVENT_BOUNCED = "email.bounced"
EVENT_COMPLAINED = "email.complained"
BOUNCE_PERMANENT = "permanent"


class SurveyEmailEvent(Base):
    __tablename__ = "survey_email_events"

    survey_email_event_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    resend_email_id: Mapped[str | None] = mapped_column(String(100), index=True)
    alumni_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("alumni.alumni_id", ondelete="SET NULL")
    )
    graduation_year: Mapped[int | None] = mapped_column(Integer)
    event_type: Mapped[str] = mapped_column(String(40), nullable=False)
    # Lowercased Resend ``bounce.type``: permanent | transient | undetermined.
    bounce_type: Mapped[str | None] = mapped_column(String(40))
    bounce_subtype: Mapped[str | None] = mapped_column(String(60))
    occurred_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    svix_id: Mapped[str] = mapped_column(String(100), nullable=False, unique=True)
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
