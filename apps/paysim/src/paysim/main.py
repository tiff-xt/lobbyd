"""HTTP API of the payment simulator.

Deliberately small and unauthenticated: it exists to stand in for a payment
provider on a developer's machine and in tests, never to be deployed alone.
"""

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from typing import Annotated, Any

import httpx
from fastapi import FastAPI, Header, HTTPException, Request
from pydantic import BaseModel, Field

from paysim.simulator import METHODS, Settings, Simulator, UnknownMethodError


class PaymentCreate(BaseModel):
    reference: str = Field(min_length=1, max_length=100, description="The merchant's own id")
    amount_cents: int = Field(gt=0)
    currency: str = Field(pattern=r"^[A-Z]{3}$")
    method: str = Field(description=f"One of: {', '.join(METHODS)}")


def create_app(
    settings: Settings | None = None,
    http: httpx.AsyncClient | None = None,
    *,
    background_delivery: bool = True,
) -> FastAPI:
    """`background_delivery=False` lets tests fire events by hand, in a known order."""
    settings = settings or Settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        client = http or httpx.AsyncClient(
            timeout=10, limits=httpx.Limits(max_connections=64, max_keepalive_connections=32)
        )
        app.state.simulator = Simulator(settings, client)
        task = asyncio.create_task(app.state.simulator.run()) if background_delivery else None
        try:
            yield
        finally:
            if task is not None:
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task
            if http is None:
                await client.aclose()

    app = FastAPI(title="paysim", summary="Payment provider simulator", lifespan=lifespan)
    Key = Annotated[str, Header(alias="Idempotency-Key", min_length=1, max_length=255)]

    def simulator(request: Request) -> Simulator:
        sim: Simulator = request.app.state.simulator
        return sim

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/payments", status_code=201)
    async def create_payment(body: PaymentCreate, request: Request, key: Key) -> dict[str, Any]:
        try:
            payment = simulator(request).create_payment(
                key=key,
                **body.model_dump(),
                # Handed back on the webhook, so the caller's trace of a
                # checkout continues through the seconds the payment takes.
                traceparent=request.headers.get("traceparent"),
            )
        except UnknownMethodError as exc:
            raise HTTPException(400, f"unknown payment method {exc}") from exc
        return payment.public()

    @app.get("/payments/{payment_id}")
    async def get_payment(payment_id: str, request: Request) -> dict[str, Any]:
        payment = simulator(request).payments.get(payment_id)
        if payment is None:
            raise HTTPException(404, "no such payment")
        return payment.public()

    @app.post("/payments/{payment_id}/refunds", status_code=202)
    async def refund(payment_id: str, request: Request, key: Key) -> dict[str, Any]:
        payment = simulator(request).refund(key=key, payment_id=payment_id)
        if payment is None:
            raise HTTPException(404, "no such payment")
        return payment.public()

    return app


app = create_app()
