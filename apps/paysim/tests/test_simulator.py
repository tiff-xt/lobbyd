"""The simulator must misbehave exactly as asked, or the tests that rely on it prove nothing."""

import hashlib
import hmac
import json
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from pydantic import SecretStr

from paysim.main import create_app
from paysim.signing import HEADER
from paysim.simulator import Settings, Simulator

SECRET = "test-webhook-secret"


class Merchant:
    """Stands in for the application receiving webhooks."""

    def __init__(self) -> None:
        self.received: list[httpx.Request] = []
        self.fail_next = 0

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if self.fail_next > 0:
            self.fail_next -= 1
            return httpx.Response(500)
        self.received.append(request)
        return httpx.Response(200)

    @property
    def events(self) -> list[dict[str, Any]]:
        return [json.loads(request.content) for request in self.received]

    @property
    def types(self) -> list[str]:
        return [event["type"] for event in self.events]


@pytest.fixture
def merchant() -> Merchant:
    return Merchant()


@pytest.fixture
async def app(merchant: Merchant) -> AsyncIterator[FastAPI]:
    settings = Settings(
        webhook_url="http://merchant/hooks",
        webhook_secret=SecretStr(SECRET),
        delay_ms=10,
        slow_delay_ms=500,
    )
    app = create_app(
        settings,
        httpx.AsyncClient(transport=httpx.MockTransport(merchant)),
        background_delivery=False,
    )
    async with app.router.lifespan_context(app):
        yield app


@pytest.fixture
async def client(app: FastAPI) -> AsyncIterator[AsyncClient]:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://paysim") as client:
        yield client


@pytest.fixture
def simulator(app: FastAPI) -> Simulator:
    sim: Simulator = app.state.simulator
    return sim


async def pay(client: AsyncClient, method: str, key: str = "key-1") -> dict[str, Any]:
    response = await client.post(
        "/payments",
        json={"reference": "order-1", "amount_cents": 4200, "currency": "EUR", "method": method},
        headers={"Idempotency-Key": key},
    )
    assert response.status_code == 201, response.text
    body: dict[str, Any] = response.json()
    return body


async def status(client: AsyncClient, payment_id: str) -> str:
    return str((await client.get(f"/payments/{payment_id}")).json()["status"])


async def test_success(client: AsyncClient, simulator: Simulator, merchant: Merchant) -> None:
    payment = await pay(client, "pm_success")
    assert payment["status"] == "created"
    assert merchant.received == []  # nothing is reported before it happens

    await simulator.deliver_due(everything=True)
    assert merchant.types == ["payment.processing", "payment.succeeded"]
    assert await status(client, payment["id"]) == "succeeded"
    data = merchant.events[-1]["data"]
    assert (data["id"], data["reference"], data["amount_cents"], data["currency"]) == (
        payment["id"],
        "order-1",
        4200,
        "EUR",
    )


async def test_declined(client: AsyncClient, simulator: Simulator, merchant: Merchant) -> None:
    payment = await pay(client, "pm_declined")
    await simulator.deliver_due(everything=True)
    assert merchant.types == ["payment.processing", "payment.failed"]
    assert await status(client, payment["id"]) == "failed"


async def test_duplicate_sends_the_same_event_twice(
    client: AsyncClient, simulator: Simulator, merchant: Merchant
) -> None:
    await pay(client, "pm_duplicate")
    await simulator.deliver_due(everything=True)
    assert merchant.types == ["payment.processing", "payment.succeeded", "payment.succeeded"]
    # The same event id both times: a redelivery, not a second event.
    assert merchant.events[1]["id"] == merchant.events[2]["id"]


async def test_reordered_reports_success_before_processing(
    client: AsyncClient, simulator: Simulator, merchant: Merchant
) -> None:
    payment = await pay(client, "pm_reordered")
    await simulator.deliver_due(everything=True)
    assert merchant.types == ["payment.succeeded", "payment.processing"]
    # The provider's own record is not confused by its delivery order.
    assert await status(client, payment["id"]) == "succeeded"


async def test_never_succeeds_silently(
    client: AsyncClient, simulator: Simulator, merchant: Merchant
) -> None:
    payment = await pay(client, "pm_never")
    await simulator.deliver_due(everything=True)
    assert merchant.received == []
    assert await status(client, payment["id"]) == "succeeded"  # only visible by asking


async def test_slow_takes_much_longer(client: AsyncClient, simulator: Simulator) -> None:
    await pay(client, "pm_success", key="fast")
    await pay(client, "pm_slow", key="slow")
    due = sorted(event.due for event in simulator._queue)
    assert due[-1] - due[0] > 0.4  # 500 ms against 10 ms


