"""Support-contact model (``support_contacts`` table).

Engineer-curated "who to contact" entries shown to logged-in users on the
in-app error screen. The list IS exactly what's displayed (no active flag); the
engineer adds / edits / removes rows. See migration 2026-06-17_support_contacts.
"""

from sqlalchemy import Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base
from app.models.mixins import TimestampMixin


class SupportContact(TimestampMixin, Base):
    __tablename__ = "support_contacts"

    support_contact_id: Mapped[int] = mapped_column(primary_key=True)
    role_label: Mapped[str] = mapped_column(String(100), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    email: Mapped[str] = mapped_column(String(255), nullable=False)
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # created_at / updated_at come from TimestampMixin.
