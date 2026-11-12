"""Test fixtures. Tests run against a real Postgres and Redis, never mocks.

The schema is built by running the migrations, so they are tested too.
"""

import asyncio
import os
from collections.abc import AsyncIterator

# Must be set before lobbyd.config is imported anywhere.
os.environ["LOBBYD_ENVIRONMENT"] = "test"
os.environ.setdefault(
    "LOBBYD_DATABASE_URL", "postgresql+asyncpg://lobbyd:lobbyd@localhost:5432/lobbyd_test"
)
os.environ.setdefault("LOBBYD_REDIS_URL", "redis://localhost:6379/15")

import pytest
from alembic import command
from alembic.config import Config
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from lobbyd.config import Settings, get_settings
from lobbyd.main import create_app
from lobbyd.platform.db import Base

API_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


@pytest.fixture(scope="session")
def settings() -> Settings:
    settings = get_settings()
    # Guard against pointing the suite at a real database: it drops everything.
    assert settings.database_url.rsplit("/", 1)[-1].startswith("lobbyd_test")
    return settings


@pytest.fixture(scope="session")
def alembic_config(settings: Settings) -> Config:
    return Config(os.path.join(API_ROOT, "alembic.ini"))


@pytest.fixture(scope="session", autouse=True)
def _schema(alembic_config: Config) -> None:
    # Synchronous on purpose: Alembic starts its own event loop.
    command.downgrade(alembic_config, "base")
    command.upgrade(alembic_config, "head")


@pytest.fixture(scope="session")
async def app(settings: Settings, _schema: None) -> AsyncIterator[FastAPI]:
    app = create_app(settings)
    async with app.router.lifespan_context(app):
        await _warm_pool(app, settings)
        yield app


async def _warm_pool(app: FastAPI, settings: Settings) -> None:
    """Open every pooled connection before any test runs.

    Opening a connection takes far longer than a query. With a cold pool, the
    first request of a "simultaneous" burst finishes before the others have
    even connected, and a concurrency test quietly runs one buyer at a time.
    """

    async def hold_one() -> None:
        async with app.state.engine.connect() as connection:
            await connection.execute(text("SELECT 1"))
            await asyncio.sleep(0.2)  # keep it checked out so the next task opens another

    await asyncio.gather(
        *(hold_one() for _ in range(settings.db_pool_size + settings.db_max_overflow))
    )


@pytest.fixture
async def client(app: FastAPI) -> AsyncIterator[AsyncClient]:
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client


@pytest.fixture
def sessionmaker(app: FastAPI) -> async_sessionmaker[AsyncSession]:
    maker: async_sessionmaker[AsyncSession] = app.state.sessionmaker
    return maker


@pytest.fixture(autouse=True)
async def _clean(app: FastAPI) -> AsyncIterator[None]:
    yield
    tables = ", ".join(f'"{table.name}"' for table in Base.metadata.sorted_tables)
    async with app.state.engine.begin() as connection:
        await connection.execute(text(f"TRUNCATE {tables} CASCADE"))
    await app.state.redis.flushdb()
