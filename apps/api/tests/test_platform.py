import time
import uuid

from alembic import command
from alembic.config import Config
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from lobbyd.platform.ids import uuid7


async def test_healthz(client: AsyncClient) -> None:
    response = await client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


async def test_readyz_reports_each_dependency(client: AsyncClient) -> None:
    response = await client.get("/readyz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "checks": {"database": "ok", "redis": "ok"}}


async def test_readyz_is_503_when_redis_is_down(app: FastAPI, client: AsyncClient) -> None:
    class DownRedis:
        async def ping(self) -> None:
            raise ConnectionError("down")

    real = app.state.redis
    app.state.redis = DownRedis()
    try:
        response = await client.get("/readyz")
    finally:
        app.state.redis = real
    assert response.status_code == 503
    assert response.json()["checks"] == {"database": "ok", "redis": "unavailable"}


async def test_every_response_carries_a_request_id(client: AsyncClient) -> None:
    generated = (await client.get("/healthz")).headers["x-request-id"]
    assert len(generated) == 32

    echoed = await client.get("/healthz", headers={"X-Request-ID": "trace-abc-12345"})
    assert echoed.headers["x-request-id"] == "trace-abc-12345"

    # Anything that is not short and plain is replaced, not echoed back.
    unsafe = await client.get("/healthz", headers={"X-Request-ID": "x" * 500})
    assert unsafe.headers["x-request-id"] != "x" * 500


async def test_unknown_route_is_a_problem_document(client: AsyncClient) -> None:
    response = await client.get("/nope")
    assert response.status_code == 404
    assert response.headers["content-type"] == "application/problem+json"
    body = response.json()
    assert body["status"] == 404
    assert body["code"] == "not_found"
    assert body["instance"] == "/nope"


async def test_unhandled_errors_do_not_leak_details(app: FastAPI) -> None:
    @app.get("/_boom")
    async def boom() -> None:
        raise RuntimeError("secret internal detail")

    try:
        transport = ASGITransport(app=app, raise_app_exceptions=False)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get("/_boom")
    finally:
        app.router.routes.pop()
    assert response.status_code == 500
    assert response.json()["code"] == "internal_error"
    assert "secret" not in response.text


def test_uuid7_is_time_ordered_and_well_formed() -> None:
    before = time.time_ns() // 1_000_000
    first = uuid7()
    time.sleep(0.002)
    second = uuid7()
    after = time.time_ns() // 1_000_000

    assert first.version == 7
    assert first.variant == uuid.RFC_4122
    assert first < second
    assert before <= (first.int >> 80) <= after
    assert len({uuid7() for _ in range(10_000)}) == 10_000


def test_models_match_migrations(alembic_config: Config) -> None:
    """Fails if a model changed without a migration (or the reverse)."""
    command.check(alembic_config)
