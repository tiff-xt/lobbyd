"""Accounts and refresh tokens."""

import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, ForeignKey, Index, LargeBinary, Text, func
from sqlalchemy.dialects.postgresql import CITEXT, TIMESTAMP
from sqlalchemy.orm import Mapped, mapped_column

from lobbyd.platform.db import Base
from lobbyd.platform.ids import uuid7

ROLES = ("user", "organizer", "staff", "admin")


class User(Base):
    __tablename__ = "users"
    __table_args__ = (
        CheckConstraint("role IN ('user', 'organizer', 'staff', 'admin')", name="role"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid7)
    # citext: uniqueness and lookups ignore case without lower() everywhere.
    email: Mapped[str] = mapped_column(CITEXT, unique=True)
    display_name: Mapped[str] = mapped_column(Text)
    password_hash: Mapped[str] = mapped_column(Text)
    role: Mapped[str] = mapped_column(Text, server_default="user")
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=func.now()
    )


class RefreshToken(Base):
    """One row per issued refresh token. Only a hash of the token is stored.

    Tokens are single use. Using one issues its replacement in the same
    family. Presenting an already used token means it was copied, so the
    whole family is revoked.
    """

    __tablename__ = "refresh_tokens"
    __table_args__ = (
        Index("refresh_tokens_family_id_idx", "family_id"),
        Index("refresh_tokens_user_id_idx", "user_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid7)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    family_id: Mapped[uuid.UUID]
    token_hash: Mapped[bytes] = mapped_column(LargeBinary, unique=True)
    expires_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True))
    used_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=func.now()
    )
