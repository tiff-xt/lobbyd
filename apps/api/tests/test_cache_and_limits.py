"""Caching and rate limiting: fast, and never at the cost of being right."""

import asyncio
import json

import pytest
from fastapi import FastAPI
from httpx import AsyncClient
from redis.exceptions import ConnectionError as RedisConnectionError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from lobbyd.config import Settings
from lobbyd.platform import cache
from lobbyd.platform.cache import cached
from tests.helpers import (
    auth_headers,
    hold,
    key,
    make_buyers,
    open_sale,
    published,
    tournament_body,
    venue_body,
)

Sessions = async_sessionmaker[AsyncSession]


class Counter:
    """A loader that counts how often it runs, and can be slow."""

    def __init__(self, delay: float = 0.0) -> None:
        self.calls = 0
        self.delay = delay

    async def __call__(self) -> str:
        self.calls += 1
        await asyncio.sleep(self.delay)
        return json.dumps({"build": self.calls})


@pytest.fixture(autouse=True)
def cache_on():  # type: ignore[no-untyped-def]
    cache.enabled = True
    yield
    cache.enabled = False


# --- the cache itself ---------------------------------------------------------


async def test_a_value_is_built_once_and_then_served_from_cache(app: FastAPI) -> None:
    load = Counter()
    first = await cached(app.state.redis, "k", load, fresh_seconds=60)
    again = await cached(app.state.redis, "k", load, fresh_seconds=60)
    assert first == again == '{"build": 1}'
    assert load.calls == 1


async def test_a_stampede_builds_the_value_once(app: FastAPI) -> None:
    """A hundred requests arrive at the same instant for something not cached."""
    load = Counter(delay=0.15)
    results = await asyncio.gather(
        *(cached(app.state.redis, "hot", load, fresh_seconds=60) for _ in range(100))
    )
    assert load.calls == 1
    assert set(results) == {'{"build": 1}'}


async def test_a_stale_value_is_served_while_one_request_refreshes_it(app: FastAPI) -> None:
    load = Counter(delay=0.2)
    await cached(app.state.redis, "s", load, fresh_seconds=0.05, stale_seconds=30)
    await asyncio.sleep(0.1)  # past its fresh time now

    started = asyncio.get_running_loop().time()
    results = await asyncio.gather(
        *(
            cached(app.state.redis, "s", load, fresh_seconds=0.05, stale_seconds=30)
            for _ in range(50)
        )
    )
    # Exactly one request did the slow refresh and got the new value.
    assert load.calls == 2
    assert sorted(set(results)) == ['{"build": 1}', '{"build": 2}']
    assert results.count('{"build": 2}') == 1
    # Nobody else waited for it. (The gather as a whole takes as long as the one refresh.)
    assert asyncio.get_running_loop().time() - started < 0.5


async def test_an_entry_is_gone_after_fresh_plus_stale(app: FastAPI) -> None:
    load = Counter()
    await cached(app.state.redis, "e", load, fresh_seconds=0.05, stale_seconds=0.05)
    await asyncio.sleep(0.15)
    assert await cached(app.state.redis, "e", load, fresh_seconds=60) == '{"build": 2}'


async def test_a_failed_build_is_not_cached_and_releases_the_lock(app: FastAPI) -> None:
    async def broken() -> str:
        raise RuntimeError("database is down")

    with pytest.raises(RuntimeError):
        await cached(app.state.redis, "b", broken, fresh_seconds=60)
    # The next request can build it straight away: no five second wait on a dead lock.
    load = Counter()
    assert await asyncio.wait_for(cached(app.state.redis, "b", load, fresh_seconds=60), 0.5)
    assert load.calls == 1


async def test_without_redis_the_value_is_simply_loaded() -> None:
    class DownRedis:
        async def get(self, key: str) -> None:
            raise RedisConnectionError("down")

    load = Counter()
    before = cache.stats.errors
    assert await cached(DownRedis(), "k", load, fresh_seconds=60) == '{"build": 1}'  # type: ignore[arg-type]
    assert await cached(DownRedis(), "k", load, fresh_seconds=60) == '{"build": 2}'  # type: ignore[arg-type]
    assert cache.stats.errors == before + 2


