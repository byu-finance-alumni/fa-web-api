"""Alumni core model.

Maps the ``alumni`` table from ``database/schema.sql``. Related detail tables
(contact info, employment, education, tags, engagement, ...) are modeled
separately as they're built out. ``source_id`` is a nullable provenance pointer
to ``data_sources`` (modeled later); the FK is declared by table name so it
holds without that model being imported yet.
"""

from __future__ import annotations

import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base
from app.models.mixins import TimestampMixin


class Alumni(TimestampMixin, Base):
    __tablename__ = "alumni"

    alumni_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    source_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("data_sources.source_id", ondelete="SET NULL")
    )

    # External identifiers.
    byu_id: Mapped[str | None] = mapped_column(String(50))
    mst_id: Mapped[str | None] = mapped_column(String(50))
    net_id: Mapped[str | None] = mapped_column(String(50))

    # Names.
    first_name: Mapped[str | None] = mapped_column(String(100))
    middle_name: Mapped[str | None] = mapped_column(String(100))
    last_name: Mapped[str | None] = mapped_column(String(100))
    preferred_first_name: Mapped[str | None] = mapped_column(String(100))
    birth_name: Mapped[str | None] = mapped_column(String(100))

    # Demographics / program.
    gender: Mapped[str | None] = mapped_column(String(30))
    birth_year: Mapped[int | None] = mapped_column(Integer)
    birth_date: Mapped[datetime.date | None] = mapped_column(Date)
    graduation_year: Mapped[int | None] = mapped_column(Integer)
    finance_program_year: Mapped[int | None] = mapped_column(Integer)
    graduate_degree: Mapped[str | None] = mapped_column(String(100))

    # Secondary affiliation / education (#47, PRD section 6). All optional/
    # nullable additive fields that extend the alumni record beyond the core
    # program/employment fields. Short single-value fields are varchar; the
    # narrative ones (free-text descriptions of involvement / roles) are text.
    mba_program: Mapped[str | None] = mapped_column(String(255))
    law_school: Mapped[str | None] = mapped_column(String(255))
    medical_school: Mapped[str | None] = mapped_column(String(255))
    graduate_school: Mapped[str | None] = mapped_column(String(255))
    startup_involvement: Mapped[str | None] = mapped_column(Text)
    advisory_roles: Mapped[str | None] = mapped_column(Text)
    secondary_employment: Mapped[str | None] = mapped_column(Text)

    # Spouse. Free-text name + birthday; spouse_alumni_id links to another
    # alumni record when the spouse is also an alumnus (self-referential FK,
    # ON DELETE SET NULL so deleting the spouse just clears the pointer).
    spouse_first_name: Mapped[str | None] = mapped_column(String(100))
    spouse_last_name: Mapped[str | None] = mapped_column(String(100))
    spouse_birth_date: Mapped[datetime.date | None] = mapped_column(Date)
    spouse_alumni_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("alumni.alumni_id", ondelete="SET NULL")
    )

    deceased: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    linkedin_url: Mapped[str | None] = mapped_column(String(500))
    notes: Mapped[str | None] = mapped_column(Text)

    # Soft-delete + import/edit provenance (manual edits win over imports).
    archived: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    manually_edited_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    last_imported_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
