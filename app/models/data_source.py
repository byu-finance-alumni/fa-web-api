"""Data provenance model: ``data_sources``.

Every alumni-related row can point at the source it came from (an import file, a
manual entry, mock data, ...). Mock/dev data is tagged with a dedicated source
so it can be removed in one cascade — see ``scripts/seed_mock_data.py``.
"""

import datetime

from sqlalchemy import BigInteger, DateTime, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base
from app.models.mixins import TimestampMixin


class DataSource(TimestampMixin, Base):
    __tablename__ = "data_sources"

    source_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    source_name: Mapped[str] = mapped_column(String(255), nullable=False)
    source_type: Mapped[str | None] = mapped_column(String(100))
    source_description: Mapped[str | None] = mapped_column(Text)
    imported_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
