"""Alert message template model — the owner-editable wording of a Slack alert.

One row per MESSAGE KIND (``security_attack_opening``,
``security_attack_resolved``, ``outage_opening``, ``outage_recovered``), at most
one per key, enforced by ``uq_alert_templates_key``.

⚠️ A ROW HERE IS AN OVERRIDE, NOT THE SOURCE OF TRUTH. Every kind carries a
built-in default in ``app/services/alert_templates.py``; an absent row, an
unreadable table, or a database that has never had the migration applied all mean
"use the default", so this feature can change what an alert SAYS and can never
stop one being sent.

The policy — which kinds exist, which placeholders each may use, how a body is
validated and rendered — lives in ``app/services/alert_templates.py``, which
drives this table with raw statements (``INSERT ... ON CONFLICT DO UPDATE``) in
the style of the neighbouring alerting services. This model exists so the table
is registered on ``Base.metadata`` alongside every other table and is legible to
anyone reading ``app/models``.

NOTHING HERE MAY EVER HOLD PII, and unusually the reason is not what the row
stores but what it CONTROLS: this text is rendered into a Slack message. The
placeholders a body may name are declared in the service and none of them can
reach an attempted email address — ``{addresses}`` is the COUNT. See the PII note
in ``app/services/login_abuse.py``.

See migration ``database/migrations/2026-08-20_alert_templates.sql``.
"""

from sqlalchemy import BigInteger, CheckConstraint, ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base
from app.models.mixins import TimestampMixin


class AlertMessageTemplate(TimestampMixin, Base):
    __tablename__ = "alert_message_templates"
    __table_args__ = (
        # Mirrors the migration. Slack rejects an oversized payload outright, so
        # the cap is the difference between a wordy alert and no alert; the
        # control-character rule keeps a one-sentence message one sentence.
        CheckConstraint(
            "char_length(body) BETWEEN 1 AND 500",
            name="ck_alert_templates_length",
        ),
        CheckConstraint(
            "body ~ '^[^[:cntrl:]]+$'",
            name="ck_alert_templates_visible",
        ),
    )

    template_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    # One of ``alert_templates.KINDS``. Not a FK to an enum table: the set of
    # kinds is a property of the code that renders them, so an unrecognised key
    # is ignored rather than being a broken reference.
    template_key: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    # Literal text plus ``{placeholders}``. Validated on write AND re-validated
    # at render time, so a body inserted by hand still cannot break a message.
    body: Mapped[str] = mapped_column(Text, nullable=False)
    # Who last edited it. The durable record of the edit is the audit trail (an
    # engineer's AuditLog is rerouted to engineer_action_log by the before_flush
    # guard, #199); this column is the convenience shown next to the field.
    updated_by_user_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("users.user_id", ondelete="SET NULL")
    )
    # created_at / updated_at come from TimestampMixin.
