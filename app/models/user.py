"""Identity and access-control models: ``users``, ``roles``, ``user_roles``.

These map the tables defined in ``database/schema.sql``. Authorization is
resolved from these tables (a user's rows in ``user_roles`` -> ``roles``), never
from a JWT claim — see ``app/api/dependencies/auth.py``.
"""

from __future__ import annotations

import datetime
import uuid

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base
from app.models.mixins import TimestampMixin


class User(TimestampMixin, Base):
    __tablename__ = "users"

    user_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    # Supabase Auth user id (the JWT `sub`). The link between an auth identity
    # and this application's user/role records.
    auth_user_id: Mapped[uuid.UUID] = mapped_column(Uuid, unique=True, nullable=False)
    first_name: Mapped[str | None] = mapped_column(String(100))
    last_name: Mapped[str | None] = mapped_column(String(100))
    email: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("true")
    )
    auth_provider: Mapped[str | None] = mapped_column(String(50))
    last_login_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    # Force a password change on next login. Set true when an account is created
    # with a temp password or when a super_admin resets a password; the user
    # clears it via POST /auth/password/complete after setting their own
    # password client-side (see app/api/routes/auth.py). Authoritative for the
    # frontend's force-change gate — never a JWT claim.
    must_change_password: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    # Hard account lock set after too many failed logins (see
    # app/services/login_lockout.py). While ``locked_at`` is non-null the account
    # is denied at the pre-login precheck regardless of credentials; only a
    # super_admin password reset clears it.
    locked_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    locked_reason: Mapped[str | None] = mapped_column(Text)

    user_roles: Mapped[list[UserRole]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )
    # Convenience read-only view of the assigned roles.
    roles: Mapped[list[Role]] = relationship(
        secondary="user_roles", viewonly=True, order_by="Role.role_name"
    )


class Role(TimestampMixin, Base):
    __tablename__ = "roles"

    role_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    role_name: Mapped[str] = mapped_column(String(100), unique=True, nullable=False)
    role_description: Mapped[str | None] = mapped_column(Text)

    user_roles: Mapped[list[UserRole]] = relationship(
        back_populates="role", cascade="all, delete-orphan"
    )


class UserRole(Base):
    __tablename__ = "user_roles"
    __table_args__ = (UniqueConstraint("user_id", "role_id", name="uq_user_roles"),)

    user_role_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    user_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("users.user_id", ondelete="CASCADE"),
        nullable=False,
    )
    role_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("roles.role_id", ondelete="CASCADE"),
        nullable=False,
    )
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    user: Mapped[User] = relationship(back_populates="user_roles")
    role: Mapped[Role] = relationship(back_populates="user_roles")
