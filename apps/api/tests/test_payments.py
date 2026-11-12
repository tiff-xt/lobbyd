"""Payments going right, going wrong, and reporting back in inconvenient ways."""

import asyncio
import json
import time
import uuid
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from lobbyd.config import Settings
from lobbyd.orders import payment_flow
from lobbyd.orders import service as orders
from lobbyd.platform import outbox
from lobbyd.platform.events import DEAD_LETTER_STREAM
from lobbyd.worker import sweep
from paysim.signing import HEADER, sign
from tests.helpers import buy, checkout, hold, make_buyers, open_sale, settle, work
from tests.invariants import assert_invariants, assert_settled

Sessions = async_sessionmaker[AsyncSession]


async def scalar(sessionmaker: Sessions, sql: str) -> Any:
    async with sessionmaker() as session:
        return await session.scalar(text(sql))


async def emails(sessionmaker: Sessions) -> list[str]:
    async with sessionmaker() as session:
        return sorted(await session.scalars(text("SELECT kind FROM notifications")))


async def lapse_all(sessionmaker: Sessions) -> None:
    async with sessionmaker.begin() as session:
        await session.execute(
            text(
                "UPDATE tournament_seats SET held_until = now() - interval '1 second' "
                "WHERE status = 'held'"
            )
        )
        await session.execute(
            text(
                "UPDATE holds SET expires_at = now() - interval '1 second' WHERE status = 'active'"
            )
        )


async def available(client: AsyncClient, slug: str) -> dict[str, int]:
    body = (await client.get(f"/tournaments/{slug}/availability")).json()
    return {t["kind"]: t["available"] for t in body["ticket_types"]}


def signed(settings: Settings, event: dict[str, Any], *, at: float | None = None) -> dict[str, Any]:
    body = json.dumps(event).encode()
    secret = settings.paysim_webhook_secret.get_secret_value()
    return {
        "content": body,
        "headers": {HEADER: sign(secret, int(at or time.time()), body)},
    }


def event(
    payment_id: str, kind: str = "payment.succeeded", event_id: str | None = None
) -> dict[str, Any]:
    return {
        "id": event_id or f"evt_{uuid.uuid4().hex}",
        "type": kind,
        "data": {"id": "pay_test", "reference": payment_id, "status": kind.split(".")[1]},
    }


# --- outcomes -------------------------------------------------------------------


async def test_a_successful_payment(
    client: AsyncClient, app: FastAPI, sessionmaker: Sessions, settings: Settings
) -> None:
    sale = await open_sale(client, sessionmaker)
    (buyer,) = await make_buyers(sessionmaker, settings, 1)
    held = (await hold(client, buyer, sale, seats=sale.seat_ids[:2], general=1)).json()

    order = await buy(client, app, buyer, held["id"])
    assert order["status"] == "paid"
    assert order["payment"] == {"status": "succeeded", "method": "pm_success"}
    assert len(order["tickets"]) == 3
    assert await emails(sessionmaker) == ["order_confirmation"]
    await assert_settled(sessionmaker)


async def test_a_declined_payment_puts_everything_back_on_sale(
    client: AsyncClient, app: FastAPI, sessionmaker: Sessions, settings: Settings
) -> None:
    sale = await open_sale(client, sessionmaker)
    (buyer,) = await make_buyers(sessionmaker, settings, 1)
    held = (await hold(client, buyer, sale, seats=sale.seat_ids[:2], general=3, team=True)).json()

    order = await buy(client, app, buyer, held["id"], method="pm_declined")
    assert order["status"] == "cancelled"
    assert order["payment"]["status"] == "failed"
    assert order["tickets"] == []
    # Straight away, not when the hold would have run out.
    assert await available(client, sale.slug) == {"reserved": 40, "general": 20, "competitor": 4}
    assert await emails(sessionmaker) == ["payment_failed"]
    # And the buyer is free to try again.
    again = await hold(client, buyer, sale, seats=sale.seat_ids[:2])
    assert again.status_code == 201
    assert (await buy(client, app, buyer, again.json()["id"]))["status"] == "paid"
    await assert_settled(sessionmaker)


