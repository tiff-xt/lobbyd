import asyncio
from datetime import timedelta

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from onceonly import KeyReused, Outcome, Refused, fingerprint, is_valid_key

from .conftest import SCHEMA, keys, things

Sessions = async_sessionmaker[AsyncSession]
PRINT = fingerprint("POST", "/orders", '{"seat":"A1"}')


async def count(sessionmaker: Sessions, table: object) -> int:
    async with sessionmaker() as session:
        return await session.scalar(select(func.count()).select_from(table)) or 0  # type: ignore[arg-type]


async def create_thing(
    sessionmaker: Sessions, key: str, *, scope: str = "ana", calls: list[int]
) -> Outcome:
    """One request: its own transaction, committed if the work finishes."""
    async with sessionmaker.begin() as session:

        async def work() -> tuple[int, str]:
            calls.append(1)
            await session.execute(things.insert().values(name=key))
            return 201, f'{{"made":"{key}"}}'

        return await keys.run(session, scope=scope, key=key, fingerprint=PRINT, work=work)


async def test_a_retry_gets_the_first_answer_and_does_no_work(sessionmaker: Sessions) -> None:
    calls: list[int] = []
    first = await create_thing(sessionmaker, "key-0001", calls=calls)
    again = await create_thing(sessionmaker, "key-0001", calls=calls)

    assert (first.status, first.body, first.replayed) == (201, '{"made":"key-0001"}', False)
    assert (again.status, again.body, again.replayed) == (201, '{"made":"key-0001"}', True)
    assert len(calls) == 1
    assert await count(sessionmaker, things) == 1


async def test_retries_racing_each_other_still_do_the_work_once(sessionmaker: Sessions) -> None:
    calls: list[int] = []
    outcomes = await asyncio.gather(
        *(create_thing(sessionmaker, "key-race", calls=calls) for _ in range(12))
    )

    assert len(calls) == 1
    assert sum(not outcome.replayed for outcome in outcomes) == 1
    assert {outcome.body for outcome in outcomes} == {'{"made":"key-race"}'}
    assert await count(sessionmaker, things) == 1


async def test_a_refusal_is_final_and_its_writes_are_undone(sessionmaker: Sessions) -> None:
    calls: list[int] = []

    async def attempt() -> Outcome:
        async with sessionmaker.begin() as session:

            async def work() -> tuple[int, str]:
                calls.append(1)
                await session.execute(things.insert().values(name="half done"))
                raise Refused(409, '{"code":"seat_taken"}')

            return await keys.run(
                session, scope="ana", key="key-0002", fingerprint=PRINT, work=work
            )

    first, again = await attempt(), await attempt()

    assert (first.status, first.body, first.replayed) == (409, '{"code":"seat_taken"}', False)
    assert (again.status, again.replayed) == (409, True)
    assert len(calls) == 1
    assert await count(sessionmaker, things) == 0


async def test_a_crash_stores_nothing_so_the_retry_starts_over(sessionmaker: Sessions) -> None:
    async def crash() -> None:
        async with sessionmaker.begin() as session:

            async def work() -> tuple[int, str]:
                await session.execute(things.insert().values(name="half done"))
                raise ConnectionError("the database went away")

            await keys.run(session, scope="ana", key="key-0003", fingerprint=PRINT, work=work)

    with pytest.raises(ConnectionError):
        await crash()
    assert await count(sessionmaker, keys.table) == 0
    assert await count(sessionmaker, things) == 0

    calls: list[int] = []
    outcome = await create_thing(sessionmaker, "key-0003", calls=calls)
    assert (outcome.status, outcome.replayed, len(calls)) == (201, False, 1)


async def test_the_same_key_with_a_different_request_is_rejected(sessionmaker: Sessions) -> None:
    await create_thing(sessionmaker, "key-0004", calls=[])

    async def work() -> tuple[int, str]:
        raise AssertionError("must not run")

    async with sessionmaker.begin() as session:
        with pytest.raises(KeyReused):
            await keys.run(
                session,
                scope="ana",
                key="key-0004",
                fingerprint=fingerprint("POST", "/orders", '{"seat":"B7"}'),
                work=work,
            )


async def test_two_callers_can_choose_the_same_key(sessionmaker: Sessions) -> None:
    calls: list[int] = []
    await create_thing(sessionmaker, "key-0005", scope="ana", calls=calls)
    other = await create_thing(sessionmaker, "key-0005", scope="ben", calls=calls)

    assert other.replayed is False
    assert len(calls) == 2


async def test_prune_removes_only_keys_past_the_retry_window(sessionmaker: Sessions) -> None:
    await create_thing(sessionmaker, "key-old1", calls=[])
    async with sessionmaker.begin() as session:
        await session.execute(
            text(f"UPDATE {SCHEMA}.idempotency_keys SET created_at = now() - interval '25 hours'")
        )
    await create_thing(sessionmaker, "key-new1", calls=[])

    async with sessionmaker.begin() as session:
        assert await keys.prune(session, older_than=timedelta(hours=24)) == 1
    assert await count(sessionmaker, keys.table) == 1


def test_fingerprint_separates_its_parts() -> None:
    assert fingerprint("ab", "c") != fingerprint("a", "bc")
    assert fingerprint("a", b"b") == fingerprint("a", "b")


@pytest.mark.parametrize(
    ("key", "valid"),
    [("0f8fad5b-d9cb-469f-a165-70867728950e", True), ("short", False), ("has a space", False)],
)
def test_key_format(key: str, valid: bool) -> None:
    assert is_valid_key(key) is valid
    assert is_valid_key("x" * 256) is False