async def test_unknown_method_is_refused(client: AsyncClient) -> None:
    response = await client.post(
        "/payments",
        json={"reference": "o", "amount_cents": 1, "currency": "EUR", "method": "pm_bitcoin"},
        headers={"Idempotency-Key": "k"},
    )
    assert response.status_code == 400


async def test_creating_with_the_same_key_returns_the_same_payment(
    client: AsyncClient, simulator: Simulator, merchant: Merchant
) -> None:
    first = await pay(client, "pm_success", key="same")
    again = await pay(client, "pm_success", key="same")
    other = await pay(client, "pm_success", key="different")
    assert first["id"] == again["id"] != other["id"]
    await simulator.deliver_due(everything=True)
    assert len(merchant.received) == 4  # two payments' worth, not three


async def test_refund(client: AsyncClient, simulator: Simulator, merchant: Merchant) -> None:
    payment = await pay(client, "pm_success")
    await simulator.deliver_due(everything=True)

    for _ in range(2):  # asking twice with one key refunds once
        response = await client.post(
            f"/payments/{payment['id']}/refunds", headers={"Idempotency-Key": "refund-1"}
        )
        assert response.status_code == 202
    await simulator.deliver_due(everything=True)

    assert merchant.types == ["payment.processing", "payment.succeeded", "refund.succeeded"]
    assert await status(client, payment["id"]) == "refunded"
    missing = await client.post("/payments/pay_nope/refunds", headers={"Idempotency-Key": "x"})
    assert missing.status_code == 404


async def test_partial_refunds(
    client: AsyncClient, simulator: Simulator, merchant: Merchant
) -> None:
    payment = await pay(client, "pm_success")  # 4200
    await simulator.deliver_due(everything=True)
    url = f"/payments/{payment['id']}/refunds"

    for _ in range(2):  # one key, one refund
        first = await client.post(
            url, headers={"Idempotency-Key": "r1"}, json={"amount_cents": 1000}
        )
        assert first.status_code == 202
    second = await client.post(url, headers={"Idempotency-Key": "r2"}, json={"amount_cents": 3200})
    assert second.status_code == 202
    # Nothing is left, even though neither refund has been carried out yet.
    greedy = await client.post(url, headers={"Idempotency-Key": "r3"}, json={"amount_cents": 1})
    assert greedy.status_code == 409
    await simulator.deliver_due(everything=True)

    assert merchant.types[-2:] == ["refund.partial", "refund.partial"]
    body = (await client.get(f"/payments/{payment['id']}")).json()
    # The payment stands; it records what went back.
    assert (body["status"], body["refunds"]) == ("succeeded", {"r1": 1000, "r2": 3200})


async def test_webhooks_are_signed(
    client: AsyncClient, simulator: Simulator, merchant: Merchant
) -> None:
    await pay(client, "pm_success")
    await simulator.deliver_due(everything=True)

    request = merchant.received[0]
    parts = dict(part.split("=", 1) for part in request.headers[HEADER].split(","))
    expected = hmac.new(
        SECRET.encode(), f"{parts['t']}.".encode() + request.content, hashlib.sha256
    ).hexdigest()
    assert parts["v1"] == expected


async def test_a_failed_delivery_is_retried_with_the_same_event(
    client: AsyncClient, simulator: Simulator, merchant: Merchant
) -> None:
    merchant.fail_next = 2  # the merchant is down for the first two deliveries
    await pay(client, "pm_never", key="quiet")  # a payment with nothing to deliver
    await pay(client, "pm_success")
    await simulator.deliver_due(everything=True)

    # Both events arrived in the end, each exactly once.
    assert sorted(merchant.types) == ["payment.processing", "payment.succeeded"]
    assert len({event["id"] for event in merchant.events}) == 2


async def test_delivery_is_abandoned_after_enough_failures(
    client: AsyncClient, simulator: Simulator, merchant: Merchant
) -> None:
    merchant.fail_next = 10**6
    await pay(client, "pm_success")
    assert await simulator.deliver_due(everything=True) == 12  # 2 events x 6 attempts
    assert merchant.received == []
    assert simulator._queue == []


async def test_concurrent_delivery_does_not_wait_for_a_slow_merchant(app: FastAPI) -> None:
    """Forty webhooks to a merchant that takes 50 ms each: together, not in a line."""
    import asyncio
    import time

    async def slow(request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(0.05)
        return httpx.Response(200)

    simulator: Simulator = app.state.simulator
    simulator.http = httpx.AsyncClient(transport=httpx.MockTransport(slow))
    for n in range(20):
        simulator.create_payment(
            key=f"k{n}", reference=f"o{n}", amount_cents=100, currency="EUR", method="pm_success"
        )
    started = time.perf_counter()
    assert await simulator.deliver_due(everything=True, concurrency=32) == 40
    assert time.perf_counter() - started < 0.5  # one at a time would take two seconds
    assert {payment.status for payment in simulator.payments.values()} == {"succeeded"}
