"""Survey response model (survey_responses table).

An alum's staged "confirm your info" submission, awaiting admin review. `payload`
holds the submitted values keyed by survey field keys (`table.column`).
"""

import datetime

from sqlalchemy import BigInteger, DateTime, ForeignKey, Integer, String, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class SurveyResponse(Base):
    __tablename__ = "survey_responses"

    survey_response_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    alumni_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("alumni.alumni_id", ondelete="CASCADE"), nullable=False
    )
    graduation_year: Mapped[int | None] = mapped_column(Integer)
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="pending")
    # Staging key of a NEW profile photo uploaded with this response (headshots
    # bucket, `survey-pending/<id>`), pending admin review. None when no photo.
    staged_photo_path: Mapped[str | None] = mapped_column(String(255))
    submitted_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    reviewed_by_user_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("users.user_id", ondelete="SET NULL")
    )
    reviewed_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True))
