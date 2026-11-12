"""Emails to buyers. Recorded and logged, not actually sent: there is no mail
server in this project. The interesting part is that each is sent exactly once
even though events are delivered at least once."""

import uuid
from typing import Any

import structlog
from sqlalchemy.dialects.postgresql import insert as pg_insert

from lobbyd.notifications.models import Notification
from lobbyd.platform.events import EventContext
from lobbyd.platform.ids import uuid7

log = structlog.get_logger()

# event topic -> kind of message
KINDS = {
    "order.paid": "order_confirmation",
    "order.cancelled": "payment_failed",
    "order.expired": "order_expired",
    "order.refunded": "refund_notice",
    "waitlist.offered": "waitlist_offer",
}


async def _notify(ctx: EventContext, kind: str, payload: dict[str, Any]) -> None:
    async with ctx.sessionmaker.begin() as session:
        recorded = await session.execute(
            pg_insert(Notification)
            .values(
                id=uuid7(),
                user_id=uuid.UUID(payload["user_id"]),
                kind=kind,
                # What the message is about: an order, or a waitlist entry.
                subject_id=uuid.UUID(payload.get("subject_id") or payload["order_id"]),
                payload=payload,
            )
            .on_conflict_do_nothing()
            .returning(Notification.id)
        )
        if recorded.first() is None:
            return  # this event was already handled
        # A real mail call would go here, inside the transaction: if sending
        # fails, the row is rolled back and the retry sends it.
        log.info(
            "email_sent", kind=kind, subject_id=payload.get("subject_id") or payload["order_id"]
        )


def handler_for(topic: str) -> Any:
    async def handle(ctx: EventContext, payload: dict[str, Any]) -> None:
        await _notify(ctx, KINDS[topic], payload)

    return handle


HANDLERS = {topic: handler_for(topic) for topic in KINDS}
