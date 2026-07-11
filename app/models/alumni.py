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
    # graduation_month is retained physically but no longer exposed in the API
    # read schema (superseded by graduation_semester + graduation_class below).
    graduation_month: Mapped[int | None] = mapped_column(Integer)
    # Semester + graduating class replace the raw month in the API surface.
    # graduation_semester is one of Fall / Winter / Spring / Summer; the
    # graduation_class is the graduating cohort/class, which is DISTINCT from
    # graduation_year (they usually match but need not).
    graduation_semester: Mapped[str | None] = mapped_column(String(20))
    graduation_class: Mapped[int | None] = mapped_column(Integer)
    finance_program_year: Mapped[int | None] = mapped_column(Integer)
    graduate_degree: Mapped[str | None] = mapped_column(String(100))

    # Survey / demographics captured on the alumni survey. All optional/nullable
    # additive fields. Short single-value fields are varchar; other_designations
    # is free-text (multiple designations, e.g. "Series 7, Series 63").
    # home_country is the country of ORIGIN (distinct from the current-address
    # country on the contact record). employment_status is person-level status
    # (Employed / Unemployed / Retired / Student / Seeking, ...).
    citizenship: Mapped[str | None] = mapped_column(String(100))
    marital_status: Mapped[str | None] = mapped_column(String(50))
    # Home town of ORIGIN (the "Hometown" line on the profile, #366) — paired with
    # home_country (country of origin), distinct from the current-address city.
    hometown: Mapped[str | None] = mapped_column(String(100))
    home_country: Mapped[str | None] = mapped_column(String(100))
    employment_status: Mapped[str | None] = mapped_column(String(50))
    other_designations: Mapped[str | None] = mapped_column(Text)
    survey_completed_date: Mapped[datetime.date | None] = mapped_column(Date)

    # Manual-edit provenance for the profile ("Profile updated by Amy"): the date
    # of the last manual profile update and the user who made it. The FK points
    # to users.user_id ON DELETE SET NULL, mirroring the spouse_alumni_id pattern
    # so deleting the updater just clears the pointer.
    profile_updated_date: Mapped[datetime.date | None] = mapped_column(Date)
    profile_updated_by_user_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("users.user_id", ondelete="SET NULL")
    )
    # Free-text "updated by" NAME captured from the intake sheet (the person who
    # updated the profile, as typed on the sheet). DISTINCT from
    # profile_updated_by_user_id, which is the resolved app-user FK — this holds
    # the raw name and backs the "Profile updated by ..." hover when no user FK
    # is linked.
    profile_updated_by: Mapped[str | None] = mapped_column(String(200))

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

    # Friends of the finance program (#218). ``is_alumni = false`` marks a
    # non-alumnus contact ("friend") stored in this same table so they reuse all
    # detail tables, search, and map shading. Defaults to true so every existing
    # row and any payload that omits the flag stays an alumnus.
    is_alumni: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("true")
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
