import asyncio
from collections.abc import Sequence
from datetime import timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from oncely import Event

from .conftest import SCHEMA, outbox, things

Sessions = async_sessionmaker[AsyncSession]


class Recorder:
    def __init__(self) -> None:
        self.events: list[Event] = []

    async def __call__(self, events: Sequence[Event]) -> None:
        self.events += events


async def emit(sessionmaker: Sessions, *topics: str) -> None:
    async with sessionmaker.begin() as session:
        for number, topic in enumerate(topics):
            await outbox.emit(session, topic, {"n": number})


async def test_an_event_is_published_only_if_its_change_committed(sessionmaker: Sessions) -> None:
    async with sessionmaker.begin() as session:
        await session.execute(things.insert().values(name="kept"))
        await outbox.emit(session, "thing.made", {"name": "kept"}, headers={"trace": "abc"})

    async def fails_after_emitting() -> None:
        async with sessionmaker.begin() as session:
            await session.execute(things.insert().values(name="lost"))
            await outbox.emit(session, "thing.made", {"name": "lost"})
            raise RuntimeError("the request failed after emitting")

    with pytest.raises(RuntimeError):
        await fails_after_emitting()

    published = Recorder()
    await outbox.relay_once(sessionmaker, published)

    assert [(event.topic, event.payload, event.headers) for event in published.events] == [
        ("thing.made", {"name": "kept"}, {"trace": "abc"})
    ]


async def test_events_are_published_in_order_and_once_on_the_happy_path(
    sessionmaker: Sessions,
) -> None:
    await emit(sessionmaker, "a", "b", "c")
    published = Recorder()

    assert len(await outbox.relay_once(sessionmaker, published, batch=2)) == 2
    assert len(await outbox.relay_once(sessionmaker, published, batch=2)) == 1
    assert await outbox.relay_once(sessionmaker, published) == []
    assert [event.topic for event in published.events] == ["a", "b", "c"]


async def test_a_failed_publish_leaves_the_batch_for_next_time(sessionmaker: Sessions) -> None:
    await emit(sessionmaker, "a", "b")

    async def broken(events: Sequence[Event]) -> None:
        raise ConnectionError("the broker is down")

    with pytest.raises(ConnectionError):
        await outbox.relay_once(sessionmaker, broken)

    published = Recorder()
    await outbox.relay_once(sessionmaker, published)
    assert [event.topic for event in published.events] == ["a", "b"]


async def test_two_relays_never_take_the_same_event(sessionmaker: Sessions) -> None:
    await emit(sessionmaker, *(f"t{n}" for n in range(6)))
    both_hold_a_batch = asyncio.Barrier(2)
    seen: list[str] = []

    async def slow(events: Sequence[Event]) -> None:
        # Each relay publishes while the other's rows are still locked.
        await both_hold_a_batch.wait()
        seen.extend(str(event.id) for event in events)

    await asyncio.gather(
        outbox.relay_once(sessionmaker, slow, batch=3),
        outbox.relay_once(sessionmaker, slow, batch=3),
    )

    assert len(seen) == 6
    assert len(set(seen)) == 6


async def test_lag_is_the_age_of_the_oldest_unpublished_event(sessionmaker: Sessions) -> None:
    async with sessionmaker() as session:
        assert await outbox.lag_seconds(session) == 0

    await emit(sessionmaker, "a")
    async with sessionmaker.begin() as session:
        await session.execute(
            text(f"UPDATE {SCHEMA}.outbox SET created_at = now() - interval '90 seconds'")
        )
        assert 89 < await outbox.lag_seconds(session) < 95

    await outbox.relay_once(sessionmaker, Recorder())
    async with sessionmaker() as session:
        assert await outbox.lag_seconds(session) == 0


async def test_prune_removes_only_events_published_long_ago(sessionmaker: Sessions) -> None:
    await emit(sessionmaker, "old")
    await outbox.relay_once(sessionmaker, Recorder())
    async with sessionmaker.begin() as session:
        await session.execute(
            text(f"UPDATE {SCHEMA}.outbox SET published_at = now() - interval '8 days'")
        )
    await emit(sessionmaker, "recent", "pending")
    await outbox.relay_once(sessionmaker, Recorder(), batch=1)

    async with sessionmaker.begin() as session:
        assert await outbox.prune(session, older_than=timedelta(days=7)) == 1


def test_ids_made_in_the_same_millisecond_still_sort_in_order() -> None:
    from oncely.outbox import uuid7

    ids = [uuid7() for _ in range(5000)]
    assert ids == sorted(ids)
    assert len(set(ids)) == len(ids)
    assert all(made.version == 7 for made in ids)
