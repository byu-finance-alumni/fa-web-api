"""The 6pm job-posting digest to staff (#567): who gets it, and what it cost.

Two tables, both small:

``opportunity_link_digest_config``
    A SINGLE-ROW table (``id`` pinned to 1, the same shape as
    ``alert_delivery_config``) holding the staff recipients the engineer sets in
    the console, and the digest's WATERMARK (``reported_through``): every survey
    posting submitted up to that instant has been reported -- and the local date
    it last ran (``last_digest_on``), so it runs at most once a day. An empty
    ``recipients`` array means no digest, and the per-posting alert of #771 is
    what fires instead — see ``app/services/opportunity_link_alert.py``.

``opportunity_link_digest_send_log``
    One row per digest e-mail actually handed to Resend. It exists for the
    survey's send budget and nothing else: the digest spends from the same Resend
    account and the same UTC-day quota as the survey, so
    ``survey_email.get_send_usage`` counts these rows beside ``survey_send_log``.
    Deliberately NO recipient address — the count is the whole requirement.

See migration ``database/migrations/2026-09-23_opportunity_link_digest.sql``.
"""

import datetime

from sqlalchemy import ARRAY, BigInteger, Date, DateTime, ForeignKey, Integer, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base
from app.models.mixins import TimestampMixin


class OpportunityLinkDigestConfig(TimestampMixin, Base):
    __tablename__ = "opportunity_link_digest_config"

    # Pinned to 1 by a CHECK constraint — there is only ever one config row.
    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    # Lowercased, deduped staff addresses. Empty = no digest. A CHECK in the
    # migration caps the count; the service validates the shape.
    recipients: Mapped[list[str]] = mapped_column(
        ARRAY(Text), nullable=False, default=list
    )
    # Every survey posting submitted at or before this instant has been reported
    # in a digest. NULL = never sent (the first run looks back a fixed window).
    reported_through: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    # The America/Denver date the digest last ran. At most one digest per local
    # day, however many cron calls arrive (two entries fire every evening).
    last_digest_on: Mapped[datetime.date | None] = mapped_column(Date)
    # Who last changed the recipients. Engineer-console detail; the durable
    # record is the audit trail (``set_opportunity_link_digest_recipients``).
    updated_by_user_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("users.user_id", ondelete="SET NULL")
    )
    # created_at / updated_at come from TimestampMixin.


class OpportunityLinkDigestSend(Base):
    __tablename__ = "opportunity_link_digest_send_log"

    digest_send_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    # Database clock, like ``survey_send_log.sent_at``, so the two ledgers are
    # bucketed into UTC days by the same clock.
    sent_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
