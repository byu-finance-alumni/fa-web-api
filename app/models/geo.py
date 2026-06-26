"""City -> lat/lng crosswalk (``city_geo`` table) for radius/proximity search.

Non-sensitive public reference data (US Census places). Keys are normalized:
``city_norm`` = lower(trim(city)), ``state`` = upper 2-letter code. Populated by
the ``2026-06-25_city_geo_crosswalk.sql`` migration from the frontend crosswalk.
"""

from __future__ import annotations

from sqlalchemy import Float, String
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class CityGeo(Base):
    __tablename__ = "city_geo"

    city_norm: Mapped[str] = mapped_column(String, primary_key=True)
    state: Mapped[str] = mapped_column(String(2), primary_key=True)
    lat: Mapped[float] = mapped_column(Float, nullable=False)
    lng: Mapped[float] = mapped_column(Float, nullable=False)