async def test_a_webhook_delivered_twice(
    client: AsyncClient, app: FastAPI, sessionmaker: Sessions, settings: Settings
) -> None:
    sale = await open_sale(client, sessionmaker)
    (buyer,) = await make_buyers(sessionmaker, settings, 1)
    held = (await hold(client, buyer, sale, seats=sale.seat_ids[:2])).json()

    order = await buy(client, app, buyer, held["id"], method="pm_duplicate")
    assert order["status"] == "paid"
    assert len(order["tickets"]) == 2  # not four
    assert await emails(sessionmaker) == ["order_confirmation"]  # not two
    # The simulator really did send it twice: three deliveries, two distinct events.
    assert await scalar(sessionmaker, "SELECT count(*) FROM payment_events") == 2
    await assert_settled(sessionmaker)


async def test_webhooks_delivered_out_of_order(
    client: AsyncClient, app: FastAPI, sessionmaker: Sessions, settings: Settings
) -> None:
    sale = await open_sale(client, sessionmaker)
    (buyer,) = await make_buyers(sessionmaker, settings, 1)
    held = (await hold(client, buyer, sale, seats=sale.seat_ids[:1])).json()

    # "succeeded" arrives first, then the stale "processing".
    order = await buy(client, app, buyer, held["id"], method="pm_reordered")
    assert order["status"] == "paid"
    assert order["payment"]["status"] == "succeeded"  # did not slide back to processing
    types = await scalar(
        sessionmaker, "SELECT string_agg(type, ',' ORDER BY received_at) FROM payment_events"
    )
    assert types == "payment.succeeded,payment.processing"
    await assert_settled(sessionmaker)


async def test_a_webhook_that_never_arrives_is_found_by_polling(
    client: AsyncClient, app: FastAPI, sessionmaker: Sessions, settings: Settings
) -> None:
    sale = await open_sale(client, sessionmaker)
    (buyer,) = await make_buyers(sessionmaker, settings, 1)
    held = (await hold(client, buyer, sale, seats=sale.seat_ids[:1])).json()

    order = await buy(client, app, buyer, held["id"], method="pm_never")
    assert order["status"] == "pending"  # the provider took the money and said nothing
    assert await scalar(sessionmaker, "SELECT count(*) FROM payment_events") == 0

    # Too recent to be worth asking about yet.
    assert await payment_flow.poll_unsettled(sessionmaker, app.state.provider) == 0
    assert (
        await payment_flow.poll_unsettled(sessionmaker, app.state.provider, older_than_seconds=0)
        == 1
    )
    await settle(app)

    order = (await client.get(f"/orders/{order['id']}", headers=buyer)).json()
    assert order["status"] == "paid"
    assert len(order["tickets"]) == 1
    await assert_settled(sessionmaker)


async def test_a_free_order_needs_no_payment(
    client: AsyncClient, app: FastAPI, sessionmaker: Sessions, settings: Settings
) -> None:
    sale = await open_sale(client, sessionmaker, team_price=0)
    (buyer,) = await make_buyers(sessionmaker, settings, 1)
    held = (await hold(client, buyer, sale, team=True)).json()
    assert held["total_cents"] == 0

    placed = await checkout(client, buyer, held["id"])
    assert placed.status_code == 201
    order = placed.json()
    assert order["status"] == "paid"  # at once, no provider involved
    assert order["payment"] is None
    assert len(order["tickets"]) == 1
    await settle(app)
    assert await emails(sessionmaker) == ["order_confirmation"]
    assert await scalar(sessionmaker, "SELECT count(*) FROM payments") == 0
    await assert_settled(sessionmaker)


# --- paying for something that is gone -----------------------------------------


