"""Survey email copy model — the staff-editable wording of the alumni survey email.

A SINGLE-ROW table (``id`` pinned to 1, the same shape as ``survey_send_config``
and ``maintenance_mode``) holding the subject / intro / closing the annual
"confirm your info" email is built from, plus which of the "here's what we have
on file" rows it shows.

⚠️ A ROW HERE IS AN OVERRIDE, NOT THE SOURCE OF TRUTH — the same contract as
``alert_message_templates``. The Career Directors' authored copy is compiled into
``app/services/survey_message.py`` (``DEFAULT_SUBJECT`` / ``DEFAULT_INTRO`` /
``DEFAULT_CLOSING`` / ``ON_FILE_FIELDS``); an absent row, a blank column, an
unreadable table or a database that has never had the migration applied all mean
"use the default". A feature that lets staff change what the email SAYS must not
be able to stop one being sent, or send an empty one.

``is_customized`` is therefore decided by COMPARING the stored copy against those
defaults, never by asking whether a row exists.

Before this table existed (#524) the "Edit email message" box on the Needs
Surveying page saved to browser ``localStorage`` and nothing else: the edits were
per-browser, per-machine, invisible to the other Career Director, and reached no
alum at all — the send used the module constants regardless.

``on_file_fields`` is a SUBSET of :data:`app.services.survey_message.ON_FILE_FIELDS`,
validated on write and re-filtered at render time, so this column can only ever
hide rows the email already knew how to build. It can never introduce a field,
which is what keeps the form / email picker / sample-survey lists in agreement.

See migration ``database/migrations/2026-09-09_survey_email_message.sql``.
"""

from sqlalchemy import BigInteger, CheckConstraint, ForeignKey, Integer, Text
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base
from app.models.mixins import TimestampMixin


class SurveyEmailMessage(TimestampMixin, Base):
    __tablename__ = "survey_email_message"
    __table_args__ = (
        # Mirrors the migration. The caps are generous — this is a whole email
        # body, not a Slack line — but they are still caps: Resend rejects an
        # oversized payload, and an email lost to a 400 is worse than a wordy one.
        CheckConstraint("id = 1", name="ck_survey_email_message_singleton"),
        CheckConstraint(
            "char_length(subject) BETWEEN 1 AND 200",
            name="ck_survey_email_message_subject_len",
        ),
        CheckConstraint(
            "char_length(intro) BETWEEN 1 AND 5000",
            name="ck_survey_email_message_intro_len",
        ),
        CheckConstraint(
            "char_length(closing) BETWEEN 1 AND 5000",
            name="ck_survey_email_message_closing_len",
        ),
    )

    # Pinned to 1 by a CHECK constraint — there is only ever one copy row.
    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    # The email's subject line. Single line by validation (no control chars).
    subject: Mapped[str] = mapped_column(Text, nullable=False)
    # The paragraph(s) above the on-file box. Blank lines separate paragraphs.
    intro: Mapped[str] = mapped_column(Text, nullable=False)
    # The paragraph(s) below the button, sign-off included.
    closing: Mapped[str] = mapped_column(Text, nullable=False)
    # Which on-file rows the email shows, by LABEL. Always a subset of
    # ``survey_message.ON_FILE_FIELDS`` and stored in that canonical order.
    on_file_fields: Mapped[list[str]] = mapped_column(
        ARRAY(Text), nullable=False, default=list
    )
    # Who last edited it. Nullable / ON DELETE SET NULL: the copy must survive
    # the account of whoever typed it being removed. The durable record of the
    # edit is the audit trail (an engineer's AuditLog is rerouted into
    # engineer_action_log by the before_flush guard, #199), not this column.
    updated_by_user_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("users.user_id", ondelete="SET NULL")
    )
    # created_at / updated_at come from TimestampMixin.
