"""Request and response shapes for holds and availability."""

import uuid
from datetime import datetime
from typing import Self

from pydantic import BaseModel, Field, model_validator

MAX_SEATS_PER_HOLD = 8
MAX_QUANTITY = 8


class CountedItemIn(BaseModel):
    ticket_type_id: uuid.UUID
    quantity: int = Field(default=1, ge=1, le=MAX_QUANTITY)


class HoldCreate(BaseModel):
    """Seats, counted tickets, or both. The hold is all or nothing."""

    tournament: str = Field(description="Tournament slug", min_length=1, max_length=80)
    seat_ids: list[uuid.UUID] = Field(default_factory=list, max_length=MAX_SEATS_PER_HOLD)
    items: list[CountedItemIn] = Field(
        default_factory=list,
        max_length=4,
        description="Team slots or general admission, by ticket type",
    )

    @model_validator(mode="after")
    def _check(self) -> Self:
        if not self.seat_ids and not self.items:
            raise ValueError("ask for at least one seat or item")
        if len(self.seat_ids) != len(set(self.seat_ids)):
            raise ValueError("seat_ids must be unique")
        types = [item.ticket_type_id for item in self.items]
        if len(types) != len(set(types)):
            raise ValueError("each ticket type may appear once in items")
        return self


class HoldItemOut(BaseModel):
    ticket_type_id: uuid.UUID
    seat_id: uuid.UUID | None
    quantity: int
    unit_price_cents: int


class HoldOut(BaseModel):
    id: uuid.UUID
    tournament_id: uuid.UUID
    status: str
    expires_at: datetime
    items: list[HoldItemOut]
    total_cents: int
    currency: str


class TicketTypeAvailability(BaseModel):
    ticket_type_id: uuid.UUID
    kind: str
    capacity: int
    available: int


class Availability(BaseModel):
    """Combine with the venue's seat map: every seat not listed in `taken_seat_ids` is free."""

    tournament_id: uuid.UUID
    ticket_types: list[TicketTypeAvailability]
    taken_seat_ids: list[uuid.UUID]
