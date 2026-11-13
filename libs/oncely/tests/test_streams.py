from collections.abc import Awaitable, Callable
from typing import Any

from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from oncely import Event
from oncely.streams import StreamConsumer, stream_publisher

from .conftest import outbox

Sessions = async_sessionmaker[AsyncSession]
STREAM = "test:events"


async def publish(sessionmaker: Sessions, redis: Redis, *topics: str) -> None:
    async with sessionmaker.begin() as session:
        for topic in topics:
            await outbox.emit(session, topic, {"topic": topic}, headers={"trace": "abc"})
    await outbox.relay_once(sessionmaker, stream_publisher(redis, STREAM))


def consumer(redis: Redis, **options: object) -> StreamConsumer:
    # Retry immediately, so tests do not wait out a real backoff.
    return StreamConsumer(redis, stream=STREAM, retry_after_ms=0, **options)  # type: ignore[arg-type]


async def test_an_emitted_event_reaches_its_handlers_once(
    sessionmaker: Sessions, redis: Redis
) -> None:
    await publish(sessionmaker, redis, "order.paid", "order.cancelled")
    handled: list[Event] = []
    worker = consumer(redis)

    async def on_paid(event: Event) -> None:
        handled.append(event)

    worker.subscribe("order.paid", on_paid)

    assert await worker.run_once() == 2  # the one nobody listens for is settled too
    assert await worker.run_once() == 0
    assert [(event.topic, event.payload, event.headers) for event in handled] == [
        ("order.paid", {"topic": "order.paid"}, {"trace": "abc"})
    ]


async def test_a_failing_handler_is_retried_until_it_works(
    sessionmaker: Sessions, redis: Redis
) -> None:
    await publish(sessionmaker, redis, "order.paid")
    attempts: list[int] = []
    results: list[str] = []
    worker = consumer(redis, on_result=lambda event, result, seconds, error: results.append(result))

    async def flaky(event: Event) -> None:
        attempts.append(1)
        if len(attempts) < 3:
            raise ConnectionError("the mail server is down")

    worker.subscribe("order.paid", flaky)

    assert [await worker.run_once() for _ in range(4)] == [0, 0, 1, 0]
    assert results == ["retry", "retry", "ok"]
    assert await redis.xlen(worker.dead_letter_stream) == 0


async def test_a_poison_event_is_dead_lettered_and_stops_blocking(
    sessionmaker: Sessions, redis: Redis
) -> None:
    await publish(sessionmaker, redis, "order.paid")
    errors: list[BaseException | None] = []
    worker = consumer(
        redis, max_attempts=3, on_result=lambda event, result, seconds, error: errors.append(error)
    )

    async def broken(event: Event) -> None:
        raise ValueError("cannot be handled")

    worker.subscribe("order.paid", broken)

    assert [await worker.run_once() for _ in range(4)] == [0, 0, 1, 0]
    entries: Any = await redis.xrange(worker.dead_letter_stream)
    [(_, dead)] = entries
    assert (dead["topic"], dead["attempts"]) == ("order.paid", "3")
    assert all(isinstance(error, ValueError) for error in errors)


async def test_another_worker_takes_over_what_a_dead_one_left(
    sessionmaker: Sessions, redis: Redis
) -> None:
    await publish(sessionmaker, redis, "order.paid")
    handled: list[str] = []

    # The first worker is delivered the event and dies before acknowledging it.
    await redis.xgroup_create(STREAM, "workers", id="0", mkstream=True)
    await redis.xreadgroup("workers", "worker-that-died", {STREAM: ">"})

    survivor = consumer(redis)

    async def on_paid(event: Event) -> None:
        handled.append(event.topic)

    survivor.subscribe("order.paid", on_paid)

    assert await survivor.run_once() == 1
    assert handled == ["order.paid"]


async def test_around_wraps_every_handler_call(sessionmaker: Sessions, redis: Redis) -> None:
    await publish(sessionmaker, redis, "order.paid")
    trail: list[str] = []

    async def around(event: Event, handle: Callable[[], Awaitable[None]]) -> None:
        trail.append(f"open {event.headers}")
        await handle()
        trail.append("close")

    worker = consumer(redis, around=around)

    async def on_paid(event: Event) -> None:
        trail.append("handled")

    worker.subscribe("order.paid", on_paid)
    await worker.run_once()

    assert trail == ["open {'trace': 'abc'}", "handled", "close"]
