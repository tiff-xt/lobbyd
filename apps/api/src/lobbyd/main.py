"""Application factory."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI

from lobbyd.catalog import service as catalog
from lobbyd.catalog.router import router as catalog_router
from lobbyd.config import Settings, get_settings
from lobbyd.identity.deps import configure_user_memo
from lobbyd.identity.router import router as identity_router
from lobbyd.inventory.router import router as inventory_router
from lobbyd.orders.router import router as orders_router
from lobbyd.payments.provider import PaymentProvider, PaysimProvider
from lobbyd.platform import cache, health
from lobbyd.platform.cache import create_redis
from lobbyd.platform.db import create_engine, create_sessionmaker
from lobbyd.platform.errors import (
    PROBLEM_RESPONSES,
    install_error_handlers,
    use_problem_media_type,
)
from lobbyd.platform.logging import RequestContextMiddleware, configure_logging
from lobbyd.wiring import wire_modules


def create_app(
    settings: Settings | None = None, provider: PaymentProvider | None = None
) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(json=settings.log_json)
    wire_modules()
    cache.enabled = settings.cache_enabled
    catalog.configure_memos(settings.catalog_memo_seconds)
    configure_user_memo(settings.auth_memo_seconds)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        engine = create_engine(settings)
        app.state.settings = settings
        app.state.engine = engine
        app.state.sessionmaker = create_sessionmaker(engine)
        app.state.redis = create_redis(settings)
        app.state.provider = provider or PaysimProvider.connect(settings.paysim_url)
        try:
            yield
        finally:
            await app.state.redis.aclose()
            if provider is None:
                await app.state.provider.aclose()
            await engine.dispose()

    app = FastAPI(
        title="lobbyd",
        summary="Ticketing and registration for gaming tournaments and LAN events.",
        version="0.1.0",
        lifespan=lifespan,
        responses=PROBLEM_RESPONSES,
    )
    build_openapi = app.openapi

    def openapi() -> dict[str, Any]:
        return use_problem_media_type(build_openapi())

    app.openapi = openapi  # type: ignore[method-assign]
    app.add_middleware(RequestContextMiddleware)
    install_error_handlers(app)
    app.include_router(health.router)
    app.include_router(identity_router)
    app.include_router(catalog_router)
    app.include_router(inventory_router)
    app.include_router(orders_router)
    return app


app = create_app()
