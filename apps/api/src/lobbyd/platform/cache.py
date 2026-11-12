"""Redis client and a read-through cache. Redis holds nothing that cannot be
rebuilt (see ADR 0005)."""

import asyncio
import time
from collections.abc import Awaitable, Callable
from typing import Annotated

from fastapi import Depends, Request
from redis.asyncio import BlockingConnectionPool, Redis
from redis.exceptions import RedisError

from lobbyd.config import Settings


def create_redis(settings: Settings) -> Redis:
    # A blocking pool: when every connection is busy, a request waits for one
    # instead of failing. The default pool raises as soon as it is exhausted,
    # which turns a traffic spike into a wave of errors.
    pool = BlockingConnectionPool.from_url(
        settings.redis_url,
        decode_responses=True,
        max_connections=settings.redis_max_connections,
        timeout=5,
    )
    return Redis(connection_pool=pool)


def get_redis(request: Request) -> Redis:
    redis: Redis = request.app.state.redis
    return redis


RedisDep = Annotated[Redis, Depends(get_redis)]


from lobbyd.platform import metrics  # noqa: E402

# --- read-through cache ----------------------------------------------------------

# Flipped by settings at startup. Off only when measuring what the cache is worth.
enabled = True

_LOCK_MS = 5000
_WAIT_STEP = 0.02
_WAIT_STEPS = 100  # up to two seconds waiting for someone else's rebuild


class _Stats:
    """Counts since the process started. Exposed as metrics later; used by tests now."""

    def __init__(self) -> None:
        self.hits = self.stale = self.misses = self.rebuilds = self.errors = 0


stats = _Stats()


async def cached(
    redis: Redis,
    key: str,
    load: Callable[[], Awaitable[str]],
    *,
    fresh_seconds: float,
    stale_seconds: float = 0,
    fresh_for: Callable[[str], float] | None = None,
) -> str:
    """Return the JSON text for `key`, building it with `load` when needed.

    `fresh_for`, if given, chooses the fresh time from the value just built
    (it overrides `fresh_seconds`). Use it when some values are settled and
    safe to keep, and others are about to change.

    Two problems are handled, both of which show up exactly when traffic peaks.

    **Stampede.** When a popular entry expires, every request that arrives
    before it is rebuilt would otherwise run the same expensive query at
    once. Here one request takes a short lock and rebuilds; the others wait
    for its result.

    **Latency while rebuilding.** With `stale_seconds`, an entry past its
    fresh time is still served for that long while a single request refreshes
    it. Nobody waits, and the data is at most fresh + stale seconds old.

    If Redis is unavailable the value is simply loaded: slower, never wrong.
    """
    if not enabled:
        return await load()
    try:
        return await _cached(
            redis, key, load, fresh_for or (lambda _: fresh_seconds), stale_seconds
        )
    except RedisError:
        # Redis failed at some point along the way. Slower, never wrong.
        stats.errors += 1
        metrics.CACHE_LOOKUPS.labels("error").inc()
        return await load()


async def _cached(
    redis: Redis,
    key: str,
    load: Callable[[], Awaitable[str]],
    fresh_seconds: Callable[[str], float],
    stale_seconds: float,
) -> str:
    raw = await _get(redis, key)
    if raw is not None:
        fresh_until, _, value = raw.partition("|")
        if time.time() < float(fresh_until):
            stats.hits += 1
            metrics.CACHE_LOOKUPS.labels("hit").inc()
            return value
        # Past its fresh time. Whoever gets the lock refreshes; everyone
        # else is served the old value immediately.
        if not await _try_lock(redis, key):
            stats.stale += 1
            metrics.CACHE_LOOKUPS.labels("stale").inc()
            return value
        return await _rebuild(redis, key, load, fresh_seconds, stale_seconds)

    stats.misses += 1

    metrics.CACHE_LOOKUPS.labels("miss").inc()
    if await _try_lock(redis, key):
        return await _rebuild(redis, key, load, fresh_seconds, stale_seconds)
    # Someone else is building it. Wait for their result instead of
    # repeating their work.
    for _ in range(_WAIT_STEPS):
        await asyncio.sleep(_WAIT_STEP)
        raw = await _get(redis, key)
        if raw is not None:
            return raw.partition("|")[2]
    return await load()  # the builder died or is very slow: do it ourselves


async def _get(redis: Redis, key: str) -> str | None:
    # The client is created with decode_responses=True, so values are text.
    value = await redis.get(key)
    return value if isinstance(value, str) or value is None else value.decode()


async def _try_lock(redis: Redis, key: str) -> bool:
    return bool(await redis.set(f"lock:{key}", "1", nx=True, px=_LOCK_MS))


async def _rebuild(
    redis: Redis,
    key: str,
    load: Callable[[], Awaitable[str]],
    fresh_seconds: Callable[[str], float],
    stale_seconds: float,
) -> str:
    stats.rebuilds += 1
    try:
        value = await load()
        fresh = fresh_seconds(value)
        ttl_ms = int((fresh + stale_seconds) * 1000)
        await redis.set(key, f"{time.time() + fresh}|{value}", px=max(ttl_ms, 1))
        return value
    finally:
        await redis.delete(f"lock:{key}")


async def invalidate(redis: Redis, *keys: str) -> None:
    """Drop entries after the data behind them changed. Best effort: they expire anyway."""
    try:
        if keys:
            await redis.delete(*keys)
    except RedisError:
        stats.errors += 1
        metrics.CACHE_LOOKUPS.labels("error").inc()
