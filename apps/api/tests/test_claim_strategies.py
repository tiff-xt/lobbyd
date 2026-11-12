"""Every claim strategy must be correct. They are allowed to differ only in speed.

docs/benchmarks.md compares how they behave under load. This file is what
makes that comparison fair: none of them is permitted to oversell.
"""

import asyncio
import random

import pytest
from fastapi import FastAPI
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from lobbyd.config import Settings
from tests.helpers import hold, make_buyers, open_sale
from tests.invariants import assert_invariants

Sessions = async_sessionmaker[AsyncSession]
STRATEGIES = ["lock_then_claim", "claim_only", "skip_locked", "redis_lock"]


@pytest.fixture(params=STRATEGIES)
def strategy(request, settings: Settings):  # type: ignore[no-untyped-def]
    settings.claim_strategy = request.param
    yield request.param
    settings.claim_strategy = "lock_then_claim"


async def test_one_seat_goes_to_exactly_one_buyer(
    strategy: str, client: AsyncClient, sessionmaker: Sessions, settings: Settings
) -> None:
    sale = await open_sale(client, sessionmaker)
    buyers = await make_buyers(sessionmaker, settings, 60)
    responses = await asyncio.gather(
        *(hold(client, buyer, sale, seats=sale.seat_ids[:1]) for buyer in buyers)
    )
    assert sorted({r.status_code for r in responses}) == [201, 409]
    assert [r.status_code for r in responses].count(201) == 1
    await assert_invariants(sessionmaker)


async def test_overlapping_claims_never_share_a_seat(
    strategy: str, client: AsyncClient, app: FastAPI, sessionmaker: Sessions, settings: Settings
) -> None:
    sale = await open_sale(client, sessionmaker, seats=40)
    buyers = await make_buyers(sessionmaker, settings, 150)
    rng = random.Random(5)
    wanted = []
    for _ in buyers:
        size = rng.randint(1, 4)
        start = rng.randrange(0, 40 - size + 1)
        seats = sale.seat_ids[start : start + size]
        rng.shuffle(seats)
        wanted.append(seats)

    responses = await asyncio.gather(
        *(
            hold(client, buyer, sale, seats=seats)
            for buyer, seats in zip(buyers, wanted, strict=True)
        )
    )
    # Refusals are fine. Server errors are not, for any strategy.
    assert {r.status_code for r in responses} <= {201, 409}

    held = [
        seat
        for response, seats in zip(responses, wanted, strict=True)
        if response.status_code == 201
        for seat in seats
    ]
    assert held, "nobody got anything"
    assert len(held) == len(set(held)), "a seat was held twice"
    for response, seats in zip(responses, wanted, strict=True):
        if response.status_code == 201:
            assert sorted(i["seat_id"] for i in response.json()["items"]) == sorted(seats)
    await assert_invariants(sessionmaker)
    await app.state.redis.flushdb()  # redis_lock leaves short-lived keys behind
