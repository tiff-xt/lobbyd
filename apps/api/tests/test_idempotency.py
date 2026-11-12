"""A retried request returns the first response and does nothing else."""

import asyncio

from httpx import AsyncClient
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from lobbyd.config import Settings
from lobbyd.inventory.models import Hold
from lobbyd.orders.models import Order
from lobbyd.platform import idempotency
from lobbyd.platform.idempotency import IdempotencyKey
from tests.helpers import checkout, hold, key, make_buyers, open_sale
from tests.invariants import assert_invariants

Sessions = async_sessionmaker[AsyncSession]


async def count(sessionmaker: Sessions, model: type) -> int:
    async with sessionmaker() as session:
        return await session.scalar(select(func.count()).select_from(model)) or 0


async def test_a_retried_hold_returns_the_original_and_holds_nothing_more(
    client: AsyncClient, sessionmaker: Sessions, settings: Settings
) -> None:
    sale = await open_sale(client, sessionmaker)
    (buyer,) = await make_buyers(sessionmaker, settings, 1)
    same = key()

    first = await hold(client, buyer, sale, seats=sale.seat_ids[:2], general=2, idempotency=same)
    retry = await hold(client, buyer, sale, seats=sale.seat_ids[:2], general=2, idempotency=same)

    assert first.status_code == retry.status_code == 201
    assert retry.json() == first.json()
    assert "idempotent-replay" not in first.headers
    assert retry.headers["idempotent-replay"] == "true"
    assert await count(sessionmaker, Hold) == 1
    await assert_invariants(sessionmaker)


async def test_a_retried_checkout_charges_once(
    client: AsyncClient, sessionmaker: Sessions, settings: Settings
) -> None:
    sale = await open_sale(client, sessionmaker)
    (buyer,) = await make_buyers(sessionmaker, settings, 1)
    held = (await hold(client, buyer, sale, seats=sale.seat_ids[:2])).json()
    same = key()

    first = await checkout(client, buyer, held["id"], same)
    retry = await checkout(client, buyer, held["id"], same)
    assert first.status_code == retry.status_code == 201
    assert retry.json() == first.json()
    assert await count(sessionmaker, Order) == 1

    # A second click with a NEW key is a new request. It is refused, not charged.
    again = await checkout(client, buyer, held["id"])
    assert (again.status_code, again.json()["code"]) == (409, "hold_already_used")
    assert await count(sessionmaker, Order) == 1
    await assert_invariants(sessionmaker)


async def test_ten_copies_of_one_request_at_once(
    client: AsyncClient, sessionmaker: Sessions, settings: Settings
) -> None:
    sale = await open_sale(client, sessionmaker)
    (buyer,) = await make_buyers(sessionmaker, settings, 1)
    same = key()

    holds = await asyncio.gather(
        *(hold(client, buyer, sale, seats=sale.seat_ids[:2], idempotency=same) for _ in range(10))
    )
    assert {r.status_code for r in holds} == {201}
    assert len({r.text for r in holds}) == 1  # byte for byte the same answer
    assert sum("idempotent-replay" in r.headers for r in holds) == 9
    assert await count(sessionmaker, Hold) == 1

    same = key()
    orders = await asyncio.gather(
        *(checkout(client, buyer, holds[0].json()["id"], same) for _ in range(10))
    )
    assert {r.status_code for r in orders} == {201}
    assert len({r.text for r in orders}) == 1
    assert await count(sessionmaker, Order) == 1
    await assert_invariants(sessionmaker)


async def test_a_refusal_is_a_final_answer_for_that_key(
    client: AsyncClient, sessionmaker: Sessions, settings: Settings
) -> None:
    sale = await open_sale(client, sessionmaker)
    first, second = await make_buyers(sessionmaker, settings, 2)
    blocker = (await hold(client, first, sale, seats=sale.seat_ids[:1])).json()
    same = key()

    refused = await hold(client, second, sale, seats=sale.seat_ids[:1], idempotency=same)
    assert refused.status_code == 409
    assert refused.headers["content-type"] == "application/problem+json"

    # The seat frees up, but a retry with the old key still gets the old answer:
    # a retry asks "what happened?", never "try again".
    await client.delete(f"/holds/{blocker['id']}", headers=first)
    retry = await hold(client, second, sale, seats=sale.seat_ids[:1], idempotency=same)
    assert retry.status_code == 409
    assert retry.json() == refused.json()
    assert retry.headers["idempotent-replay"] == "true"

    # A new attempt, with a new key, succeeds.
    assert (await hold(client, second, sale, seats=sale.seat_ids[:1])).status_code == 201
    await assert_invariants(sessionmaker)


async def test_a_key_cannot_be_reused_for_a_different_request(
    client: AsyncClient, sessionmaker: Sessions, settings: Settings
) -> None:
    sale = await open_sale(client, sessionmaker)
    (buyer,) = await make_buyers(sessionmaker, settings, 1)
    same = key()
    await hold(client, buyer, sale, seats=sale.seat_ids[:1], idempotency=same)

    different = await hold(client, buyer, sale, seats=sale.seat_ids[1:2], idempotency=same)
    assert (different.status_code, different.json()["code"]) == (422, "idempotency_key_reused")
    assert await count(sessionmaker, Hold) == 1


async def test_keys_belong_to_the_user_who_sent_them(
    client: AsyncClient, sessionmaker: Sessions, settings: Settings
) -> None:
    sale = await open_sale(client, sessionmaker)
    one, two = await make_buyers(sessionmaker, settings, 2)
    same = key()
    mine = await hold(client, one, sale, seats=sale.seat_ids[:1], idempotency=same)
    # Someone else sending the same key must not be handed my response.
    theirs = await hold(client, two, sale, seats=sale.seat_ids[1:2], idempotency=same)
    assert mine.status_code == theirs.status_code == 201
    assert mine.json()["id"] != theirs.json()["id"]


async def test_the_key_is_required_and_checked(
    client: AsyncClient, sessionmaker: Sessions, settings: Settings
) -> None:
    sale = await open_sale(client, sessionmaker)
    (buyer,) = await make_buyers(sessionmaker, settings, 1)
    body = {"tournament": sale.slug, "seat_ids": sale.seat_ids[:1]}

    missing = await client.post("/holds", json=body, headers=buyer)
    assert (missing.status_code, missing.json()["code"]) == (400, "idempotency_key_required")
    for bad in ("short", "has a space in it", "x" * 256):
        response = await client.post("/holds", json=body, headers={**buyer, "Idempotency-Key": bad})
        assert (response.status_code, response.json()["code"]) == (400, "idempotency_key_invalid")
    assert await count(sessionmaker, Hold) == 0


async def test_old_keys_are_pruned(
    client: AsyncClient, sessionmaker: Sessions, settings: Settings
) -> None:
    sale = await open_sale(client, sessionmaker)
    (buyer,) = await make_buyers(sessionmaker, settings, 1)
    old = key()
    await hold(client, buyer, sale, seats=sale.seat_ids[:1], idempotency=old)
    async with sessionmaker.begin() as session:
        await session.execute(
            text("UPDATE idempotency_keys SET created_at = now() - interval '25 hours'")
        )
        await session.execute(text("UPDATE holds SET status = 'released'"))
        await session.execute(
            text("UPDATE tournament_seats SET status='available', hold_id=NULL, held_until=NULL")
        )
    await hold(client, buyer, sale, seats=sale.seat_ids[1:2])  # a recent key

    async with sessionmaker.begin() as session:
        assert await idempotency.prune(session) == 1
    assert await count(sessionmaker, IdempotencyKey) == 1