async def test_payment_lands_after_the_seats_went_to_someone_else(
    client: AsyncClient, app: FastAPI, sessionmaker: Sessions, settings: Settings
) -> None:
    sale = await open_sale(client, sessionmaker)
    slow, quick = await make_buyers(sessionmaker, settings, 2)
    seats = sale.seat_ids[:2]

    stale = (await hold(client, slow, sale, seats=seats)).json()
    slow_order = (await checkout(client, slow, stale["id"], method="pm_slow")).json()
    await settle(app, deliver=False)  # the slow payment is under way at the provider
    await lapse_all(sessionmaker)  # ...and takes longer than the hold lasts

    fresh = await hold(client, quick, sale, seats=seats)
    assert fresh.status_code == 201
    quick_order = await buy(client, app, quick, fresh.json()["id"])  # also delivers slow's success

    assert quick_order["status"] == "paid"
    assert sorted(t["seat_id"] for t in quick_order["tickets"]) == sorted(seats)

    slow_order = (await client.get(f"/orders/{slow_order['id']}", headers=slow)).json()
    # The second buyer keeps the seats. The first gets their money back.
    assert slow_order["status"] == "refunded"
    assert slow_order["payment"]["status"] == "refunded"
    assert slow_order["tickets"] == []
    assert await emails(sessionmaker) == ["order_confirmation", "order_expired", "refund_notice"]
    await assert_settled(sessionmaker)


async def test_payment_lands_a_moment_late_but_nobody_took_the_seats(
    client: AsyncClient, app: FastAPI, sessionmaker: Sessions, settings: Settings
) -> None:
    sale = await open_sale(client, sessionmaker)
    (buyer,) = await make_buyers(sessionmaker, settings, 1)
    held = (await hold(client, buyer, sale, seats=sale.seat_ids[:2], general=2)).json()
    placed = (await checkout(client, buyer, held["id"])).json()
    await settle(app, deliver=False)
    await lapse_all(sessionmaker)  # the deadline passes; no sweeper has run

    await settle(app)
    order = (await client.get(f"/orders/{placed['id']}", headers=buyer)).json()
    # Honoured: refusing here would mean refunding a buyer for seats still free.
    assert order["status"] == "paid"
    assert len(order["tickets"]) == 4
    await assert_settled(sessionmaker)


async def test_payment_lands_after_the_order_was_given_up_on(
    client: AsyncClient, app: FastAPI, sessionmaker: Sessions, settings: Settings
) -> None:
    sale = await open_sale(client, sessionmaker)
    (buyer,) = await make_buyers(sessionmaker, settings, 1)
    held = (await hold(client, buyer, sale, seats=sale.seat_ids[:2], general=2)).json()
    placed = (await checkout(client, buyer, held["id"], method="pm_slow")).json()
    await settle(app, deliver=False)
    await lapse_all(sessionmaker)

    # Housekeeping closes the hold, then the order that was waiting on it.
    assert await sweep(sessionmaker) == (1, 2)
    async with sessionmaker.begin() as session:
        assert await orders.expire_stale_orders(session) == 1
    order = (await client.get(f"/orders/{placed['id']}", headers=buyer)).json()
    assert order["status"] == "expired"
    assert await available(client, sale.slug) == {"reserved": 40, "general": 20, "competitor": 4}

    await settle(app)  # now the money arrives
    order = (await client.get(f"/orders/{placed['id']}", headers=buyer)).json()
    assert order["status"] == "refunded"
    assert order["tickets"] == []
    assert await emails(sessionmaker) == ["order_expired", "refund_notice"]
    assert await available(client, sale.slug) == {"reserved": 40, "general": 20, "competitor": 4}
    await assert_settled(sessionmaker)


# --- the webhook endpoint ---------------------------------------------------------


