"""Transactional outbox.

An event is written to a table in the same transaction as the change it
describes, so it exists if and only if the change happened. A relay then
hands events to a publisher. Delivery is at least once: every consumer must
be safe to run twice.
"""

import os
import threading
import time
import uuid
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from sqlalchemy import (
    Column,
    Index,
    MetaData,
    Table,
    Text,
    Uuid,
    delete,
    func,
    select,
    text,
    update,
)
from sqlalchemy.dialects.postgresql import JSONB, TIMESTAMP
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

_last_id = 0
_last_id_lock = threading.Lock()


def uuid7() -> uuid.UUID:
    """A time-ordered UUID (RFC 9562), so id order is creation order.

    Two ids made in the same millisecond would otherwise be ordered by their
    random bits, so within a process each id is also made greater than the
    one before it. Across processes, order is to the millisecond.
    """
    global _last_id
    millis = time.time_ns() // 1_000_000
    rand = int.from_bytes(os.urandom(10))
    value = (millis & ((1 << 48) - 1)) << 80
    value |= 0x7 << 76
    value |= (rand >> 68) << 64
    value |= 0b10 << 62
    value |= rand & ((1 << 62) - 1)
    with _last_id_lock:
        if value <= _last_id:
            value = _last_id + 1
        _last_id = value
    return uuid.UUID(int=value)


@dataclass(frozen=True, slots=True)
class Event:
    id: uuid.UUID
    topic: str
    payload: dict[str, Any]
    #: Context that travels with the event and is not part of its meaning:
    #: a trace id, a tenant, a schema version.
    headers: dict[str, str] | None = None


#: Sends a batch somewhere durable: a stream, a queue, a webhook. If it
#: raises, nothing is marked published and the batch is offered again.
Publisher = Callable[[Sequence[Event]], Awaitable[None]]


class Outbox:
    """A table of events, attached to your metadata so your migrations create it."""

    def __init__(self, metadata: MetaData, *, table_name: str = "outbox") -> None:
        self.table = Table(
            table_name,
            metadata,
            Column("id", Uuid, primary_key=True),
            Column("topic", Text, nullable=False),
            Column("payload", JSONB, nullable=False),
            Column("headers", JSONB),
            Column(
                "created_at", TIMESTAMP(timezone=True), server_default=func.now(), nullable=False
            ),
            Column("published_at", TIMESTAMP(timezone=True)),
            # The relay's only query: oldest unpublished first. Partial, so it
            # stays tiny however many events have already been published.
            Index(f"{table_name}_pending_idx", "id", postgresql_where=text("published_at IS NULL")),
        )

    async def emit(
        self,
        session: AsyncSession,
        topic: str,
        payload: dict[str, Any],
        *,
        headers: dict[str, str] | None = None,
    ) -> uuid.UUID:
        """Record an event. It is published only if the caller's transaction commits."""
        event_id = uuid7()
        await session.execute(
            self.table.insert().values(id=event_id, topic=topic, payload=payload, headers=headers)
        )
        return event_id

    async def relay_once(
        self,
        sessionmaker: async_sessionmaker[AsyncSession],
        publish: Publisher,
        *,
        batch: int = 100,
    ) -> list[Event]:
        """Publish one batch of pending events, oldest first. Returns them.

        If the process dies after publishing but before the commit that marks
        the rows, they are published again next time. That is the "at least
        once".
        """
        t = self.table
        async with sessionmaker.begin() as session:
            rows = (
                await session.execute(
                    select(t.c.id, t.c.topic, t.c.payload, t.c.headers)
                    .where(t.c.published_at.is_(None))
                    .order_by(t.c.id)
                    .limit(batch)
                    # Several relays can run: each takes rows the others have not.
                    .with_for_update(skip_locked=True)
                )
            ).all()
            if not rows:
                return []
            events = [Event(row.id, row.topic, row.payload, row.headers) for row in rows]
            await publish(events)
            await session.execute(
                update(t)
                .where(t.c.id.in_([event.id for event in events]))
                .values(published_at=func.now())
            )
            return events

    async def lag_seconds(self, session: AsyncSession) -> float:
        """Age of the oldest unpublished event. The outbox's key health number."""
        t = self.table
        oldest = await session.scalar(
            select(func.extract("epoch", func.now() - func.min(t.c.created_at))).where(
                t.c.published_at.is_(None)
            )
        )
        return float(oldest or 0)

    async def prune(self, session: AsyncSession, *, older_than: timedelta) -> int:
        """Delete events published longer ago than this. Returns how many."""
        t = self.table
        deleted = await session.execute(
            delete(t).where(t.c.published_at < func.now() - older_than).returning(t.c.id)
        )
        return len(deleted.all())
