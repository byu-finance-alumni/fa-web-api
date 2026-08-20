"""Alert-delivery mode model — Slack only, or Slack and e-mail (#458).

A SINGLE-ROW table (``id`` pinned to 1, the same shape as ``maintenance_mode``
and ``survey_send_config``) holding one engineer-settable choice: whether a
failure/security alert goes to Slack alone, or to Slack AND the alert mailbox
every time.

WHY A TABLE AND NOT AN ENVIRONMENT VARIABLE. The owner's requirement was that he
can change this without a redeploy, and an env var on Vercel needs one. It also
has to be a fact about the SERVICE rather than about one invocation: this API
runs on Vercel serverless, where a module-level variable dies with the
invocation and the twenty instances handling an outage share no memory with each
other. Postgres is the only durable store this stack already has a connection
to. Same argument as ``service_incidents``, ``login_ip_blocks`` and the
maintenance switch, and it is written out in full in the migration header.

``mode`` is one of ``app.services.alert_delivery.MODES``, enforced by a CHECK
constraint so an unknown value cannot be stored at all — and, belt and braces,
the service maps anything it does not recognise back to the default when it
reads. ``updated_by_user_id`` / ``updated_at`` are engineer-console detail.

⚠️ NEITHER MODE CAN PRODUCE SILENCE. The e-mail backstop survives both settings
— in "Slack only" a failed or unconfigured Slack post still falls through to
e-mail. The switch chooses whether e-mail is a COPY or a BACKSTOP; it can never
choose "no channel at all". The invariant lives in
``app/services/failure_alert.deliver_alert``, which is where it is tested.

See migration ``database/migrations/2026-08-19_alert_delivery_config.sql``.
"""

from sqlalchemy import BigInteger, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base
from app.models.mixins import TimestampMixin


class AlertDeliveryConfig(TimestampMixin, Base):
    __tablename__ = "alert_delivery_config"

    # Pinned to 1 by a CHECK constraint — there is only ever one config row.
    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    # 'slack_only' (default) or 'slack_and_email'. A CHECK constraint in the
    # migration is the authority on the permitted values.
    mode: Mapped[str] = mapped_column(
        String(20), nullable=False, default="slack_only"
    )
    # Who last changed it. Engineer-console detail; the durable record of the
    # change itself is the audit trail (``set_alert_delivery_mode``).
    updated_by_user_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("users.user_id", ondelete="SET NULL")
    )
    # created_at / updated_at come from TimestampMixin.
