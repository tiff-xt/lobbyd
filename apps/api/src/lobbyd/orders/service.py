"""Checkout. The only code that touches the orders tables."""

import uuid

import structlog
from sqlalchemy import func, insert, select
from sqlalchemy.ext.asyncio import AsyncSession

from lobbyd.inventory import service as inventory
from lobbyd.orders.models import Order, OrderItem
from lobbyd.orders.schemas import OrderItemOut, OrderOut
from lobbyd.platform.errors import ProblemError
from lobbyd.platform.ids import uuid7
from lobbyd.tickets import service as tickets
from lobbyd.tickets.service import TicketLine

log = structlog.get_logger()


async def place_order(session: AsyncSession, user_id: uuid.UUID, hold_id: uuid.UUID) -> OrderOut:
    """Turn a hold into a paid order with tickets, in one transaction.

    Inventory moves from held to sold, the order is recorded, and the tickets
    are issued together, or none of it happens. Payment is a stub for now and
    always succeeds; phase 4 puts a real payment step between the pending
    order and this point.
    """
    converted = await inventory.convert_hold(session, user_id, hold_id)

    order_id = uuid7()
    await session.execute(
        insert(Order).values(
            id=order_id,
            user_id=user_id,
            tournament_id=converted.tournament_id,
            hold_id=converted.hold_id,
            status="paid",
            total_cents=sum(i.quantity * i.unit_price_cents for i in converted.items),
            currency=converted.currency,
            paid_at=func.now(),
        )
    )
    lines = [
        TicketLine(uuid7(), item.ticket_type_id, item.seat_id, item.quantity)
        for item in converted.items
    ]
    await session.execute(
        insert(OrderItem),
        [
            {
                "id": line.order_item_id,
                "order_id": order_id,
                "ticket_type_id": item.ticket_type_id,
                "seat_id": item.seat_id,
                "quantity": item.quantity,
                "unit_price_cents": item.unit_price_cents,
            }
            for line, item in zip(lines, converted.items, strict=True)
        ],
    )
    await tickets.issue(
        session,
        order_id=order_id,
        tournament_id=converted.tournament_id,
        holder_id=user_id,
        lines=lines,
    )
    log.info("order_paid", order_id=str(order_id))
    return (await _orders(session, Order.id == order_id))[0]


async def get_order(session: AsyncSession, user_id: uuid.UUID, order_id: uuid.UUID) -> OrderOut:
    orders = await _orders(session, Order.id == order_id, Order.user_id == user_id)
    if not orders:
        raise ProblemError(404, "order_not_found", "No such order.")
    return orders[0]


async def list_orders(session: AsyncSession, user_id: uuid.UUID) -> list[OrderOut]:
    return await _orders(session, Order.user_id == user_id)


async def _orders(session: AsyncSession, *where: object) -> list[OrderOut]:
    orders = list(
        await session.scalars(
            select(Order).where(*where).order_by(Order.created_at.desc(), Order.id.desc()).limit(50)  # type: ignore[arg-type]
        )
    )
    if not orders:
        return []
    order_ids = [order.id for order in orders]
    items: dict[uuid.UUID, list[OrderItemOut]] = {order_id: [] for order_id in order_ids}
    rows = await session.execute(
        select(
            OrderItem.order_id,
            OrderItem.ticket_type_id,
            OrderItem.seat_id,
            OrderItem.quantity,
            OrderItem.unit_price_cents,
        )
        .where(OrderItem.order_id.in_(order_ids))
        .order_by(OrderItem.id)
    )
    for order_id, ticket_type_id, seat_id, quantity, unit_price_cents in rows:
        items[order_id].append(
            OrderItemOut(
                ticket_type_id=ticket_type_id,
                seat_id=seat_id,
                quantity=quantity,
                unit_price_cents=unit_price_cents,
            )
        )
    issued = await tickets.for_orders(session, order_ids)
    return [
        OrderOut(
            id=order.id,
            tournament_id=order.tournament_id,
            hold_id=order.hold_id,
            status=order.status,
            total_cents=order.total_cents,
            currency=order.currency,
            created_at=order.created_at,
            paid_at=order.paid_at,
            items=items[order.id],
            tickets=issued[order.id],
        )
        for order in orders
    ]
