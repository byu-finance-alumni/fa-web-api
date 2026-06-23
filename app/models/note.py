"""Unified notes ORM model.

One ``notes`` table backs free-text notes attached at three levels — an alumni
profile, a single interaction, or an event. The attach target is modelled as
three nullable foreign keys with a DB ``CHECK`` that EXACTLY ONE is set, so the
"unified" surface keeps real referential integrity (and ``ON DELETE CASCADE``
per target) rather than a loose polymorphic ``(entity_type, entity_id)`` pair.
The API exposes the unified ``entity_type`` / ``entity_id`` shape and maps it to
the right column.
"""

from sqlalchemy import BigInteger, CheckConstraint, ForeignKey, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base
from app.models.mixins import TimestampMixin


class Note(TimestampMixin, Base):
    __tablename__ = "notes"
    __table_args__ = (
        # Exactly one attach target. Mirrors the migration's CHECK so the ORM
        # metadata and the DB agree.
        CheckConstraint(
            "num_nonnulls(alumni_id, interaction_id, event_id) = 1",
            name="ck_notes_single_target",
        ),
        CheckConstraint(
            "char_length(body) <= 10000",
            name="ck_notes_body_length",
        ),
    )

    note_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)

    # Exactly one of these is non-null (enforced by the CHECK above). Each
    # cascades so a note never outlives its parent record.
    alumni_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("alumni.alumni_id", ondelete="CASCADE")
    )
    interaction_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("interactions.interaction_id", ondelete="CASCADE"),
    )
    event_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("events.event_id", ondelete="CASCADE")
    )

    body: Mapped[str] = mapped_column(Text, nullable=False)

    # Authorship. SET NULL so deleting a user keeps the note (FERPA: the audit
    # trail snapshots actor identity separately).
    created_by_user_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("users.user_id", ondelete="SET NULL")
    )
    updated_by_user_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("users.user_id", ondelete="SET NULL")
    )
    # created_at / updated_at come from TimestampMixin.
