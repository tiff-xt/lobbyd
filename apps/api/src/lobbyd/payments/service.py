"""Payment records and their state machine. The only code that touches the payments tables.

A payment only ever moves forward:

    created -> processing -> succeeded -> refund_pending -> refunded
                         \\-> failed

Every change is a guarded transition: it happens only from an allowed earlier
state. That one rule makes duplicate, late, and out-of-order news from the
provider harmless, whether it arrives by webhook or by polling.
"""

import uuid
from dataclasses import dataclass
from typing import Any

import structlog
from sqlalchemy import func, insert, select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from lobbyd.payments.models import Payment, PaymentEvent
from lobbyd.platform import outbox
from lobbyd.platform.events import EventContext
from lobbyd.platform.ids import uuid7

log = structlog.get_logger()

PAYMENT_REQUESTED = "payment.requested"
REFUND_REQUESTED = "refund.requested"

# new status -> the statuses it may be reached from
_ALLOWED_FROM = {
    "processing": ("created",),
    "succeeded": ("created", "processing"),
    "failed": ("created", "processing"),
    "refunded": ("succeeded", "refund_pending"),
}


@dataclass(frozen=True)
class PaymentOut:
    status: str
    method: str


@dataclass(frozen=True)
class Transition:
    """A payment changed state. Returned so the orders module can react."""

    payment_id: uuid.UUID
    order_id: uuid.UUID
    status: str


async def create(
    session: AsyncSession, *, order_id: uuid.UUID, amount_cents: int, currency: str, method: str
) -> None:
    """Record that a payment is wanted. No network call happens here.

    The provider is contacted by a worker, from the outbox event. So a
    database transaction is never held open across a network request, and
    checkout still works while the provider is slow or down.
    """
    payment_id = uuid7()
    await session.execute(
        insert(Payment).values(
            id=payment_id,
            order_id=order_id,
            method=method,
            amount_cents=amount_cents,
            currency=currency,
        )
    )
    await outbox.emit(session, PAYMENT_REQUESTED, {"payment_id": str(payment_id)})


async def for_orders(
    session: AsyncSession, order_ids: list[uuid.UUID]
) -> dict[uuid.UUID, PaymentOut]:
    rows = await session.execute(
        select(Payment.order_id, Payment.status, Payment.method).where(
            Payment.order_id.in_(order_ids)
        )
    )
    return {order_id: PaymentOut(status, method) for order_id, status, method in rows}


async def record_event(session: AsyncSession, event: dict[str, Any]) -> bool:
    """Store a webhook delivery. False means this event id was already handled."""
    try:
        payment_id: uuid.UUID | None = uuid.UUID(event["data"]["reference"])
    except (KeyError, ValueError, TypeError):
        payment_id = None
    inserted = await session.execute(
        pg_insert(PaymentEvent)
        .values(
            provider_event_id=event["id"], payment_id=payment_id, type=event["type"], payload=event
        )
        .on_conflict_do_nothing()
        .returning(PaymentEvent.provider_event_id)
    )
    return inserted.first() is not None


async def apply(
    session: AsyncSession, payment_id: uuid.UUID, *, status: str, provider_ref: str | None
) -> Transition | None:
    """Move a payment to `status` if that is a step forward. None if it is not.

    The payment is found by OUR id, which the provider echoes back as its
    `reference`, not by the provider's id. A webhook can arrive before we
    have stored the provider's id, and must not be lost when it does.
    """
    payment = await session.scalar(
        select(Payment).where(Payment.id == payment_id).with_for_update()
    )
    if payment is None or status not in _ALLOWED_FROM:
        return None
    if payment.provider_ref is None and provider_ref is not None:
        payment.provider_ref = provider_ref
    if payment.status not in _ALLOWED_FROM[status]:
        # Old news: a duplicate, or an event overtaken by a later one.
        return None
    payment.status = status
    payment.updated_at = func.now()
    await session.flush()
    log.info("payment_" + status, payment_id=str(payment.id))
    return Transition(payment.id, payment.order_id, status)


async def request_refund(session: AsyncSession, order_id: uuid.UUID) -> None:
    """Ask for a succeeded payment to be returned. Safe to call more than once."""
    payment = await session.scalar(
        select(Payment).where(Payment.order_id == order_id).with_for_update()
    )
    if payment is None or payment.status != "succeeded":
        return
    payment.status = "refund_pending"
    payment.updated_at = func.now()
    await outbox.emit(session, REFUND_REQUESTED, {"payment_id": str(payment.id)})
    log.info("refund_requested", payment_id=str(payment.id))


async def unsettled(
    session: AsyncSession, *, older_than_seconds: int, limit: int = 100
) -> list[tuple[uuid.UUID, str]]:
    """(payment id, provider ref) of payments that have waited a while for news."""
    rows = await session.execute(
        select(Payment.id, Payment.provider_ref)
        .where(
            Payment.status.in_(("created", "processing", "refund_pending")),
            Payment.provider_ref.is_not(None),
            Payment.updated_at < func.now() - text(f"interval '{int(older_than_seconds)} seconds'"),
        )
        .order_by(Payment.updated_at)
        .limit(limit)
    )
    return [(payment_id, ref) for payment_id, ref in rows if ref is not None]


# --- event handlers (run by workers) ------------------------------------------


async def handle_payment_requested(ctx: EventContext, payload: dict[str, Any]) -> None:
    """Create the payment at the provider.

    Safe to run twice: the provider is given our payment id as the
    idempotency key and returns the payment it already made.
    """
    payment_id = uuid.UUID(payload["payment_id"])
    async with ctx.sessionmaker() as session:
        payment = await session.get(Payment, payment_id)
    if payment is None or payment.provider_ref is not None:
        return

    # No transaction is open while we wait on the network.
    created = await ctx.provider.create_payment(
        key=str(payment.id),
        reference=str(payment.id),
        amount_cents=payment.amount_cents,
        currency=payment.currency,
        method=payment.method,
    )
    async with ctx.sessionmaker.begin() as session:
        stored = await session.scalar(
            select(Payment).where(Payment.id == payment_id).with_for_update()
        )
        if stored is not None and stored.provider_ref is None:
            stored.provider_ref = created.ref


async def handle_refund_requested(ctx: EventContext, payload: dict[str, Any]) -> None:
    payment_id = uuid.UUID(payload["payment_id"])
    async with ctx.sessionmaker() as session:
        payment = await session.get(Payment, payment_id)
    if payment is None or payment.status != "refund_pending" or payment.provider_ref is None:
        return
    await ctx.provider.refund(key=f"refund-{payment.id}", ref=payment.provider_ref)
