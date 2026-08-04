"""Maintenance-mode model.

A SINGLE-ROW table (``id`` pinned to 1, same shape as ``survey_send_config``)
holding the site-wide maintenance switch. When ``enabled`` is true:

  * every non-exempt authenticated request is refused with 503 /
    ``maintenance_mode`` (see ``app/api/dependencies/auth.py``),
  * ``POST /auth/login`` refuses to record/claim a non-exempt sign-in, so a
    paused user cannot re-establish a session, and
  * the public ``GET /maintenance/status`` reports ``enabled`` + ``message`` so
    the frontend can render a maintenance page to logged-out visitors.

Users holding the ``engineer`` role are EXEMPT from all three, which is what
makes the switch reversible — see ``app/services/maintenance.py`` for the full
rationale.

``message`` is engineer-authored copy shown to the public; it must never be used
to carry internal detail. ``enabled_by_user_id`` / ``enabled_at`` are for the
engineer console only and are NEVER included in the public status payload.

See migration ``database/migrations/2026-08-03_maintenance_mode.sql``.
"""

import datetime

from sqlalchemy import BigInteger, Boolean, DateTime, ForeignKey, Integer, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base
from app.models.mixins import TimestampMixin


class MaintenanceMode(TimestampMixin, Base):
    __tablename__ = "maintenance_mode"

    # Pinned to 1 by a CHECK constraint — there is only ever one config row.
    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    # The switch. False = normal operation.
    enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False
    )
    # PUBLIC copy shown on the maintenance page. NULL falls back to the default
    # message in app/services/maintenance.py.
    message: Mapped[str | None] = mapped_column(Text)
    # When the CURRENT window was turned on (NULL while disabled). Engineer-only.
    enabled_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    # Who turned it on/off last. Engineer-only — never surfaced publicly.
    enabled_by_user_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("users.user_id", ondelete="SET NULL")
    )
    # created_at / updated_at come from TimestampMixin.
