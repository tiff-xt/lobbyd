"""Transactional outbox. See docs/adr/0004-transactional-outbox.md.

An event is written to the `outbox` table in the same transaction as the
change it describes, so it exists if and only if the change happened. A relay
then copies events to a Redis stream. Delivery is at least once: every
consumer must be safe to run twice.
"""

from datetime import timedelta
from typing import Any

from oncely import Outbox
from oncely.streams import stream_publisher
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from lobbyd.platform import metrics, tracing
from lobbyd.platform.db import Base

STREAM = "lobbyd:events"

# The mechanism lives in the oncely package. This module
# adds what is lobbyd's own: the stream name, trace context, metrics.
_outbox = Outbox(Base.metadata)


class OutboxEvent(Base):
    """The outbox table, mapped so tests and queries can use it like any model."""

    __table__ = _outbox.table


async def emit(session: AsyncSession, topic: str, payload: dict[str, Any]) -> None:
    """Record an event. It is published only if the caller's transaction commits.

    The current trace travels in the event's headers, so its handlers can
    continue it.
    """
    await _outbox.emit(session, topic, payload, headers=tracing.current_context())


async def relay_once(
    sessionmaker: async_sessionmaker[AsyncSession], redis: Redis, *, batch: int = 100
) -> int:
    """Publish one batch of pending events to the stream. Returns how many."""
    events = await _outbox.relay_once(sessionmaker, stream_publisher(redis, STREAM), batch=batch)
    for event in events:
        metrics.OUTBOX_PUBLISHED.labels(event.topic).inc()
    return len(events)


async def lag_seconds(session: AsyncSession) -> float:
    """Age of the oldest unpublished event. The outbox's key health number."""
    return await _outbox.lag_seconds(session)


async def prune(session: AsyncSession, *, older_than_days: int = 7) -> int:
    return await _outbox.prune(session, older_than=timedelta(days=older_than_days))
