"""Login IP-block model — the durable state behind automatic login blocking.

One row per BLOCKED SOURCE (per environment), never one row per attempt. At most
one un-lifted row per (environment, ip_address), enforced by the partial unique
index ``uq_login_ip_blocks_active``; that index is what collapses concurrent
serverless instances — which share no memory — into a single block row.

The policy and the state machine live in ``app/services/login_block.py`` and
drive this table with raw statements (``INSERT ... SELECT ... WHERE NOT EXISTS``
and ``ON CONFLICT ... DO UPDATE``) rather than the ORM, because the whole
guarantee rests on those conditions being evaluated by Postgres: the two
``NOT EXISTS`` clauses ARE the "never block an address with a recent successful
login" and "never block an engineer's address" safety properties. This model
exists so the table is registered on ``Base.metadata`` alongside every other
table and is legible to anyone reading ``app/models``.

``blocked_until`` is NOT NULL by design — there is no way to spell a permanent
block — and every read carries ``AND blocked_until > now()``, so a block lapses
because time passed rather than because a cleanup job ran.

NOTHING HERE MAY EVER HOLD PII: the row's contents are rendered into a Slack
security alert. Counts and a fixed pattern string only; the ATTEMPTED ADDRESSES
stay in ``login_failures``, behind the engineer console.

See migration ``database/migrations/2026-08-19_login_ip_blocks.sql`` (#457).
"""

import datetime

from sqlalchemy import BigInteger, DateTime, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base
from app.models.mixins import TimestampMixin


class LoginIpBlock(TimestampMixin, Base):
    __tablename__ = "login_ip_blocks"

    block_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    # 'production' / 'development'. Scopes the one-active-block rule, because
    # preview deployments share the dev database.
    environment: Mapped[str] = mapped_column(String(40), nullable=False)
    # ⚠️ CLIENT-SUPPLIED. Copied from ``login_failures.ip_address``, which the
    # frontend fills from the incoming request's ``x-forwarded-for``. See the
    # anti-DoS exemption in app/services/login_block.py.
    ip_address: Mapped[str] = mapped_column(String(64), nullable=False)
    blocked_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    # THE EXPIRY, and the reason no cleanup job is needed.
    blocked_until: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    # Why, snapshotted from the same aggregate the incident row and the alert
    # use, so the three surfaces cannot describe one source three ways.
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    distinct_email_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0
    )
    pattern: Mapped[str | None] = mapped_column(String(64))
    # The login_abuse_incidents row opened alongside this block, when there was
    # one. Not a FK: blocking does not depend on alerting being configured, so a
    # block can legitimately exist with no incident row.
    abuse_incident_id: Mapped[int | None] = mapped_column(BigInteger)
    # Set when an engineer lifts the block. Takes the row out of the partial
    # unique index, and suppresses automatic re-blocking of that source for
    # ``login_block.LIFT_GRACE_SECONDS``.
    lifted_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    lifted_by_user_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("users.user_id", ondelete="SET NULL")
    )
    # created_at / updated_at come from TimestampMixin.