async def pending_payment(
    client: AsyncClient, sessionmaker: Sessions, settings: Settings
) -> tuple[dict[str, str], str, str]:
    """A buyer with a pending order. Returns (buyer, order id, payment id)."""
    sale = await open_sale(client, sessionmaker)
    (buyer,) = await make_buyers(sessionmaker, settings, 1)
    held = (await hold(client, buyer, sale, seats=sale.seat_ids[:2])).json()
    order = (await checkout(client, buyer, held["id"])).json()
    payment_id = await scalar(sessionmaker, "SELECT id::text FROM payments")
    return buyer, order["id"], payment_id


async def test_webhooks_must_be_signed_by_the_provider(
    client: AsyncClient, sessionmaker: Sessions, settings: Settings
) -> None:
    buyer, order_id, payment_id = await pending_payment(client, sessionmaker, settings)
    genuine = signed(settings, event(payment_id))

    async def post(**kwargs: Any) -> tuple[int, str]:
        response = await client.post("/webhooks/payments", **kwargs)
        return response.status_code, response.json().get("code", "")

    forged = {**genuine, "headers": {HEADER: "t=1,v1=" + "0" * 64}}
    tampered = {**genuine, "content": genuine["content"].replace(b"succeeded", b"SUCCEEDED")}
    wrong_secret = {
        "content": genuine["content"],
        "headers": {HEADER: sign("not-the-secret", int(time.time()), genuine["content"])},
    }
    # A genuine delivery, captured and replayed an hour later.
    replayed = signed(settings, event(payment_id), at=time.time() - 3600)

    assert await post(content=genuine["content"]) == (400, "invalid_signature")  # no header
    for attempt in (forged, tampered, wrong_secret, replayed):
        assert await post(**attempt) == (400, "invalid_signature")
    for garbage in ("", "nonsense", "t=abc,v1=xyz", "v1=only"):
        assert await post(content=genuine["content"], headers={HEADER: garbage}) == (
            400,
            "invalid_signature",
        )

    # None of that paid for anything.
    order = (await client.get(f"/orders/{order_id}", headers=buyer)).json()
    assert order["status"] == "pending"
    # The genuine one does.
    assert (await client.post("/webhooks/payments", **genuine)).status_code == 200
    order = (await client.get(f"/orders/{order_id}", headers=buyer)).json()
    assert order["status"] == "paid"


async def test_signed_webhooks_we_cannot_use_are_handled_calmly(
    client: AsyncClient, sessionmaker: Sessions, settings: Settings
) -> None:
    buyer, order_id, payment_id = await pending_payment(client, sessionmaker, settings)

    not_json = b"this is not json"
    secret = settings.paysim_webhook_secret.get_secret_value()
    response = await client.post(
        "/webhooks/payments",
        content=not_json,
        headers={HEADER: sign(secret, int(time.time()), not_json)},
    )
    assert (response.status_code, response.json()["code"]) == (400, "invalid_webhook")
    response = await client.post("/webhooks/payments", **signed(settings, {"id": "evt_1"}))
    assert (response.status_code, response.json()["code"]) == (400, "invalid_webhook")

    # A payment we have never heard of, and an event type we do not act on:
    # acknowledged, so the provider stops resending, and nothing changes.
    for unusable in (event(str(uuid.uuid4())), event(payment_id, "payment.disputed")):
        assert (
            await client.post("/webhooks/payments", **signed(settings, unusable))
        ).status_code == 200
    order = (await client.get(f"/orders/{order_id}", headers=buyer)).json()
    assert order["status"] == "pending"


