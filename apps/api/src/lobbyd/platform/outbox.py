"""Transactional outbox. See docs/adr/0004-transactional-outbox.md.

An event is written to the `outbox` table in the same transaction as the
change it describes, so it exists if and only if the change happened. A relay
then copies events to a Redis stream. Delivery is at least once: every
consumer must be safe to run twice.
"""

import json
import uuid
from datetime import datetime
from typing import Any

from redis.asyncio import Redis
from sqlalchemy import Index, Text, delete, func, insert, select, text, update
from sqlalchemy.dialects.postgresql import JSONB, TIMESTAMP
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import Mapped, mapped_column

from lobbyd.platform import metrics, tracing
from lobbyd.platform.db import Base
from lobbyd.platform.ids import uuid7

STREAM = "lobbyd:events"


class OutboxEvent(Base):
    __tablename__ = "outbox"
    __table_args__ = (
        # The relay's only query: oldest unpublished first. Partial, so it
        # stays tiny however many events have already been published.
        Index("outbox_pending_idx", "id", postgresql_where=text("published_at IS NULL")),
    )

    # UUIDv7, so id order is creation order.
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid7)
    topic: Mapped[str] = mapped_column(Text)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB)
    # The trace the event was emitted in, so its handlers can continue it.
    trace: Mapped[dict[str, str] | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=func.now()
    )
    published_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))


async def emit(session: AsyncSession, topic: str, payload: dict[str, Any]) -> None:
    """Record an event. It is published only if the caller's transaction commits."""
    await session.execute(
        insert(OutboxEvent).values(
            id=uuid7(), topic=topic, payload=payload, trace=tracing.current_context()
        )
    )


async def relay_once(
    sessionmaker: async_sessionmaker[AsyncSession], redis: Redis, *, batch: int = 100
) -> int:
    """Publish one batch of pending events. Returns how many.

    If the process dies after publishing but before the commit that marks the
    rows, they are published again next time. That is the "at least once".
    """
    async with sessionmaker.begin() as session:
        events = (
            await session.execute(
                select(OutboxEvent.id, OutboxEvent.topic, OutboxEvent.payload, OutboxEvent.trace)
                .where(OutboxEvent.published_at.is_(None))
                .order_by(OutboxEvent.id)
                .limit(batch)
                # Several relays can run: each takes rows the others have not.
                .with_for_update(skip_locked=True)
            )
        ).all()
        if not events:
            return 0
        pipeline = redis.pipeline(transaction=False)
        for event in events:
            fields = {
                "id": str(event.id),
                "topic": event.topic,
                "payload": json.dumps(event.payload),
            }
            if event.trace:
                fields["trace"] = json.dumps(event.trace)
            pipeline.xadd(STREAM, fields)  # type: ignore[arg-type]
        await pipeline.execute()
        for event in events:
            metrics.OUTBOX_PUBLISHED.labels(event.topic).inc()
        await session.execute(
            update(OutboxEvent)
            .where(OutboxEvent.id.in_([event.id for event in events]))
            .values(published_at=func.now())
        )
        return len(events)


async def lag_seconds(session: AsyncSession) -> float:
    """Age of the oldest unpublished event. The outbox's key health number."""
    oldest = await session.scalar(
        select(func.extract("epoch", func.now() - func.min(OutboxEvent.created_at))).where(
            OutboxEvent.published_at.is_(None)
        )
    )
    return float(oldest or 0)


async def prune(session: AsyncSession, *, older_than_days: int = 7) -> int:
    deleted = await session.execute(
        delete(OutboxEvent)
        .where(
            OutboxEvent.published_at < func.now() - text(f"interval '{int(older_than_days)} days'")
        )
        .returning(OutboxEvent.id)
    )
    return len(deleted.all())
