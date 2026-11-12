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
from sqlalchemy import event as sa_event

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


# --- availability feed ------------------------------------------------------------


class AvailabilityFeed:
    """Tells viewers of a tournament which seats were just taken or freed.

    A sale produces hundreds of changes a second. Sending each as its own
    message would be thousands of WebSocket writes a second per instance, so
    changes are gathered per tournament and sent as one message every quarter
    of a second.

    This is a hint, not a source of truth, and is allowed to be lost or
    wrong for a moment: clients still fetch availability on a slow timer, and
    the claim itself is always decided in the database.
    """

    FLUSH_SECONDS = 0.25

    def __init__(self) -> None:
        self._redis: Redis | None = None
        self._task: asyncio.Task[None] | None = None
        self._pending: dict[str, tuple[set[str], set[str]]] = {}
        self._users = 0

    def start(self, redis: Redis) -> None:
        """Counted, so two apps in one process (tests, the demo) can share the feed."""
        self._users += 1
        if self._task is None:
            self._redis = redis
            self._task = asyncio.create_task(self._run(), name="availability-feed")

    async def stop(self) -> None:
        self._users = max(0, self._users - 1)
        if self._users or self._task is None:
            return
        self._task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await self._task
        self._task = self._redis = None
        self._pending.clear()

    def add(self, tournament_id: str, taken: set[str], freed: set[str]) -> None:
        if self._task is None:
            return  # nobody to tell (a script, or a test without the app)
        now_taken, now_freed = self._pending.setdefault(tournament_id, (set(), set()))
        # The latest news about a seat wins.
        now_freed -= taken
        now_taken |= taken
        now_taken -= freed
        now_freed |= freed

    async def flush(self) -> None:
        if self._redis is None or not self._pending:
            return
        pending, self._pending = self._pending, {}
        for tournament_id, (taken, freed) in pending.items():
            await publish(
                self._redis,
                tournament_id,
                {"type": "availability", "taken": sorted(taken), "freed": sorted(freed)},
            )

    async def _run(self) -> None:
        while True:
            await asyncio.sleep(self.FLUSH_SECONDS)
            try:
                await self.flush()
            except RedisError:
                log.warning("availability_feed_failed", exc_info=True)


feed = AvailabilityFeed()

_NOTES = "availability_notes"


def note_availability(
    session: Any, tournament_id: Any, *, taken: Any = (), freed: Any = ()
) -> None:
    """Announce a change once `session`'s transaction commits (and not if it rolls back).

    Call with no seats to say only "the numbers changed" (standing tickets).
    """
    notes = session.info.get(_NOTES)
    if notes is None:
        notes = session.info[_NOTES] = []

        def announce(_: Any) -> None:
            for tid, was_taken, was_freed in session.info.pop(_NOTES, []):
                feed.add(tid, was_taken, was_freed)

        sa_event.listen(session.sync_session, "after_commit", announce, once=True)
    notes.append((str(tournament_id), {str(s) for s in taken}, {str(s) for s in freed}))
