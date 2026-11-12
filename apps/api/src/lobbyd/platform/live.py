"""Live updates to browsers: Redis pub/sub in, WebSockets out.

Every API process runs one Hub. A Hub holds a single Redis subscription and
fans each message out to the WebSocket clients connected to that process. So
an update published anywhere, by any process, reaches every viewer whichever
instance they happen to be connected to.

Pub/sub does not store messages: one sent while a process was disconnected
is gone. The hub therefore never tries to hide a gap. A client that might
have missed something is disconnected, and on reconnecting it is sent the
current state first.
"""

import asyncio
import contextlib
import json
from typing import Any

import structlog
from redis.asyncio import Redis
from redis.exceptions import RedisError

from lobbyd.platform import metrics

log = structlog.get_logger()

CHANNEL_PREFIX = "live:"
# Messages a slow client may fall behind by before it is dropped.
MAX_BACKLOG = 64


async def publish(redis: Redis, topic: str, message: dict[str, Any]) -> None:
    await redis.publish(CHANNEL_PREFIX + topic, json.dumps(message, separators=(",", ":")))


class Subscription:
    """One client's view of one topic. Iterate it; it ends if the client must resync."""

    def __init__(self, hub: "Hub", topic: str) -> None:
        self._hub = hub
        self.topic = topic
        self._queue: asyncio.Queue[str | None] = asyncio.Queue(maxsize=MAX_BACKLOG)

    def _offer(self, message: str) -> bool:
        try:
            self._queue.put_nowait(message)
        except asyncio.QueueFull:
            return False
        return True

    def _end(self) -> None:
        # Make room for the end marker: the backlog no longer matters.
        while True:
            try:
                self._queue.put_nowait(None)
                return
            except asyncio.QueueFull:
                self._queue.get_nowait()

    def close(self) -> None:
        self._hub._drop(self)

    def __aiter__(self) -> "Subscription":
        return self

    async def __anext__(self) -> str:
        message = await self._queue.get()
        if message is None:
            raise StopAsyncIteration
        return message


class Hub:
    def __init__(self, redis: Redis) -> None:
        self._redis = redis
        self._subscribers: dict[str, set[Subscription]] = {}
        self._task: asyncio.Task[None] | None = None
        self._listening = asyncio.Event()

    async def start(self) -> None:
        self._task = asyncio.create_task(self._run(), name="live-hub")
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(self._listening.wait(), timeout=5)

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
        self._disconnect_everyone()

    def subscribe(self, topic: str) -> Subscription:
        subscription = Subscription(self, topic)
        self._subscribers.setdefault(topic, set()).add(subscription)
        metrics.LIVE_CLIENTS.inc()
        return subscription

    @property
    def clients(self) -> int:
        return sum(len(group) for group in self._subscribers.values())

    def _drop(self, subscription: Subscription) -> None:
        group = self._subscribers.get(subscription.topic)
        if group is not None and subscription in group:
            metrics.LIVE_CLIENTS.dec()
            group.discard(subscription)
            if not group:
                del self._subscribers[subscription.topic]
        subscription._end()

    def _disconnect_everyone(self) -> None:
        for group in list(self._subscribers.values()):
            for subscription in list(group):
                self._drop(subscription)

    def _deliver(self, topic: str, message: str) -> None:
        for subscription in list(self._subscribers.get(topic, ())):
            if not subscription._offer(message):
                # Too far behind to catch up message by message. Cut it loose;
                # it reconnects and starts from the current state.
                log.warning("live_client_dropped", topic=topic)
                self._drop(subscription)

    async def _run(self) -> None:
        delay = 0.5
        while True:
            try:
                async with self._redis.pubsub() as pubsub:
                    await pubsub.psubscribe(CHANNEL_PREFIX + "*")
                    self._listening.set()
                    delay = 0.5
                    async for message in pubsub.listen():
                        if message["type"] == "pmessage":
                            topic = message["channel"][len(CHANNEL_PREFIX) :]
                            self._deliver(topic, message["data"])
            except RedisError as exc:
                # Anything published while we were away is lost to us, so
                # nobody connected here can be trusted to be up to date.
                self._listening.clear()
                self._disconnect_everyone()
                log.warning("live_hub_reconnecting", error=str(exc))
                await asyncio.sleep(delay)
                delay = min(delay * 2, 10)
