"""The new-people survey (api#661): one shared link, and the answers it holds.

``new_people_survey_link``
    A SINGLE-ROW table (``id`` pinned to 1) holding the secret in the public URL
    and the on/off switch. The API mints the key; reissuing replaces it, which
    kills the old URL at once. A disabled link answers exactly like a wrong key.

``new_people_submissions``
    One row per submission from that link, HELD for staff review. Nobody is
    added to ``alumni`` until a reviewer approves the row. ``payload`` and
    ``comments`` are PII while the row is pending and are purged (set to NULL)
    as soon as it is approved or rejected; a CHECK constraint enforces it.

These rows are deliberately NOT in ``alumni`` or ``survey_responses``, so held
people never count toward alumni totals, KPIs, campaign tallies or the normal
survey review queue. Only ``services.new_people_survey`` reads them.

See migration ``database/migrations/2026-11-10_new_people_survey.sql``.
"""

import datetime

from sqlalchemy import BigInteger, Boolean, DateTime, ForeignKey, Integer, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base
from app.models.mixins import TimestampMixin

NEW_PERSON_KINDS = ("alum", "friend")

STATUS_PENDING = "pending"
STATUS_APPROVED = "approved"
STATUS_REJECTED = "rejected"


class NewPeopleSurveyLink(TimestampMixin, Base):
    __tablename__ = "new_people_survey_link"

    # Pinned to 1 by a CHECK constraint: there is only ever one shared link.
    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    # The secret in the public URL. Random and URL-safe, 32+ characters.
    link_key: Mapped[str] = mapped_column(String(128), nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    updated_by_user_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("users.user_id", ondelete="SET NULL")
    )
    # created_at / updated_at come from TimestampMixin.


class NewPersonSubmission(Base):
    __tablename__ = "new_people_submissions"

    submission_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    # `alum` (BYU Alumni Professional) or `friend` (Non-BYU Alumni Professional).
    kind: Mapped[str] = mapped_column(String(10), nullable=False)
    # Cleaned answers keyed by survey field key. NULL once decided (purged).
    # ⚠️ none_as_null=True is REQUIRED: without it SQLAlchemy writes Python None
    # as the JSON literal `null`, which is NOT SQL NULL, and the purge violates
    # ck_new_people_submissions_payload_state on real Postgres (fake-session
    # tests can't see this).
    payload: Mapped[dict | None] = mapped_column(JSONB(none_as_null=True))
    comments: Mapped[str | None] = mapped_column(Text)
    # Clamped by the service before it lands here; NULL = not measured.
    fill_seconds: Mapped[int | None] = mapped_column(Integer)
    submitted_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    status: Mapped[str] = mapped_column(String(20), nullable=False, default=STATUS_PENDING)
    decided_by_user_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("users.user_id", ondelete="SET NULL")
    )
    decided_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True))
    # The record an approved submission created or was merged into.
    alumni_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("alumni.alumni_id", ondelete="SET NULL")
    )


class NewPeopleInvite(Base):
    """One new-people survey invite email that Resend accepted (api#661 add-on).

    The LEDGER behind two rules: the shared Resend budget
    (``survey_email.get_send_usage`` counts these rows) and the 30-day
    "recently invited" skip. NO ADDRESS is stored: ``email_hash`` is the sha256
    hex of the trimmed, lower-cased address.

    See migration ``database/migrations/2026-11-10_new_people_invites.sql``.
    """

    __tablename__ = "new_people_invites"

    invite_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    email_hash: Mapped[str] = mapped_column(Text, nullable=False)
    sent_by_user_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("users.user_id", ondelete="SET NULL")
    )
    # Database clock, like the other two usage ledgers, so all three are
    # bucketed into UTC days by the same clock.
    sent_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    resend_email_id: Mapped[str | None] = mapped_column(Text)
