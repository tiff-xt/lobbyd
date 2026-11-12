"""Request and response shapes for orders."""

import uuid
from datetime import datetime

from pydantic import BaseModel, Field

from lobbyd.tickets.service import TicketOut


class OrderCreate(BaseModel):
    hold_id: uuid.UUID
    payment_method: str = Field(
        default="pm_success",
        pattern=r"^pm_[a-z_]{1,40}$",
        description=(
            "A test payment method, standing in for card details. pm_success pays; "
            "pm_declined is refused; pm_duplicate, pm_reordered, pm_slow and pm_never "
            "succeed while the provider misbehaves in the way the name says."
        ),
    )


class PaymentOut(BaseModel):
    status: str
    method: str


class OrderItemOut(BaseModel):
    ticket_type_id: uuid.UUID
    seat_id: uuid.UUID | None
    quantity: int
    unit_price_cents: int


class OrderOut(BaseModel):
    id: uuid.UUID
    tournament_id: uuid.UUID
    hold_id: uuid.UUID
    status: str
    total_cents: int
    currency: str
    created_at: datetime
    paid_at: datetime | None
    payment: PaymentOut | None = Field(description="Absent for orders that cost nothing")
    items: list[OrderItemOut]
    tickets: list[TicketOut]
