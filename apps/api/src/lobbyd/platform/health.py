"""Liveness and readiness endpoints."""

import asyncio

import structlog
from fastapi import APIRouter, Response
from sqlalchemy import text

from lobbyd.platform.cache import RedisDep
from lobbyd.platform.db import SessionDep

router = APIRouter(tags=["health"])
log = structlog.get_logger()

_CHECK_TIMEOUT_SECONDS = 2.0


@router.get("/healthz", summary="Liveness: the process is up")
async def healthz() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/readyz", summary="Readiness: dependencies are reachable")
async def readyz(session: SessionDep, redis: RedisDep, response: Response) -> dict[str, object]:
    async def check_database() -> None:
        await session.execute(text("SELECT 1"))

    async def check_redis() -> None:
        await redis.ping()

    checks: dict[str, str] = {}
    for name, check in (("database", check_database), ("redis", check_redis)):
        try:
            await asyncio.wait_for(check(), timeout=_CHECK_TIMEOUT_SECONDS)
            checks[name] = "ok"
        except Exception:
            log.warning("readiness_check_failed", dependency=name, exc_info=True)
            checks[name] = "unavailable"

    ready = all(value == "ok" for value in checks.values())
    if not ready:
        response.status_code = 503
    return {"status": "ok" if ready else "unavailable", "checks": checks}
