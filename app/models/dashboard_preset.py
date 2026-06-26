"""Dashboard quick-filter preset model (``dashboard_presets`` table).

Engineer / super-admin-curated "quick filter" presets shown on the dashboard's
Quick search tab. Each row is a label + a relative deep-link (``href``) into the
app (e.g. ``/alumni?cfa=1&city=Salt%20Lake%20City``). The list IS exactly what's
displayed (no active flag); admins add / edit / reorder / remove rows. See
migration 2026-06-26_dashboard_presets.
"""

from sqlalchemy import Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base
from app.models.mixins import TimestampMixin


class DashboardPreset(TimestampMixin, Base):
    __tablename__ = "dashboard_presets"

    dashboard_preset_id: Mapped[int] = mapped_column(primary_key=True)
    label: Mapped[str] = mapped_column(String(200), nullable=False)
    # Relative in-app deep link, e.g. "/alumni?cfa=1&state=UT". Validated to a
    # relative path (starts with a single "/") so a preset can't point off-site.
    href: Mapped[str] = mapped_column(String(500), nullable=False)
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # created_at / updated_at come from TimestampMixin.
