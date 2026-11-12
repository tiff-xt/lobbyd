"""Consuming events from the stream the outbox relay fills.

Redis Streams with a consumer group: each event goes to one worker, stays
"pending" until that worker acknowledges it, and can be taken over by another
worker if the first one dies. An event whose handlers keep failing is moved
to a dead-letter stream after a fixed number of attempts, so one poison
message cannot block the rest or retry forever.
"""

import asyncio
import json
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

import structlog
from redis.asyncio import Redis
from redis.exceptions import ResponseError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from lobbyd.config import Settings
from lobbyd.platform import metrics, tracing
from lobbyd.platform.outbox import STREAM

log = structlog.get_logger()

GROUP = "workers"
DEAD_LETTER_STREAM = "lobbyd:events:dead"
_ATTEMPTS = "lobbyd:events:attempts"
_CONCURRENCY = 16


@dataclass
class EventContext:
    sessionmaker: async_sessionmaker[AsyncSession]
    redis: Redis
    settings: Settings
    provider: Any
    consumer: str = field(default_factory=lambda: f"worker-{uuid.uuid4().hex[:8]}")


Handler = Callable[[EventContext, dict[str, Any]], Awaitable[None]]

_handlers: dict[str, list[Handler]] = {}


def subscribe(topic: str, handler: Handler) -> None:
    handlers = _handlers.setdefault(topic, [])
    if handler not in handlers:
        handlers.append(handler)


async def _ensure_group(redis: Redis) -> None:
    try:
        await redis.xgroup_create(STREAM, GROUP, id="0", mkstream=True)
    except ResponseError as exc:
        if "BUSYGROUP" not in str(exc):  # already exists: fine
            raise


async def consume_once(ctx: EventContext, *, count: int = 50, block_ms: int | None = None) -> int:
    """Handle one batch: first events whose earlier attempt failed or whose
    worker died, then new ones. Returns how many were handled successfully."""
    await _ensure_group(ctx.redis)

    # Take over anything delivered but not acknowledged for long enough.
    claimed: Any = await ctx.redis.xautoclaim(
        STREAM,
        GROUP,
        ctx.consumer,
        min_idle_time=ctx.settings.event_retry_after_ms,
        start_id="0-0",
        count=count,
    )
    fresh: Any = await ctx.redis.xreadgroup(
        GROUP, ctx.consumer, {STREAM: ">"}, count=count, block=block_ms
    )
    messages: list[tuple[str, dict[str, str]]] = list(claimed[1])
    messages += [message for _, batch in fresh for message in batch]

    async def one(message_id: str, fields: dict[str, str]) -> bool:
        if not fields:  # the entry was trimmed from the stream
            await ctx.redis.xack(STREAM, GROUP, message_id)
            return False
        return await _handle(ctx, message_id, fields)

    # Handlers mostly wait on the network (the payment provider, the
    # database), so a batch is worked on together, not one event at a time.
    # Handlers are idempotent and events for different orders are independent.
    gate = asyncio.Semaphore(_CONCURRENCY)

    async def limited(message: tuple[str, dict[str, str]]) -> bool:
        async with gate:
            return await one(*message)

    return sum(await asyncio.gather(*(limited(message) for message in messages)))


async def _handle(ctx: EventContext, message_id: str, fields: dict[str, str]) -> bool:
    topic, event_id = fields["topic"], fields["id"]
    structlog.contextvars.bind_contextvars(event_id=event_id, topic=topic)
    carrier = json.loads(fields["trace"]) if "trace" in fields else None
    started = time.perf_counter()
    outcome = "ok"
    try:
        # A span that continues the trace of the request that emitted the event.
        with tracing.continued(f"handle {topic}", carrier, **{"messaging.message.id": event_id}):
            payload = json.loads(fields["payload"])
            for handler in _handlers.get(topic, []):
                await handler(ctx, payload)
    except Exception:
        attempts = await ctx.redis.hincrby(_ATTEMPTS, message_id, 1)
        if attempts < ctx.settings.event_max_attempts:
            # Leave it pending. It is retried once it has been idle long enough.
            log.warning("event_failed", attempt=attempts, exc_info=True)
            metrics.EVENTS_HANDLED.labels(topic, "retry").inc()
            return False
        log.error("event_dead_lettered", attempts=attempts, exc_info=True)
        outcome = "dead"
        dead: dict[Any, Any] = {**fields, "attempts": str(attempts)}
        await ctx.redis.xadd(DEAD_LETTER_STREAM, dead)
    finally:
        metrics.EVENT_DURATION.labels(topic).observe(time.perf_counter() - started)
        structlog.contextvars.unbind_contextvars("event_id", "topic")
    metrics.EVENTS_HANDLED.labels(topic, outcome).inc()

    # Reached on success, and after dead-lettering.
    pipeline = ctx.redis.pipeline(transaction=False)
    pipeline.xack(STREAM, GROUP, message_id)
    pipeline.hdel(_ATTEMPTS, message_id)
    await pipeline.execute()
    return True


async def trim(redis: Redis, *, keep: int = 100_000) -> None:
    """Bound the stream's size. Acknowledged entries are not needed again."""
    await redis.xtrim(STREAM, maxlen=keep, approximate=True)
