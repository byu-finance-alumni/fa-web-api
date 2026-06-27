"""Role-capability grant model (``role_capabilities`` table, #164).

Each row is one capability granted to one role. The PRESENCE of a row means the
role holds that capability; removing the row revokes it. The capability codes
themselves are defined in code (``app/core/capabilities.py``) — this table only
stores which roles hold which capabilities, editable by the engineer in the
permission editor. See migration ``2026-06-26_role_capabilities``.
"""

from sqlalchemy import BigInteger, ForeignKey, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base
from app.models.mixins import CreatedAtMixin


class RoleCapability(CreatedAtMixin, Base):
    __tablename__ = "role_capabilities"
    __table_args__ = (
        UniqueConstraint(
            "role_id", "capability_code", name="uq_role_capabilities"
        ),
    )

    role_capability_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    role_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("roles.role_id", ondelete="CASCADE"),
        nullable=False,
    )
    # A capability code from app/core/capabilities.py (e.g. "alumni.full"). Kept
    # a plain string (not an FK) because the canonical list lives in code, not a
    # table — see the module docstring there.
    capability_code: Mapped[str] = mapped_column(String(100), nullable=False)
    # created_at comes from CreatedAtMixin.
