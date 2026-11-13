"""Consuming events from the stream the outbox relay fills.

Redis Streams with a consumer group: each event goes to one worker, stays
"pending" until that worker acknowledges it, and can be taken over by another
worker if the first one dies. An event whose handlers keep failing is moved
to a dead-letter stream after a fixed number of attempts, so one poison
message cannot block the rest or retry forever.
"""

import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

import structlog
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from lobbyd.config import Settings
from lobbyd.platform import metrics, tracing
from lobbyd.platform.outbox import STREAM
from oncely import Event
from oncely.streams import Result, StreamConsumer

log = structlog.get_logger()

GROUP = "workers"
DEAD_LETTER_STREAM = f"{STREAM}:dead"


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


async def _around(event: Event, handle: Callable[[], Awaitable[None]]) -> None:
    """Handle one event inside the trace of the request that emitted it."""
    structlog.contextvars.bind_contextvars(event_id=str(event.id), topic=event.topic)
    try:
        with tracing.continued(
            f"handle {event.topic}", event.headers, **{"messaging.message.id": str(event.id)}
        ):
            await handle()
    finally:
        structlog.contextvars.unbind_contextvars("event_id", "topic")


def _report(event: Event, result: Result, seconds: float, error: BaseException | None) -> None:
    metrics.EVENT_DURATION.labels(event.topic).observe(seconds)
    metrics.EVENTS_HANDLED.labels(event.topic, result).inc()
    context = {"event_id": str(event.id), "topic": event.topic, "exc_info": error}
    if result == "retry":
        log.warning("event_failed", **context)
    elif result == "dead":
        log.error("event_dead_lettered", **context)


def _consumer(ctx: EventContext) -> StreamConsumer:
    consumer = StreamConsumer(
        ctx.redis,
        stream=STREAM,
        group=GROUP,
        consumer=ctx.consumer,
        retry_after_ms=ctx.settings.event_retry_after_ms,
        max_attempts=ctx.settings.event_max_attempts,
        dead_letter_stream=DEAD_LETTER_STREAM,
        around=_around,
        on_result=_report,
    )
    for topic, handlers in _handlers.items():
        for handler in handlers:

            async def call(event: Event, handler: Handler = handler) -> None:
                await handler(ctx, event.payload)

            consumer.subscribe(topic, call)
    return consumer


async def consume_once(ctx: EventContext, *, count: int = 50, block_ms: int | None = None) -> int:
    """Handle one batch: first events whose earlier attempt failed or whose
    worker died, then new ones. Returns how many were settled.

    Retries, takeover from dead workers and the dead-letter stream are the
    oncely consumer's. Handlers are idempotent, and events for different
    orders are independent, so a batch is worked on together.
    """
    return await _consumer(ctx).run_once(count=count, block_ms=block_ms)


async def trim(redis: Redis, *, keep: int = 100_000) -> None:
    """Bound the stream's size. Acknowledged entries are not needed again."""
    await redis.xtrim(STREAM, maxlen=keep, approximate=True)
