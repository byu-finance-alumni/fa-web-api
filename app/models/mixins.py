"""Reusable column mixins for ORM models.

These mirror the conventions in ``database/schema.sql`` — ``created_at`` /
``updated_at`` are ``timestamptz`` columns that default to ``now()`` in the
database. ``updated_at`` also bumps on ORM-side updates so the application layer
doesn't have to set it by hand.
"""

import datetime

from sqlalchemy import DateTime, func
from sqlalchemy.orm import Mapped, mapped_column


class TimestampMixin:
    """Adds ``created_at`` and ``updated_at`` timestamptz columns."""

    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )
