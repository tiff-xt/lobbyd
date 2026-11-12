"""Redis client. Holds nothing that cannot be rebuilt (see ADR 0005)."""

from typing import Annotated

from fastapi import Depends, Request
from redis.asyncio import Redis

from lobbyd.config import Settings


def create_redis(settings: Settings) -> Redis:
    return Redis.from_url(settings.redis_url, decode_responses=True)


def get_redis(request: Request) -> Redis:
    redis: Redis = request.app.state.redis
    return redis


RedisDep = Annotated[Redis, Depends(get_redis)]
