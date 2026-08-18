"""Alumni-submitted internship / job opportunity links (#441).

One row per opening. Many rows per alum — which is exactly why this is a table
and not a survey field: every other survey answer maps to one existing column
(the survey's `_FIELDS` keys are literally `table.column`), and an opening has a
url, a location, a role type, a deadline and a description of its own. There is
no column to map it to, so it gets its own table and its own write path, outside
the response review queue.

Two write paths, two landing states:

  * ``source='survey'`` — PUBLIC, token-gated. Lands ``pending``; a staff member
    must approve it before it is treated as real.
  * ``source='staff'`` — a staff member typing it in IS the review, so it lands
    ``approved`` with the reviewer stamped as themselves.

Moderation is per LINK and has its own endpoints. It deliberately does NOT reuse
the survey response queue, which applies or rejects a whole submission at once
and so cannot express "approve the address change, reject the link".

``url`` is attacker-supplied on the survey path and is rendered as a clickable
href to a signed-in staff member. The validator that gates it lives in
``app/services/opportunity_links.py``; read its docstring before touching this
model or widening a column.
"""

from __future__ import annotations

import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    String,
    Text,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base

# Column widths, mirrored from database/schema.sql. These are imported by the
# schemas and the service so "what the column holds" has ONE definition — a
# public writer must never be able to stage a value the column would reject at
# apply time (or, worse, one it would silently accept and bloat).
URL_MAX = 2048
COMPANY_NAME_MAX = 255
CITY_MAX = 100
STATE_MAX = 100
# Same width as city/state, and the same width as `current_employment.current_country`
# already uses — a location field is a location field, and a country that would not
# fit the column the rest of the app stores countries in is not a country.
COUNTRY_MAX = 100
DETAILS_MAX = 2000

# The moderation states. `pending` is where a survey submission lands; `approved`
# is where staff entry lands and where moderation moves a good submission.
STATUSES: tuple[str, ...] = ("pending", "approved", "rejected")

# Where the row came from. Not cosmetic: it is what says whether the row's text
# was written by the public or by a signed-in staff member.
SOURCES: tuple[str, ...] = ("survey", "staff")

# The role types the owner specified: Internship / Full-time / Both.
ROLE_TYPES: tuple[str, ...] = ("internship", "full_time", "both")


class OpportunityLink(Base):
    __tablename__ = "opportunity_links"
    __table_args__ = (
        # Each mirrors the migration's CHECK so the ORM metadata and the DB agree.
        CheckConstraint(
            "status IN ('pending', 'approved', 'rejected')",
            name="ck_opportunity_links_status",
        ),
        CheckConstraint(
            "source IN ('survey', 'staff')", name="ck_opportunity_links_source"
        ),
        CheckConstraint(
            "role_type IN ('internship', 'full_time', 'both')",
            name="ck_opportunity_links_role_type",
        ),
        # Exactly one company identity — see `is_own_company` below.
        CheckConstraint(
            "(is_own_company AND company_name IS NULL)"
            " OR (NOT is_own_company AND company_name IS NOT NULL)",
            name="ck_opportunity_links_company",
        ),
        CheckConstraint(
            f"details IS NULL OR char_length(details) <= {DETAILS_MAX}",
            name="ck_opportunity_links_details_length",
        ),
        CheckConstraint(
            f"char_length(url) BETWEEN 1 AND {URL_MAX}",
            name="ck_opportunity_links_url_length",
        ),
    )

    opportunity_link_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)

    # The alum the opportunity came from. NOT NULL even for staff entry — a link
    # with no alumnus behind it has no provenance, and provenance is the only
    # reason to trust an unvetted URL enough to look at it.
    alumni_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("alumni.alumni_id", ondelete="CASCADE"),
        nullable=False,
    )

    # "This is my company." When True the display name is resolved at READ time
    # from the alum's `current_employment.current_employer`, and `company_name`
    # stays NULL — one fact, one home, so an alum who changes employer does not
    # leave a stale company label on their entries. When False a name was typed
    # and is required (DB CHECK above).
    is_own_company: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False
    )
    company_name: Mapped[str | None] = mapped_column(String(COMPANY_NAME_MAX))

    url: Mapped[str] = mapped_column(String(URL_MAX), nullable=False)

    location_city: Mapped[str | None] = mapped_column(String(CITY_MAX))
    location_state: Mapped[str | None] = mapped_column(String(STATE_MAX))
    # Nullable, and deliberately NOT defaulted to "United States". Both entry
    # forms grew an "outside the United States" mode, so a country can now be
    # stated — but a row where nobody stated one is genuinely unknown, and
    # inventing a value would turn "we never asked" into "we were told". The
    # pre-existing rows were written before the field existed; they stay NULL.
    location_country: Mapped[str | None] = mapped_column(String(COUNTRY_MAX))

    role_type: Mapped[str] = mapped_column(String(20), nullable=False)
    application_deadline: Mapped[datetime.date | None] = mapped_column(Date)
    details: Mapped[str | None] = mapped_column(Text)

    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default="pending"
    )
    source: Mapped[str] = mapped_column(String(20), nullable=False)

    submitted_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )

    # The staff member who typed a manual entry; NULL on the survey path (there
    # is no logged-in actor there). SET NULL so deleting a user never deletes the
    # opportunity — the FERPA trail snapshots actor identity separately.
    created_by_user_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("users.user_id", ondelete="SET NULL")
    )
    reviewed_by_user_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("users.user_id", ondelete="SET NULL")
    )
    reviewed_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