# --- cached endpoints ---------------------------------------------------------


async def test_cached_endpoints_return_the_same_bytes_as_uncached(
    client: AsyncClient, app: FastAPI, sessionmaker: Sessions
) -> None:
    headers = await auth_headers(client, sessionmaker)
    venue = (await client.post("/venues", json=venue_body(2, 3, 4), headers=headers)).json()
    tournament = await published(client, headers, venue_id=venue["id"])
    paths = [
        f"/venues/{venue['id']}/seats",
        f"/tournaments/{tournament['slug']}",
        f"/tournaments/{tournament['slug']}/availability",
        "/tournaments",
        "/tournaments?q=starfall&limit=5",
        "/games",
    ]
    for path in paths:
        cold = await client.get(path)
        warm = await client.get(path)
        assert cold.status_code == warm.status_code == 200, path
        assert cold.content == warm.content, path
        assert warm.headers["content-type"] == "application/json"
    assert (await client.get(paths[0])).headers["cache-control"] == "public, max-age=3600"


async def test_an_edit_shows_up_at_once(client: AsyncClient, sessionmaker: Sessions) -> None:
    headers = await auth_headers(client, sessionmaker)
    slug = (await published(client, headers))["slug"]
    assert (await client.get(f"/tournaments/{slug}")).json()["title"] == "Starfall Open"

    await client.patch(f"/tournaments/{slug}", json={"title": "Renamed"}, headers=headers)
    # Not thirty seconds later: the edit drops the cached copy.
    assert (await client.get(f"/tournaments/{slug}")).json()["title"] == "Renamed"


async def test_a_draft_is_never_cached_or_leaked(
    client: AsyncClient, sessionmaker: Sessions
) -> None:
    owner = await auth_headers(client, sessionmaker)
    await client.post("/tournaments", json=tournament_body(), headers=owner)
    path = "/tournaments/starfall-open"

    # The owner reads their draft (twice, so a cache would be warm)...
    assert (await client.get(path, headers=owner)).status_code == 200
    assert (await client.get(path, headers=owner)).status_code == 200
    # ...and it is still invisible to everyone else.
    assert (await client.get(path)).status_code == 404
    other = await auth_headers(client, sessionmaker, "other@example.com")
    assert (await client.get(path, headers=other)).status_code == 404


async def test_stale_availability_can_refuse_a_hold_but_never_sell_a_seat_twice(
    client: AsyncClient, sessionmaker: Sessions, settings: Settings
) -> None:
    sale = await open_sale(client, sessionmaker)
    first, second = await make_buyers(sessionmaker, settings, 2)
    path = f"/tournaments/{sale.slug}/availability"
    assert (await client.get(path)).json()["taken_seat_ids"] == []  # now cached

    assert (await hold(client, first, sale, seats=sale.seat_ids[:1])).status_code == 201
    # The cached picture still says the seat is free...
    assert (await client.get(path)).json()["taken_seat_ids"] == []
    # ...but the claim is checked against the database, not the cache.
    refused = await hold(client, second, sale, seats=sale.seat_ids[:1])
    assert (refused.status_code, refused.json()["code"]) == (409, "seats_unavailable")

    await asyncio.sleep(1.1)  # and the picture catches up within its fresh time
    await client.get(path)  # triggers the refresh
    assert (await client.get(path)).json()["taken_seat_ids"] == [sale.seat_ids[0]]


async def test_a_spike_of_readers_runs_the_availability_query_once(
    client: AsyncClient, sessionmaker: Sessions
) -> None:
    sale = await open_sale(client, sessionmaker)
    before = cache.stats.rebuilds
    responses = await asyncio.gather(
        *(client.get(f"/tournaments/{sale.slug}/availability") for _ in range(200))
    )
    assert {r.status_code for r in responses} == {200}
    assert len({r.content for r in responses}) == 1
    assert cache.stats.rebuilds - before == 1