async def test_a_webhook_can_arrive_before_we_know_the_providers_id(
    client: AsyncClient, app: FastAPI, sessionmaker: Sessions, settings: Settings
) -> None:
    """The worker asks the provider to create the payment, and the provider's
    webhook wins the race against the worker saving the provider's id."""
    buyer, order_id, payment_id = await pending_payment(client, sessionmaker, settings)
    assert await scalar(sessionmaker, "SELECT provider_ref FROM payments") is None

    response = await client.post("/webhooks/payments", **signed(settings, event(payment_id)))
    assert response.status_code == 200
    order = (await client.get(f"/orders/{order_id}", headers=buyer)).json()
    assert order["status"] == "paid"
    assert await scalar(sessionmaker, "SELECT provider_ref FROM payments") == "pay_test"

    await settle(app)  # the worker catches up afterwards, and must not disturb anything
    assert await scalar(sessionmaker, "SELECT count(*) FROM tickets") == 2
    await assert_invariants(sessionmaker)


async def test_twenty_copies_of_one_webhook_at_once(
    client: AsyncClient, sessionmaker: Sessions, settings: Settings
) -> None:
    buyer, order_id, payment_id = await pending_payment(client, sessionmaker, settings)
    same = signed(settings, event(payment_id, event_id="evt_same"))
    different_ids = [signed(settings, event(payment_id)) for _ in range(10)]

    responses = await asyncio.gather(
        *(client.post("/webhooks/payments", **same) for _ in range(10)),
        *(client.post("/webhooks/payments", **other) for other in different_ids),
    )
    assert {r.status_code for r in responses} == {200}
    order = (await client.get(f"/orders/{order_id}", headers=buyer)).json()
    assert order["status"] == "paid"
    assert len(order["tickets"]) == 2
    assert await scalar(sessionmaker, "SELECT count(*) FROM outbox WHERE topic = 'order.paid'") == 1
    await assert_invariants(sessionmaker)


async def test_a_failure_arriving_after_success_changes_nothing(
    client: AsyncClient, sessionmaker: Sessions, settings: Settings
) -> None:
    buyer, order_id, payment_id = await pending_payment(client, sessionmaker, settings)
    for kind in ("payment.succeeded", "payment.failed", "payment.processing"):
        assert (
            await client.post("/webhooks/payments", **signed(settings, event(payment_id, kind)))
        ).status_code == 200
    order = (await client.get(f"/orders/{order_id}", headers=buyer)).json()
    assert order["status"] == "paid"
    assert order["payment"]["status"] == "succeeded"
    assert len(order["tickets"]) == 2
    await assert_invariants(sessionmaker)


# --- the outbox and the workers -----------------------------------------------


async def test_an_event_exists_only_if_its_transaction_commits(
    sessionmaker: Sessions,
) -> None:
    async def change_that_fails() -> None:
        async with sessionmaker.begin() as session:
            await outbox.emit(session, "test.rolled_back", {})
            raise RuntimeError("the business change failed")

    with pytest.raises(RuntimeError):
        await change_that_fails()
    async with sessionmaker.begin() as session:
        await outbox.emit(session, "test.committed", {})
    assert (
        await scalar(sessionmaker, "SELECT string_agg(topic, ',') FROM outbox") == "test.committed"
    )


async def test_events_published_twice_are_handled_once(
    client: AsyncClient, app: FastAPI, sessionmaker: Sessions, settings: Settings
) -> None:
    sale = await open_sale(client, sessionmaker)
    (buyer,) = await make_buyers(sessionmaker, settings, 1)
    held = (await hold(client, buyer, sale, seats=sale.seat_ids[:2])).json()
    order = await buy(client, app, buyer, held["id"])
    assert order["status"] == "paid"

    # The relay crashed after publishing but before recording that it had:
    # on restart it publishes everything again.
    async with sessionmaker.begin() as session:
        republished = await session.execute(text("UPDATE outbox SET published_at = NULL"))
    assert republished.rowcount >= 2  # type: ignore[attr-defined]
    await settle(app)

    assert await emails(sessionmaker) == ["order_confirmation"]
    assert len(app.state.simulator.payments) == 1  # the provider was not asked twice
    assert await scalar(sessionmaker, "SELECT count(*) FROM tickets") == 2
    await assert_settled(sessionmaker)


