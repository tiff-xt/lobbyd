"""Redis Streams: a publisher for the outbox relay, and a consumer.

The consumer uses a consumer group: each event goes to one worker, stays
"pending" until that worker acknowledges it, and can be taken over by another
worker if the first one dies. An event whose handlers keep failing is moved
to a dead-letter stream after a fixed number of attempts, so one poison
message cannot block the rest or retry forever.
"""

import asyncio
import json
import time
import uuid
from collections.abc import Awaitable, Callable, Sequence
from typing import Any, Literal

from redis.asyncio import Redis
from redis.exceptions import ResponseError

from oncely.outbox import Event, Publisher

Handler = Callable[[Event], Awaitable[None]]
#: Wraps the handling of one event: open a span, bind log context, time it.
Around = Callable[[Event, Callable[[], Awaitable[None]]], Awaitable[None]]
Result = Literal["ok", "retry", "dead"]
#: Told how each attempt ended and how long it took, for metrics and logs.
#: `error` is the exception behind a "retry" or "dead".
OnResult = Callable[[Event, Result, float, BaseException | None], None]


def stream_publisher(redis: Redis, stream: str) -> Publisher:
    """A publisher that appends each event to a Redis stream."""

    async def publish(events: Sequence[Event]) -> None:
        pipeline = redis.pipeline(transaction=False)
        for event in events:
            pipeline.xadd(stream, _encode(event))  # type: ignore[arg-type]
        await pipeline.execute()

    return publish


def _encode(event: Event) -> dict[str, str]:
    fields = {"id": str(event.id), "topic": event.topic, "payload": json.dumps(event.payload)}
    if event.headers:
        fields["headers"] = json.dumps(event.headers)
    return fields


def _decode(fields: dict[str, str]) -> Event:
    return Event(
        id=uuid.UUID(fields["id"]),
        topic=fields["topic"],
        payload=json.loads(fields["payload"]),
        headers=json.loads(fields["headers"]) if "headers" in fields else None,
    )


class StreamConsumer:
    """One worker in a consumer group. Run as many as you like.

    Handlers must be safe to run twice: an event is delivered again if a
    handler raised, if the worker died before acknowledging it, or if the
    relay published it twice.
    """

    def __init__(
        self,
        redis: Redis,
        *,
        stream: str,
        group: str = "workers",
        consumer: str | None = None,
        retry_after_ms: int = 30_000,
        max_attempts: int = 5,
        concurrency: int = 16,
        dead_letter_stream: str | None = None,
        around: Around | None = None,
        on_result: OnResult | None = None,
    ) -> None:
        self.redis = redis
        self.stream = stream
        self.group = group
        self.consumer = consumer or f"worker-{uuid.uuid4().hex[:8]}"
        self.retry_after_ms = retry_after_ms
        self.max_attempts = max_attempts
        self.concurrency = concurrency
        self.dead_letter_stream = dead_letter_stream or f"{stream}:dead"
        self._attempts = f"{stream}:attempts"
        self._around = around
        self._on_result = on_result
        self._handlers: dict[str, list[Handler]] = {}

    def subscribe(self, topic: str, handler: Handler) -> None:
        handlers = self._handlers.setdefault(topic, [])
        if handler not in handlers:
            handlers.append(handler)

    async def _ensure_group(self) -> None:
        try:
            await self.redis.xgroup_create(self.stream, self.group, id="0", mkstream=True)
        except ResponseError as exc:
            if "BUSYGROUP" not in str(exc):  # already exists: fine
                raise

    async def run_once(self, *, count: int = 50, block_ms: int | None = None) -> int:
        """Handle one batch: first events whose earlier attempt failed or whose
        worker died, then new ones. Returns how many were settled (handled, or
        given up on and dead-lettered)."""
        await self._ensure_group()

        # Take over anything delivered but not acknowledged for long enough.
        claimed: Any = await self.redis.xautoclaim(
            self.stream,
            self.group,
            self.consumer,
            min_idle_time=self.retry_after_ms,
            start_id="0-0",
            count=count,
        )
        fresh: Any = await self.redis.xreadgroup(
            self.group, self.consumer, {self.stream: ">"}, count=count, block=block_ms
        )
        messages: list[tuple[str, dict[str, str]]] = list(claimed[1])
        messages += [message for _, batch in fresh for message in batch]

        # Handlers mostly wait on the network, so a batch is worked on
        # together, not one event at a time.
        gate = asyncio.Semaphore(self.concurrency)

        async def one(message_id: str, fields: dict[str, str]) -> bool:
            async with gate:
                if not fields:  # the entry was trimmed from the stream
                    await self.redis.xack(self.stream, self.group, message_id)
                    return False
                return await self._handle(message_id, fields)

        return sum(await asyncio.gather(*(one(*message) for message in messages)))

    async def _handle(self, message_id: str, fields: dict[str, str]) -> bool:
        event = _decode(fields)
        started = time.perf_counter()

        async def handlers() -> None:
            for handler in self._handlers.get(event.topic, []):
                await handler(event)

        def report(result: Result, error: BaseException | None = None) -> None:
            if self._on_result:
                self._on_result(event, result, time.perf_counter() - started, error)

        try:
            if self._around:
                await self._around(event, handlers)
            else:
                await handlers()
        except Exception as exc:
            attempts = await self.redis.hincrby(self._attempts, message_id, 1)
            if attempts < self.max_attempts:
                # Leave it pending. It is retried once it has been idle long enough.
                report("retry", exc)
                return False
            dead: dict[Any, Any] = {**fields, "attempts": str(attempts)}
            await self.redis.xadd(self.dead_letter_stream, dead)
            report("dead", exc)
        else:
            report("ok")

        # Reached on success, and after dead-lettering.
        pipeline = self.redis.pipeline(transaction=False)
        pipeline.xack(self.stream, self.group, message_id)
        pipeline.hdel(self._attempts, message_id)
        await pipeline.execute()
        return True

    async def trim(self, *, keep: int = 100_000) -> None:
        """Bound the stream's size. Acknowledged entries are not needed again."""
        await self.redis.xtrim(self.stream, maxlen=keep, approximate=True)
