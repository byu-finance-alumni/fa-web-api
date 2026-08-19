"""Service-incident model — the durable dedup state behind API failure alerting.

One row per INCIDENT (a contiguous period of server errors), never one row per
error. At most one row per environment may be open (``resolved_at IS NULL``),
enforced by the partial unique index ``uq_service_incidents_open``; that index is
what makes "one email per incident" hold across concurrent serverless instances,
which share no memory with each other.

The state machine lives in ``app/services/failure_alert.py`` and it drives this
table with raw statements (conditional INSERT ... ON CONFLICT and
UPDATE ... WHERE <column> IS NULL RETURNING) rather than the ORM, because the
whole guarantee rests on those conditions being evaluated by Postgres. This model
exists so the table is registered on ``Base.metadata`` alongside every other
table and is legible to anyone reading ``app/models``.

NOTHING HERE MAY EVER HOLD PII: every column is emailed off-platform when an
incident opens. Paths are route TEMPLATES (``/alumni/{alumni_id}``), and
``error_kind`` is an exception CLASS NAME, never a message.

See migration ``database/migrations/2026-08-18_service_incidents.sql`` (#444).
"""

import datetime

from sqlalchemy import BigInteger, DateTime, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base
from app.models.mixins import TimestampMixin


class ServiceIncident(TimestampMixin, Base):
    __tablename__ = "service_incidents"

    incident_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    # 'production' / 'development'. Scopes the one-open-incident rule, because
    # preview deployments share the dev database.
    environment: Mapped[str] = mapped_column(String(40), nullable=False)
    # First and most recent failure of this incident. Quietness is measured from
    # ``last_failure_at`` — it decides both recovery and reaping a stale row.
    started_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    last_failure_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    # Failures REPORTED, not failures that happened: each instance throttles its
    # own reporting so a flood cannot become a write storm. A floor, not a total.
    failure_count: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    first_path: Mapped[str | None] = mapped_column(String(200))
    last_path: Mapped[str | None] = mapped_column(String(200))
    status_code: Mapped[int | None] = mapped_column(Integer)
    error_kind: Mapped[str | None] = mapped_column(String(100))
    # Claimed (set) BEFORE the email is sent, so a send that dies mid-flight
    # cannot be retried into a second email.
    alert_sent_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    recovery_sent_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    # NULL = open. The partial unique index allows exactly one such row per
    # environment.
    resolved_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    # created_at / updated_at come from TimestampMixin.
