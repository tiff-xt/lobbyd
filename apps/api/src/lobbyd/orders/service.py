"""Checkout. The only code that touches the orders tables.

An order's life:

    pending --payment succeeded--> paid ------------------> (refunded)
       |---payment failed-------> cancelled
       |---hold ran out---------> expired
                                     \\--payment arrives anyway--> refunded

Each arrow is a guarded transition made while holding the order's row lock,
so news about a payment can arrive twice, late, or in the wrong order
without the order ending up anywhere it should not.
"""

import uuid
from typing import Any

import structlog
from sqlalchemy import ColumnElement, func, insert, select
from sqlalchemy.ext.asyncio import AsyncSession

from lobbyd.catalog import service as catalog
from lobbyd.config import Settings
from lobbyd.inventory import service as inventory
from lobbyd.orders.models import Order, OrderItem
from lobbyd.orders.schemas import OrderItemOut, OrderOut, PaymentOut
from lobbyd.payments import service as payments
from lobbyd.payments.service import Transition
from lobbyd.platform import outbox
from lobbyd.platform.cache import invalidate
from lobbyd.platform.errors import ProblemError
from lobbyd.platform.events import EventContext
from lobbyd.platform.ids import uuid7
from lobbyd.tickets import service as tickets
from lobbyd.tickets.service import TicketLine

log = structlog.get_logger()


async def place_order(
    session: AsyncSession,
    settings: Settings,
    user_id: uuid.UUID,
    hold_id: uuid.UUID,
    payment_method: str,
) -> OrderOut:
    """Start checkout: record a pending order and ask for payment.

    Returns at once with the order pending. The payment itself happens
    asynchronously; the client watches the order until it settles.
    """
    snapshot = await inventory.begin_checkout(
        session, user_id, hold_id, window_seconds=settings.payment_window_seconds
    )
    existing = await session.scalar(select(Order.id).where(Order.hold_id == hold_id))
    if existing is not None:
        raise ProblemError(
            409,
            "hold_already_used",
            "Checkout has already started for this hold.",
            order_id=str(existing),
        )

    order_id = uuid7()
    total = sum(item.quantity * item.unit_price_cents for item in snapshot.items)
    await session.execute(
        insert(Order).values(
            id=order_id,
            user_id=user_id,
            tournament_id=snapshot.tournament_id,
            hold_id=hold_id,
            status="pending",
            total_cents=total,
            currency=snapshot.currency,
        )
    )
    await session.execute(
        insert(OrderItem),
        [
            {
                "id": uuid7(),
                "order_id": order_id,
                "ticket_type_id": item.ticket_type_id,
                "seat_id": item.seat_id,
                "quantity": item.quantity,
                "unit_price_cents": item.unit_price_cents,
            }
            for item in snapshot.items
        ],
    )

    if total == 0:
        # Nothing to pay (free entry): complete the order here and now.
        order = await _lock(session, order_id)
        await _complete(session, order)
    else:
        await payments.create(
            session,
            order_id=order_id,
            amount_cents=total,
            currency=snapshot.currency,
            method=payment_method,
        )
    log.info("order_placed", order_id=str(order_id))
    return (await _orders(session, Order.id == order_id))[0]


async def _lock(session: AsyncSession, order_id: uuid.UUID) -> Order:
    order = await session.scalar(select(Order).where(Order.id == order_id).with_for_update())
    if order is None:
        raise RuntimeError(f"order {order_id} is missing")
    return order


def _event(order: Order) -> dict[str, Any]:
    return {
        "order_id": str(order.id),
        "hold_id": str(order.hold_id),
        "user_id": str(order.user_id),
        "total_cents": order.total_cents,
        "currency": order.currency,
    }


async def _complete(session: AsyncSession, order: Order) -> bool:
    """Sell the inventory, mark the order paid, issue the tickets: one transaction.

    Returns False, having changed nothing, if the hold lapsed and its
    inventory went to someone else.
    """
    try:
        # A savepoint: if the hold turns out to be gone, undo the partial
        # sale but keep the transaction, which still has an order to settle.
        async with session.begin_nested():
            await inventory.convert_hold(session, order.hold_id)
    except ProblemError as exc:
        if exc.code != "hold_expired":
            raise
        return False

    order.status = "paid"
    order.paid_at = func.now()
    items = (
        await session.execute(
            select(OrderItem.id, OrderItem.ticket_type_id, OrderItem.seat_id, OrderItem.quantity)
            .where(OrderItem.order_id == order.id)
            .order_by(OrderItem.id)
        )
    ).all()
    await tickets.issue(
        session,
        order_id=order.id,
        tournament_id=order.tournament_id,
        holder_id=order.user_id,
        lines=[TicketLine(*item) for item in items],
    )
    # In the same transaction as the sale: the confirmation is sent if and
    # only if the order really was paid.
    await outbox.emit(session, "order.paid", _event(order))
    log.info("order_paid", order_id=str(order.id))
    return True


def cache_key(user_id: uuid.UUID, order_id: uuid.UUID) -> str:
    """Where an order's rendered JSON is cached. The user is part of the key,
    so a cached order can only ever be served to its owner."""
    return f"order:{user_id}:{order_id}"


