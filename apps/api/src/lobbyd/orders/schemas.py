"""Request and response shapes for orders."""

import uuid
from datetime import datetime

from pydantic import BaseModel

from lobbyd.tickets.service import TicketOut


class OrderCreate(BaseModel):
    hold_id: uuid.UUID


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
    items: list[OrderItemOut]
    tickets: list[TicketOut]
