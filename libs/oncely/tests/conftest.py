"""Fixtures. Tests run against a real Postgres and Redis, never mocks.

Everything lives in its own Postgres schema and Redis database, so the suite
can share a server with other projects.
"""

import os
from collections.abc import AsyncIterator

import pytest
from redis.asyncio import Redis
from sqlalchemy import Column, Integer, MetaData, Table, Text, text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from oncely import IdempotencyKeys, Outbox

DATABASE_URL = os.environ.get(
    "ONCELY_TEST_DATABASE_URL", "postgresql+asyncpg://lobbyd:lobbyd@localhost:5432/lobbyd_test"
)
REDIS_URL = os.environ.get("ONCELY_TEST_REDIS_URL", "redis://localhost:6379/14")
SCHEMA = "oncely_test"

metadata = MetaData(schema=SCHEMA)
keys = IdempotencyKeys(metadata)
outbox = Outbox(metadata)
# Stands in for the application's own data: the thing the work changes.
things = Table("things", metadata, Column("id", Integer, primary_key=True), Column("name", Text))


@pytest.fixture(scope="session")
async def engine() -> AsyncIterator[AsyncEngine]:
    engine = create_async_engine(DATABASE_URL)
    async with engine.begin() as connection:
        await connection.execute(text(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE"))
        await connection.execute(text(f"CREATE SCHEMA {SCHEMA}"))
        await connection.run_sync(metadata.create_all)
    yield engine
    async with engine.begin() as connection:
        await connection.execute(text(f"DROP SCHEMA {SCHEMA} CASCADE"))
    await engine.dispose()


@pytest.fixture
async def sessionmaker(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    async with engine.begin() as connection:
        for table in metadata.sorted_tables:
            await connection.execute(table.delete())
    return async_sessionmaker(engine, expire_on_commit=False)


@pytest.fixture
async def redis() -> AsyncIterator[Redis]:
    client = Redis.from_url(REDIS_URL, decode_responses=True)
    await client.flushdb()
    yield client
    await client.aclose()
