"""Ticket issuing. The only code that touches the tickets table."""

import uuid
from dataclasses import dataclass

from pydantic import BaseModel
from sqlalchemy import insert, select
from sqlalchemy.ext.asyncio import AsyncSession

from lobbyd.catalog import service as catalog
from lobbyd.catalog.schemas import SeatLabel
from lobbyd.platform.ids import uuid7
from lobbyd.tickets.models import Ticket


class TicketOut(BaseModel):
    id: uuid.UUID
    ticket_type_id: uuid.UUID
    ticket_type_name: str
    seat_id: uuid.UUID | None
    seat: SeatLabel | None
    status: str


@dataclass(frozen=True)
class TicketLine:
    """One purchased line: `quantity` tickets, or one ticket for a seat."""

    order_item_id: uuid.UUID
    ticket_type_id: uuid.UUID
    seat_id: uuid.UUID | None
    quantity: int


async def issue(
    session: AsyncSession,
    *,
    order_id: uuid.UUID,
    tournament_id: uuid.UUID,
    holder_id: uuid.UUID,
    lines: list[TicketLine],
) -> None:
    await session.execute(
        insert(Ticket),
        [
            {
                "id": uuid7(),
                "order_id": order_id,
                "order_item_id": line.order_item_id,
                "tournament_id": tournament_id,
                "ticket_type_id": line.ticket_type_id,
                "seat_id": line.seat_id,
                "holder_id": holder_id,
            }
            for line in lines
            for _ in range(line.quantity)
        ],
    )


async def for_orders(
    session: AsyncSession, order_ids: list[uuid.UUID]
) -> dict[uuid.UUID, list[TicketOut]]:
    tickets: dict[uuid.UUID, list[TicketOut]] = {order_id: [] for order_id in order_ids}
    rows = (
        await session.execute(
            select(Ticket.order_id, Ticket.id, Ticket.ticket_type_id, Ticket.seat_id, Ticket.status)
            .where(Ticket.order_id.in_(order_ids))
            .order_by(Ticket.id)
        )
    ).all()
    seats = await catalog.seat_labels(session, [row.seat_id for row in rows if row.seat_id])
    names = await catalog.ticket_type_names(session, [row.ticket_type_id for row in rows])
    for order_id, ticket_id, ticket_type_id, seat_id, status in rows:
        tickets[order_id].append(
            TicketOut(
                id=ticket_id,
                ticket_type_id=ticket_type_id,
                ticket_type_name=names[ticket_type_id],
                seat_id=seat_id,
                seat=seats.get(seat_id) if seat_id else None,
                status=status,
            )
        )
    return tickets
