"""Messages sent to users."""

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import ForeignKey, Text, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import JSONB, TIMESTAMP
from sqlalchemy.orm import Mapped, mapped_column

from lobbyd.platform.db import Base
from lobbyd.platform.ids import uuid7


class Notification(Base):
    __tablename__ = "notifications"
    __table_args__ = (
        # At most one message of each kind about each thing. This constraint
        # is what makes the email handlers safe to run twice.
        UniqueConstraint("kind", "subject_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid7)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    kind: Mapped[str] = mapped_column(Text)
    subject_id: Mapped[uuid.UUID]  # the order the message is about
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=func.now()
    )