class FlakyProvider:
    """The real provider, except that creating a payment fails the first few times."""

    def __init__(self, real: Any, failures: int) -> None:
        self.real, self.failures, self.calls = real, failures, 0

    async def create_payment(self, **kwargs: Any) -> Any:
        self.calls += 1
        if self.calls <= self.failures:
            raise ConnectionError("provider unreachable")
        return await self.real.create_payment(**kwargs)

    def __getattr__(self, name: str) -> Any:
        return getattr(self.real, name)


async def test_the_provider_being_down_delays_payment_but_loses_nothing(
    client: AsyncClient, app: FastAPI, sessionmaker: Sessions, settings: Settings
) -> None:
    sale = await open_sale(client, sessionmaker)
    (buyer,) = await make_buyers(sessionmaker, settings, 1)
    held = (await hold(client, buyer, sale, seats=sale.seat_ids[:2])).json()
    flaky = FlakyProvider(app.state.provider, failures=3)
    app.state.events.provider = flaky

    # Checkout itself does not need the provider at all.
    placed = await checkout(client, buyer, held["id"])
    assert placed.status_code == 201

    for _ in range(3):
        assert await work(app) in (0, 1)  # the relay publishes; the handler fails
        order = (await client.get(f"/orders/{placed.json()['id']}", headers=buyer)).json()
        assert order["status"] == "pending"
    await settle(app)  # fourth attempt gets through

    assert flaky.calls == 4
    order = (await client.get(f"/orders/{placed.json()['id']}", headers=buyer)).json()
    assert order["status"] == "paid"
    await assert_settled(sessionmaker)


async def test_an_event_that_keeps_failing_is_set_aside(
    client: AsyncClient, app: FastAPI, sessionmaker: Sessions, settings: Settings
) -> None:
    sale = await open_sale(client, sessionmaker)
    one, two = await make_buyers(sessionmaker, settings, 2)
    doomed = (await hold(client, one, sale, seats=sale.seat_ids[:1])).json()
    app.state.events.provider = FlakyProvider(app.state.provider, failures=10**9)
    placed = await checkout(client, one, doomed["id"])

    for _ in range(settings.event_max_attempts + 2):
        await work(app)
    dead = await app.state.redis.xrange(DEAD_LETTER_STREAM)
    assert len(dead) == 1
    assert dead[0][1]["topic"] == "payment.requested"
    assert dead[0][1]["attempts"] == str(settings.event_max_attempts)

    # It no longer blocks the queue: the next buyer's payment goes through.
    app.state.events.provider = app.state.provider
    fine = (await hold(client, two, sale, seats=sale.seat_ids[1:2])).json()
    assert (await buy(client, app, two, fine["id"]))["status"] == "paid"
    order = (await client.get(f"/orders/{placed.json()['id']}", headers=one)).json()
    assert order["status"] == "pending"  # never charged; its hold will lapse and free the seat


async def test_several_workers_at_once(
    client: AsyncClient, app: FastAPI, sessionmaker: Sessions, settings: Settings
) -> None:
    sale = await open_sale(client, sessionmaker, seats=60)
    buyers = await make_buyers(sessionmaker, settings, 60)
    holds = await asyncio.gather(
        *(hold(client, b, sale, seats=[sale.seat_ids[n]]) for n, b in enumerate(buyers))
    )
    await asyncio.gather(
        *(checkout(client, b, h.json()["id"]) for b, h in zip(buyers, holds, strict=True))
    )

    for _ in range(10):
        # Four relays and four consumers racing over the same rows and stream.
        await asyncio.gather(*(work(app) for _ in range(4)))
        await app.state.simulator.deliver_due(everything=True)
    await settle(app)

    assert await scalar(sessionmaker, "SELECT count(*) FROM orders WHERE status = 'paid'") == 60
    assert len(app.state.simulator.payments) == 60
    assert await emails(sessionmaker) == ["order_confirmation"] * 60
    await assert_settled(sessionmaker)