# --- rate limits --------------------------------------------------------------


@pytest.fixture
def limits_on(settings: Settings):  # type: ignore[no-untyped-def]
    settings.rate_limit_enabled = True
    yield
    settings.rate_limit_enabled = False


async def test_login_attempts_are_limited_per_address(client: AsyncClient, limits_on: None) -> None:
    body = {"email": "nobody@example.com", "password": "wrong-password"}
    statuses = [(await client.post("/auth/login", json=body)).status_code for _ in range(12)]
    # A burst of ten is let through (and refused as wrong), then the door closes.
    assert statuses == [401] * 10 + [429] * 2

    limited = await client.post("/auth/login", json=body)
    assert limited.json()["code"] == "rate_limited"
    assert int(limited.headers["retry-after"]) >= 1
    assert limited.json()["retry_after"] == int(limited.headers["retry-after"])


async def test_booking_is_limited_per_user_not_per_address(
    client: AsyncClient, sessionmaker: Sessions, settings: Settings, limits_on: None
) -> None:
    sale = await open_sale(client, sessionmaker)
    greedy, patient = await make_buyers(sessionmaker, settings, 2)
    body = {"tournament": sale.slug, "seat_ids": [sale.seat_ids[0]]}

    statuses = [
        (await client.post("/holds", json=body, headers={**greedy, **key()})).status_code
        for _ in range(14)
    ]
    assert statuses[0] == 201
    assert 429 in statuses[10:]
    assert 429 not in statuses[:10]
    # Someone else, from the same address, is not affected.
    other = await client.post(
        "/holds",
        json={"tournament": sale.slug, "seat_ids": [sale.seat_ids[1]]},
        headers={**patient, **key()},
    )
    assert other.status_code == 201


async def test_tokens_come_back_with_time(
    app: FastAPI, client: AsyncClient, limits_on: None
) -> None:
    body = {"email": "nobody@example.com", "password": "wrong-password"}
    for _ in range(10):
        await client.post("/auth/login", json=body)
    assert (await client.post("/auth/login", json=body)).status_code == 429

    # Wind the bucket's clock back six seconds: at one token per five
    # seconds, that is one more attempt.
    bucket = "rl:auth:127.0.0.1"
    at = float(await app.state.redis.hget(bucket, "at"))
    await app.state.redis.hset(bucket, "at", at - 6)
    assert (await client.post("/auth/login", json=body)).status_code == 401
    assert (await client.post("/auth/login", json=body)).status_code == 429


async def test_forwarded_addresses_are_only_believed_when_configured(
    client: AsyncClient, settings: Settings, limits_on: None
) -> None:
    body = {"email": "nobody@example.com", "password": "wrong-password"}

    async def attempts(count: int, address: str) -> list[int]:
        headers = {"X-Forwarded-For": address}
        return [
            (await client.post("/auth/login", json=body, headers=headers)).status_code
            for _ in range(count)
        ]

    # Not trusted (the default): claiming to be someone else changes nothing.
    assert (await attempts(11, "1.1.1.1"))[-1] == 429
    assert (await attempts(1, "2.2.2.2")) == [429]

    settings.trust_forwarded_for = True
    try:
        # Behind our own proxy, each real client gets its own bucket.
        assert (await attempts(1, "3.3.3.3")) == [401]
        assert (await attempts(1, "4.4.4.4, 10.0.0.1")) == [401]
    finally:
        settings.trust_forwarded_for = False


async def test_without_redis_requests_are_let_through(
    app: FastAPI, client: AsyncClient, limits_on: None
) -> None:
    class DownRedis:
        async def eval(self, *args: object) -> None:
            raise RedisConnectionError("down")

    real = app.state.redis
    app.state.redis = DownRedis()
    try:
        body = {"email": "nobody@example.com", "password": "wrong-password"}
        statuses = [(await client.post("/auth/login", json=body)).status_code for _ in range(15)]
    finally:
        app.state.redis = real
    assert statuses == [401] * 15
