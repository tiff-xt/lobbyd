"""Orders."""

import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, ForeignKey, Index, Integer, String, Text, func
from sqlalchemy.dialects.postgresql import TIMESTAMP
from sqlalchemy.orm import Mapped, mapped_column

from lobbyd.platform.db import Base
from lobbyd.platform.ids import uuid7


class Order(Base):
    __tablename__ = "orders"
    __table_args__ = (
        CheckConstraint(
            "status IN ('pending', 'paid', 'expired', 'cancelled', 'refunded')", name="status"
        ),
        CheckConstraint("total_cents >= 0", name="total"),
        Index("orders_user_id_idx", "user_id", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid7)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"))
    tournament_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tournaments.id"))
    # One order per hold, enforced by the database: a double submit that got
    # past everything else still cannot create a second order.
    hold_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("holds.id"), unique=True)
    status: Mapped[str] = mapped_column(Text, server_default="pending")
    total_cents: Mapped[int] = mapped_column(Integer)
    currency: Mapped[str] = mapped_column(String(3))
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=func.now()
    )
    paid_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))


class OrderItem(Base):
    __tablename__ = "order_items"
    __table_args__ = (
        CheckConstraint("quantity >= 1", name="quantity"),
        Index("order_items_order_id_idx", "order_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid7)
    order_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("orders.id", ondelete="CASCADE"))
    ticket_type_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("ticket_types.id"))
    seat_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("seats.id"))
    quantity: Mapped[int] = mapped_column(Integer)
    # Copied at purchase, so a later price change never rewrites history.
    unit_price_cents: Mapped[int] = mapped_column(Integer)
