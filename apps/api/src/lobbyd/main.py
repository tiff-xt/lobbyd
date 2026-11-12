"""Application factory."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from lobbyd.config import Settings, get_settings
from lobbyd.identity.router import router as identity_router
from lobbyd.platform import health
from lobbyd.platform.cache import create_redis
from lobbyd.platform.db import create_engine, create_sessionmaker
from lobbyd.platform.errors import install_error_handlers
from lobbyd.platform.logging import RequestContextMiddleware, configure_logging


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(json=settings.log_json)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        engine = create_engine(settings)
        app.state.settings = settings
        app.state.engine = engine
        app.state.sessionmaker = create_sessionmaker(engine)
        app.state.redis = create_redis(settings)
        try:
            yield
        finally:
            await app.state.redis.aclose()
            await engine.dispose()

    app = FastAPI(
        title="lobbyd",
        summary="Ticketing and registration for gaming tournaments and LAN events.",
        version="0.1.0",
        lifespan=lifespan,
    )
    app.add_middleware(RequestContextMiddleware)
    install_error_handlers(app)
    app.include_router(health.router)
    app.include_router(identity_router)
    return app


app = create_app()
