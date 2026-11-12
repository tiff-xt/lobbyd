"""The whole backend in one process: `uvicorn lobbyd_demo.main:app`.

The real deployment is three processes (API, worker, payment simulator), as
in docker-compose.yml. A free hosting plan gives one small web service that
sleeps when idle and no background workers, so the public demo folds all
three into one:

- the API serves HTTP as usual
- the worker's loops run as tasks on the same event loop
- the payment simulator is mounted in memory: the API calls it, and it calls
  the API's webhook, through ASGI instead of over the network

On start it applies migrations, and reseeds when the data is missing or more
than a day old, so the demo always opens on upcoming tournaments however
visitors left it.

Nothing here is new behaviour. It is the same code as the three-process
deployment, wired together the way the test suite already does.
"""

import asyncio
import contextlib
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import httpx
import structlog
from alembic import command
from alembic.config import Config
from fastapi import FastAPI
from httpx import ASGITransport
from sqlalchemy import text

from lobbyd import seed, worker
from lobbyd.config import Settings, get_settings
from lobbyd.main import create_app
from lobbyd.payments.provider import PaysimProvider
from lobbyd.platform.events import EventContext
from paysim.main import create_app as create_paysim
from paysim.simulator import Settings as PaysimSettings

log = structlog.get_logger()

RESEED_AFTER_HOURS = 24
ALEMBIC_INI = Path(os.environ.get("LOBBYD_ALEMBIC_INI", "alembic.ini"))


def _migrate() -> None:
    command.upgrade(Config(str(ALEMBIC_INI)), "head")


async def _needs_seed(app: FastAPI) -> bool:
    async with app.state.sessionmaker() as session:
        age_hours = await session.scalar(
            text("SELECT extract(epoch FROM now() - min(created_at)) / 3600 FROM users")
        )
    return age_hours is None or age_hours > RESEED_AFTER_HOURS


class _Late:
    """An ASGI app whose target is filled in later (the API and the simulator call each other)."""

    target: Any

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        await self.target(scope, receive, send)


def create_demo(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    api_ref = _Late()
    simulator = create_paysim(
        PaysimSettings(
            webhook_url="http://api/webhooks/payments",
            webhook_secret=settings.paysim_webhook_secret,
        ),
        httpx.AsyncClient(transport=ASGITransport(app=api_ref), timeout=30),
    )
    provider = PaysimProvider(
        httpx.AsyncClient(transport=ASGITransport(app=simulator), base_url="http://paysim")
    )
    api = create_app(settings, provider=provider)
    api_ref.target = api
    api_lifespan = api.router.lifespan_context

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        # Alembic runs its own event loop, so it gets its own thread.
        await asyncio.to_thread(_migrate)
        async with simulator.router.lifespan_context(simulator), api_lifespan(app):
            if await _needs_seed(app):
                log.info("demo_reseeding")
                await app.state.redis.flushdb()
                await seed.seed(tournaments=60, random_seed=1, reset=True, hosted_demo=True)
            ctx = EventContext(
                sessionmaker=app.state.sessionmaker,
                redis=app.state.redis,
                settings=settings,
                provider=provider,
            )
            loops = worker.start_loops(ctx)
            log.info("demo_started", consumer=ctx.consumer)
            try:
                yield
            finally:
                for task in loops:
                    task.cancel()
                for task in loops:
                    with contextlib.suppress(asyncio.CancelledError):
                        await task
                await provider.aclose()

    api.router.lifespan_context = lifespan
    return api


def app() -> FastAPI:
    """Factory for uvicorn: `uvicorn --factory lobbyd_demo.main:app`."""
    return create_demo()
