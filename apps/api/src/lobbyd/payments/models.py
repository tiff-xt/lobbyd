"""Payments and the provider events we have accepted."""

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import CheckConstraint, Index, Integer, String, Text, func, text
from sqlalchemy.dialects.postgresql import JSONB, TIMESTAMP
from sqlalchemy.orm import Mapped, mapped_column

from lobbyd.platform.db import Base
from lobbyd.platform.ids import uuid7


class Payment(Base):
    __tablename__ = "payments"
    __table_args__ = (
        CheckConstraint(
            "status IN ('created', 'processing', 'succeeded', 'failed', "
            "'refund_pending', 'refunded')",
            name="status",
        ),
        CheckConstraint("amount_cents > 0", name="amount"),
        # Finds payments still waiting on the provider, for the status poll.
        Index(
            "payments_unsettled_idx",
            "updated_at",
            postgresql_where=text("status IN ('created', 'processing', 'refund_pending')"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid7)
    # One payment per order. A plain column: orders sits above this module.
    order_id: Mapped[uuid.UUID] = mapped_column(unique=True)
    # The provider's id. Null until the provider has acknowledged the payment.
    provider_ref: Mapped[str | None] = mapped_column(Text, unique=True)
    method: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text, server_default="created")
    amount_cents: Mapped[int] = mapped_column(Integer)
    currency: Mapped[str] = mapped_column(String(3))
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=func.now()
    )


class PaymentEvent(Base):
    """Every webhook delivery accepted. The primary key is the deduplication."""

    __tablename__ = "payment_events"

    provider_event_id: Mapped[str] = mapped_column(Text, primary_key=True)
    payment_id: Mapped[uuid.UUID | None]
    type: Mapped[str] = mapped_column(Text)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB)
    received_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=func.now()
    )
