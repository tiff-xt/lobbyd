"""Where news from the payment provider enters: webhooks, and polling as a fallback.

Both paths end in the same two calls, `payments.apply` then
`orders.on_payment_update`, so they cannot disagree about what a piece of
news means.
"""

import json
import uuid
from typing import Any

import structlog
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from lobbyd.config import Settings
from lobbyd.orders import service as orders
from lobbyd.payments import service as payments
from lobbyd.payments.provider import (
    InvalidSignatureError,
    PaymentProvider,
    verify_signature,
)
from lobbyd.platform.errors import ProblemError

log = structlog.get_logger()

# webhook type -> payment status
_STATUS = {
    "payment.processing": "processing",
    "payment.succeeded": "succeeded",
    "payment.failed": "failed",
    "refund.succeeded": "refunded",
}


async def handle_webhook(
    session: AsyncSession, settings: Settings, *, signature: str | None, body: bytes
) -> None:
    """Verify, deduplicate, apply. All in the request's one transaction.

    Raising makes the provider retry later, which is what we want for
    anything we could not process. Returning normally acknowledges it.
    """
    try:
        verify_signature(
            settings.paysim_webhook_secret.get_secret_value(),
            signature,
            body,
            tolerance_seconds=settings.webhook_tolerance_seconds,
        )
    except InvalidSignatureError as exc:
        raise ProblemError(400, "invalid_signature", "The webhook signature is not valid.") from exc

    try:
        event: dict[str, Any] = json.loads(body)
        event_id, event_type = str(event["id"]), str(event["type"])
        payment_id = uuid.UUID(event["data"]["reference"])
        provider_ref = str(event["data"]["id"])
    except (ValueError, KeyError, TypeError) as exc:
        raise ProblemError(400, "invalid_webhook", "The webhook body is not understood.") from exc

    # Recording the event id and acting on it commit together. A redelivery
    # of the same event finds the id and stops here.
    if not await payments.record_event(session, event):
        log.info("webhook_duplicate", provider_event_id=event_id)
        return
    status = _STATUS.get(event_type)
    if status is None:
        return  # a type we do not act on; acknowledged so it is not resent
    change = await payments.apply(session, payment_id, status=status, provider_ref=provider_ref)
    if change is not None:
        await orders.on_payment_update(session, change)


async def poll_unsettled(
    sessionmaker: async_sessionmaker[AsyncSession],
    provider: PaymentProvider,
    *,
    older_than_seconds: int = 60,
) -> int:
    """Ask the provider about payments we have heard nothing about for a while.

    Webhooks are the fast path, not the only one: a lost webhook must not
    leave a buyer charged with no tickets. Returns how many payments moved.
    """
    async with sessionmaker() as session:
        waiting = await payments.unsettled(session, older_than_seconds=older_than_seconds)

    moved = 0
    for payment_id, provider_ref in waiting:
        try:
            current = await provider.get_payment(provider_ref)  # no transaction open
        except Exception:
            log.warning("payment_poll_failed", payment_id=str(payment_id), exc_info=True)
            continue
        async with sessionmaker.begin() as session:
            change = await payments.apply(
                session, payment_id, status=current.status, provider_ref=provider_ref
            )
            if change is not None:
                await orders.on_payment_update(session, change)
                moved += 1
    return moved
