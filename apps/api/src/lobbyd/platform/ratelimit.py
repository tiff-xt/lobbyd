"""Rate limiting with token buckets in Redis. See ADR 0005.

A bucket holds up to `capacity` tokens and refills at `per_second`. Each
request takes one. That allows a short burst (a page that fires several
requests at once) while capping the sustained rate, which a fixed
"N per minute" counter does not: that either blocks bursts or lets a client
spend the whole minute's allowance in its first second.
"""

import math
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from fastapi import Request
from redis.exceptions import RedisError

from lobbyd.platform.errors import ProblemError

# Check and take in one atomic step. Run as a script because two separate
# commands would let concurrent requests both see the last token.
# Returns {allowed (1/0), seconds until a token is available}.
_TAKE = """
local capacity = tonumber(ARGV[1])
local rate = tonumber(ARGV[2])
local now = redis.call('TIME')
now = tonumber(now[1]) + tonumber(now[2]) / 1000000
local state = redis.call('HMGET', KEYS[1], 'tokens', 'at')
local tokens = tonumber(state[1]) or capacity
local at = tonumber(state[2]) or now
tokens = math.min(capacity, tokens + (now - at) * rate)
local allowed = 0
local wait = 0
if tokens >= 1 then
  tokens = tokens - 1
  allowed = 1
else
  wait = (1 - tokens) / rate
end
redis.call('HSET', KEYS[1], 'tokens', tokens, 'at', now)
redis.call('EXPIRE', KEYS[1], math.ceil(capacity / rate) + 1)
return {allowed, tostring(wait)}
"""


@dataclass(frozen=True)
class Limit:
    name: str
    capacity: int
    per_second: float


def client_ip(request: Request) -> str:
    """The caller's address.

    Behind our own proxy (the web client forwards every request) the socket
    address is the proxy's, so the first hop of X-Forwarded-For is used, but
    only when the deployment says that header can be trusted. Believing it
    from anyone would let a client pick its own bucket.
    """
    if request.app.state.settings.trust_forwarded_for:
        forwarded = request.headers.get("x-forwarded-for")
        if forwarded:
            return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


async def take(request: Request, limit: Limit, subject: str) -> None:
    """Take one token from `subject`'s bucket, or raise 429 with Retry-After."""
    settings = request.app.state.settings
    if not settings.rate_limit_enabled:
        return
    try:
        allowed, wait = await request.app.state.redis.eval(
            _TAKE, 1, f"rl:{limit.name}:{subject}", limit.capacity, limit.per_second
        )
    except RedisError:
        # Limiting is protection, not correctness: without Redis, let traffic through.
        return
    if not allowed:
        retry_after = max(1, math.ceil(float(wait)))
        raise ProblemError(
            429,
            "rate_limited",
            "Too many requests. Slow down and try again shortly.",
            headers={"Retry-After": str(retry_after)},
            retry_after=retry_after,
        )


def by_ip(limit: Limit) -> Callable[[Request], Awaitable[None]]:
    """Dependency: one bucket per client address."""

    async def dependency(request: Request) -> None:
        await take(request, limit, client_ip(request))

    return dependency


# Reading is cheap and mostly cached: generous, and bursts are normal.
READS = Limit("read", capacity=120, per_second=30)
# Guessing passwords and creating accounts in bulk are the things to slow down.
AUTH = Limit("auth", capacity=10, per_second=0.2)
# The contended write path. Enough for a person, not for a script.
BOOKING = Limit("booking", capacity=10, per_second=1)