async def on_payment_update(session: AsyncSession, change: Transition) -> str:
    """React to a payment changing state. The caller holds the payment's row lock.

    Returns the cache key of the order, for the caller to drop once the
    transaction has committed.
    """
    order = await _lock(session, change.order_id)

    if change.status == "succeeded":
        if order.status == "pending":
            if not await _complete(session, order):
                # Paid, but the seats are gone. The second buyer keeps them;
                # this buyer gets their money back.
                order.status = "expired"
                await outbox.emit(session, "order.expired", _event(order))
                await payments.request_refund(session, order.id)
        elif order.status in ("expired", "cancelled"):
            # Money arrived for an order we had already given up on.
            await payments.request_refund(session, order.id)

    elif change.status == "failed":
        if order.status == "pending":
            order.status = "cancelled"
            # Put the inventory back on sale now, not when the hold lapses.
            await inventory.release_hold(session, order.hold_id)
            await outbox.emit(session, "order.cancelled", _event(order))

    elif change.status == "refunded" and order.status != "refunded":
        order.status = "refunded"
        await outbox.emit(session, "order.refunded", _event(order))

    await session.flush()
    return cache_key(order.user_id, order.id)


async def expire_stale_orders(session: AsyncSession, *, limit: int = 200) -> list[str]:
    """Close pending orders whose hold lapsed before payment arrived.

    If the payment turns up afterwards, on_payment_update refunds it.
    Returns the cache keys of the orders it closed.
    """
    pending = list(
        await session.scalars(
            select(Order)
            .where(Order.status == "pending")
            .order_by(Order.created_at)
            .limit(limit)
            .with_for_update(skip_locked=True)
        )
    )
    closed = await inventory.closed_holds(session, [order.hold_id for order in pending])
    expired = [order for order in pending if order.hold_id in closed]
    for order in expired:
        order.status = "expired"
        await outbox.emit(session, "order.expired", _event(order))
    await session.flush()
    return [cache_key(order.user_id, order.id) for order in expired]


# --- reading -------------------------------------------------------------------


async def get_order(session: AsyncSession, user_id: uuid.UUID, order_id: uuid.UUID) -> OrderOut:
    orders = await _orders(session, Order.id == order_id, Order.user_id == user_id)
    if not orders:
        raise ProblemError(404, "order_not_found", "No such order.")
    return orders[0]


async def list_orders(session: AsyncSession, user_id: uuid.UUID) -> list[OrderOut]:
    return await _orders(session, Order.user_id == user_id)


async def _orders(session: AsyncSession, *where: ColumnElement[bool]) -> list[OrderOut]:
    orders = list(
        await session.scalars(
            select(Order)
            .where(*where)
            .order_by(Order.created_at.desc(), Order.id.desc())
            .limit(50)
            .execution_options(populate_existing=True)
        )
    )
    if not orders:
        return []
    order_ids = [order.id for order in orders]
    items: dict[uuid.UUID, list[OrderItemOut]] = {order_id: [] for order_id in order_ids}
    rows = (
        await session.execute(
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
    ).all()
    seats = await catalog.seat_labels(session, [row.seat_id for row in rows if row.seat_id])
    names = await catalog.ticket_type_names(session, [row.ticket_type_id for row in rows])
    for order_id, ticket_type_id, seat_id, quantity, unit_price_cents in rows:
        items[order_id].append(
            OrderItemOut(
                ticket_type_id=ticket_type_id,
                ticket_type_name=names[ticket_type_id],
                seat_id=seat_id,
                seat=seats.get(seat_id) if seat_id else None,
                quantity=quantity,
                unit_price_cents=unit_price_cents,
            )
        )
    tournaments = await catalog.tournament_refs(session, [order.tournament_id for order in orders])
    issued = await tickets.for_orders(session, order_ids)
    paid_with = await payments.for_orders(session, order_ids)
    return [
        OrderOut(
            id=order.id,
            tournament_id=order.tournament_id,
            tournament=tournaments[order.tournament_id],
            hold_id=order.hold_id,
            status=order.status,
            total_cents=order.total_cents,
            currency=order.currency,
            created_at=order.created_at,
            paid_at=order.paid_at,
            payment=(
                PaymentOut(status=paid_with[order.id].status, method=paid_with[order.id].method)
                if order.id in paid_with
                else None
            ),
            items=items[order.id],
            tickets=issued[order.id],
        )
        for order in orders
    ]


# --- event handlers -------------------------------------------------------------


async def handle_team_withdrawn(ctx: EventContext, payload: dict[str, Any]) -> None:
    """A confirmed team gave up its slot: cancel its entry ticket and put the slot back on sale.

    Safe to run twice: the ticket is only voided once, and the slot is only
    returned for a ticket this call voided.
    """
    hold_id = uuid.UUID(payload["hold_id"])
    ticket_type_id = uuid.UUID(payload["ticket_type_id"])
    async with ctx.sessionmaker.begin() as session:
        order = await session.scalar(
            select(Order).where(Order.hold_id == hold_id).with_for_update()
        )
        if order is None or order.status != "paid":
            return
        voided = await tickets.void(session, order.id, ticket_type_id)
        if voided:
            await inventory.return_sold(session, ticket_type_id, voided)
        key = cache_key(order.user_id, order.id)
    await invalidate(ctx.redis, key)
