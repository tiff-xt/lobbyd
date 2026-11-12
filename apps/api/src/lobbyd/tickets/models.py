"""Issued tickets."""

import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, ForeignKey, Index, Text, func, text
from sqlalchemy.dialects.postgresql import TIMESTAMP
from sqlalchemy.orm import Mapped, mapped_column

from lobbyd.platform.db import Base
from lobbyd.platform.ids import uuid7


class Ticket(Base):
    __tablename__ = "tickets"
    __table_args__ = (
        CheckConstraint("status IN ('valid', 'void')", name="status"),
        # Second line of defence against a double sale: whatever went wrong
        # upstream, two valid tickets for one seat cannot be stored.
        Index(
            "tickets_one_per_seat_idx",
            "tournament_id",
            "seat_id",
            unique=True,
            postgresql_where=text("status = 'valid' AND seat_id IS NOT NULL"),
        ),
        Index("tickets_order_id_idx", "order_id"),
        Index("tickets_holder_id_idx", "holder_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid7)
    # Plain columns, not foreign keys: orders sits above this module.
    order_id: Mapped[uuid.UUID]
    order_item_id: Mapped[uuid.UUID]
    tournament_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tournaments.id"))
    ticket_type_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("ticket_types.id"))
    seat_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("seats.id"))
    holder_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"))
    status: Mapped[str] = mapped_column(Text, server_default="valid")
    checked_in_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    issued_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), server_default=func.now())
