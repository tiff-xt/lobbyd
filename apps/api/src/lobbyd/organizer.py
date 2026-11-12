"""The organizer's view of one tournament: how the sale is going.

Read only. Every number comes from the module that owns it, so this file
holds no queries of its own; it is the one place allowed to look across
inventory, orders, payments, teams, and the door at once.
"""

import uuid

from fastapi import APIRouter
from pydantic import BaseModel, Field

from lobbyd.catalog import service as catalog
from lobbyd.identity.deps import CurrentUser
from lobbyd.inventory import service as inventory
from lobbyd.orders import service as orders
from lobbyd.payments import service as payments
from lobbyd.platform.db import SessionDep
from lobbyd.teams import service as teams
from lobbyd.tickets import checkin

router = APIRouter(tags=["organizer"])


class TicketTypeSales(BaseModel):
    id: uuid.UUID
    name: str
    kind: str
    price_cents: int
    capacity: int
    sold: int
    held: int = Field(description="In someone's basket right now")
    available: int
    gross_cents: int = Field(description="What paid orders spent on this type")


class TeamCounts(BaseModel):
    confirmed: int
    pending: int = Field(description="Slot held, entry fee not yet paid")
    waitlist: int


class SalesOut(BaseModel):
    tournament_id: uuid.UUID
    currency: str
    ticket_types: list[TicketTypeSales]
    orders: dict[str, int] = Field(description="Orders by status")
    gross_cents: int = Field(description="Total of paid orders")
    refunded_cents: int = Field(description="Paid back on those orders (teams that withdrew)")
    net_cents: int
    teams: TeamCounts
    tickets: int = Field(description="Valid tickets issued")
    checked_in: int


@router.get(
    "/tournaments/{slug}/sales",
    summary="How a tournament's sale is going",
    description="For the tournament's organizer. Numbers are live, not cached.",
)
async def get_sales(slug: str, session: SessionDep, user: CurrentUser) -> SalesOut:
    managed = await catalog.require_owner(session, user, slug=slug)
    info = await catalog.get_sale_info(session, slug)
    stock = await inventory.stock(session, managed.tournament_id)
    figures = await orders.sales_figures(session, managed.tournament_id)
    refunded = await payments.refunded_cents(session, orders.paid_order_ids(managed.tournament_id))
    door = await checkin.summary(session, managed)

    ticket_types = []
    for ticket_type in sorted(info.ticket_types.values(), key=lambda t: (t.kind, t.name)):
        held, sold = stock.get(ticket_type.id, (0, 0))
        ticket_types.append(
            TicketTypeSales(
                id=ticket_type.id,
                name=ticket_type.name,
                kind=ticket_type.kind,
                price_cents=ticket_type.price_cents,
                capacity=ticket_type.capacity,
                sold=sold,
                held=held,
                available=max(0, ticket_type.capacity - sold - held),
                gross_cents=figures.gross_by_ticket_type.get(ticket_type.id, 0),
            )
        )
    return SalesOut(
        tournament_id=managed.tournament_id,
        currency=info.currency,
        ticket_types=ticket_types,
        orders=figures.orders_by_status,
        gross_cents=figures.gross_cents,
        refunded_cents=refunded,
        net_cents=figures.gross_cents - refunded,
        teams=TeamCounts(**await teams.headcount(session, managed.tournament_id)),
        tickets=door.tickets,
        checked_in=door.checked_in,
    )
