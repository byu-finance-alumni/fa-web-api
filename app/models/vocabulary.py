"""Controlled-vocabulary model (``vocabulary_terms`` table).

A single generic lookup store for editable dropdown values (#82): each row is one
option in a category (e.g. ``industry``, ``event_type``). Engineer/super_admin
manage these at runtime; the rest of the app reads the active terms to populate
dropdowns and to validate writes. Deactivation (``active=false``) is a soft
delete — the value stays valid for existing records but is hidden from new entry.
"""

import datetime

from sqlalchemy import Boolean, DateTime, Integer, String, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class VocabularyTerm(Base):
    __tablename__ = "vocabulary_terms"
    __table_args__ = (
        UniqueConstraint("category", "value", name="uq_vocabulary_terms_category_value"),
    )

    term_id: Mapped[int] = mapped_column(primary_key=True)
    category: Mapped[str] = mapped_column(String(50), nullable=False)
    value: Mapped[str] = mapped_column(String(100), nullable=False)
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
