"""The waiting room: fair, steady, and impossible to skip."""

import asyncio
import time
from datetime import UTC, datetime, timedelta
from typing import Any

import jwt
from fastapi import FastAPI
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from lobbyd.config import Settings
from lobbyd.waitingroom import service as waitingroom
from tests.helpers import Sale, hold, key, make_buyers, open_sale
from tests.invariants import assert_invariants

Sessions = async_sessionmaker[AsyncSession]
Buyer = dict[str, str]


async def guarded_sale(
    client: AsyncClient,
    sessionmaker: Sessions,
    *,
    opens_in: timedelta | None = None,
    **kwargs: Any,
) -> Sale:
    """A sale with the waiting room on. `opens_in` moves the opening time."""
    sale = await open_sale(client, sessionmaker, **kwargs)
    async with sessionmaker.begin() as session:
        await session.execute(text("UPDATE tournaments SET waiting_room = true"))
        if opens_in is not None:
            await session.execute(
                text("UPDATE tournaments SET sales_open_at = now() + :delta"), {"delta": opens_in}
            )
    return sale


async def queue(client: AsyncClient, buyer: Buyer, sale: Sale) -> dict[str, Any]:
    response = await client.post(f"/tournaments/{sale.slug}/queue", headers=buyer)
    assert response.status_code == 200, response.text
    body: dict[str, Any] = response.json()
    return body


async def let_in(app: FastAPI, settings: Settings, count: int) -> int:
    """Run the admitter as if enough time had passed to admit `count` people."""
    await waitingroom.admit(app.state.redis, settings, now=time.time())  # starts the clock
    seconds = count / settings.waiting_room_admit_per_second
    return await waitingroom.admit(app.state.redis, settings, now=time.time() + seconds)


async def test_sales_without_a_waiting_room_are_unaffected(
    client: AsyncClient, sessionmaker: Sessions, settings: Settings
) -> None:
    sale = await open_sale(client, sessionmaker)
    (buyer,) = await make_buyers(sessionmaker, settings, 1)
    assert (await queue(client, buyer, sale))["status"] == "not_required"
    assert (await hold(client, buyer, sale, seats=sale.seat_ids[:1])).status_code == 201


async def test_you_cannot_book_without_a_turn(
    client: AsyncClient, app: FastAPI, sessionmaker: Sessions, settings: Settings
) -> None:
    sale = await guarded_sale(client, sessionmaker)
    first, second = await make_buyers(sessionmaker, settings, 2)

    refused = await hold(client, first, sale, seats=sale.seat_ids[:1])
    assert (refused.status_code, refused.json()["code"]) == (403, "admission_required")

    status = await queue(client, first, sale)
    assert status["status"] == "queued"
    assert status["position"] == 1
    assert (await queue(client, second, sale))["position"] == 2
    assert (await queue(client, first, sale))["position"] == 1  # polling does not move you

    assert await let_in(app, settings, 1) == 1
    admitted = await queue(client, first, sale)
    assert admitted["status"] == "admitted"
    assert (await queue(client, second, sale))["position"] == 1  # the line moved up

    body = {"tournament": sale.slug, "seat_ids": sale.seat_ids[:1]}
    token = {"X-Admission-Token": str(admitted["admission_token"])}
    held = await client.post("/holds", json=body, headers={**first, **key(), **token})
    assert held.status_code == 201, held.text
    await assert_invariants(sessionmaker)


async def test_a_turn_is_for_one_person_and_one_sale(
    client: AsyncClient, app: FastAPI, sessionmaker: Sessions, settings: Settings
) -> None:
    sale = await guarded_sale(client, sessionmaker)
    other = await open_sale(
        client, sessionmaker, organizer_email="second@example.com", title="Other Cup"
    )
    async with sessionmaker.begin() as session:
        await session.execute(text("UPDATE tournaments SET waiting_room = true"))
    holder, friend = await make_buyers(sessionmaker, settings, 2)
    await queue(client, holder, sale)
    await let_in(app, settings, 1)
    token = str((await queue(client, holder, sale))["admission_token"])

    async def attempt(buyer: Buyer, where: Sale, admission: str) -> tuple[int, str]:
        response = await client.post(
            "/holds",
            json={"tournament": where.slug, "seat_ids": where.seat_ids[:1]},
            headers={**buyer, **key(), "X-Admission-Token": admission},
        )
        return response.status_code, response.json().get("code", "")

    secret = settings.jwt_secret.get_secret_value()
    claims = jwt.decode(token, secret, algorithms=["HS256"], audience="lobbyd-admission")
    expired = jwt.encode({**claims, "exp": datetime.now(UTC) - timedelta(seconds=1)}, secret)
    forged = jwt.encode(claims, "x" * 40)
    access_token = holder["Authorization"].removeprefix("Bearer ")

    refused = (403, "admission_required")
    assert await attempt(friend, sale, token) == refused  # passed to a friend
    assert await attempt(holder, other, token) == refused  # used on another sale
    assert await attempt(holder, sale, expired) == refused
    assert await attempt(holder, sale, forged) == refused
    assert await attempt(holder, sale, access_token) == refused  # a login token is not a turn
    assert await attempt(holder, sale, "garbage") == refused
    assert (await attempt(holder, sale, token))[0] == 201


async def test_the_lobby_is_drawn_in_random_order_at_opening(
    client: AsyncClient, app: FastAPI, sessionmaker: Sessions, settings: Settings
) -> None:
    sale = await guarded_sale(client, sessionmaker, opens_in=timedelta(hours=1))
    early = await make_buyers(sessionmaker, settings, 40)
    late = (await make_buyers(sessionmaker, settings, 1))[0]

    for buyer in early:  # they arrive one at a time, in this order
        status = await queue(client, buyer, sale)
        assert status["status"] == "lobby"
    assert status["queue_length"] == 40
    assert "position" not in {k for k, v in status.items() if v is not None}

    async with sessionmaker.begin() as session:  # the doors open
        await session.execute(
            text("UPDATE tournaments SET sales_open_at = now() - interval '1 second'")
        )
    # Someone who was not in the lobby triggers the draw. They still go last.
    assert (await queue(client, late, sale))["position"] == 41

    positions = [(await queue(client, buyer, sale))["position"] for buyer in early]
    assert sorted(positions) == list(range(1, 41))  # everyone has a distinct place
    # Arriving first earned nothing: the order is not the order of arrival.
    # (The chance of a fair shuffle of 40 matching it is 1 in 40 factorial.)
    assert positions != list(range(1, 41))
    assert positions != list(range(40, 0, -1))
    # And the draw happens once: asking again does not reshuffle.
    assert [(await queue(client, buyer, sale))["position"] for buyer in early] == positions


async def test_two_draws_give_different_orders(
    client: AsyncClient, sessionmaker: Sessions, settings: Settings
) -> None:
    """The shuffle is random each time, not a fixed permutation."""
    orders = []
    for attempt in range(2):
        sale = await guarded_sale(
            client,
            sessionmaker,
            opens_in=timedelta(hours=1),
            organizer_email=f"org{attempt}@example.com",
            title=f"Draw {attempt}",
        )
        buyers = await make_buyers(sessionmaker, settings, 30)
        for buyer in buyers:
            await queue(client, buyer, sale)
        async with sessionmaker.begin() as session:
            await session.execute(
                text("UPDATE tournaments SET sales_open_at = now() - interval '1 second'")
            )
        orders.append([(await queue(client, buyer, sale))["position"] for buyer in buyers])
    assert orders[0] != orders[1]


async def test_people_are_admitted_at_a_steady_rate(
    client: AsyncClient, app: FastAPI, sessionmaker: Sessions, settings: Settings
) -> None:
    sale = await guarded_sale(client, sessionmaker)
    buyers = await make_buyers(sessionmaker, settings, 100)
    await asyncio.gather(*(queue(client, buyer, sale) for buyer in buyers))

    rate = settings.waiting_room_admit_per_second
    start = time.time()
    await waitingroom.admit(app.state.redis, settings, now=start)  # starts the clock

    async def admitted() -> int:
        statuses = await asyncio.gather(*(queue(client, buyer, sale) for buyer in buyers))
        return sum(s["status"] == "admitted" for s in statuses)

    assert await admitted() == 0
    await waitingroom.admit(app.state.redis, settings, now=start + 1)
    assert await admitted() == rate
    # Two workers ticking at the same moment do not admit twice as many.
    await asyncio.gather(
        waitingroom.admit(app.state.redis, settings, now=start + 2),
        waitingroom.admit(app.state.redis, settings, now=start + 2),
    )
    assert await admitted() == 2 * rate
    # Fractions of a second accumulate; nothing is lost between ticks.
    for tick in range(1, 11):
        await waitingroom.admit(app.state.redis, settings, now=start + 2 + tick * 0.1)
    # (Give or take one: a fraction of a turn can be carried into the next tick.)
    assert 3 * rate - 1 <= await admitted() <= 3 * rate
    await waitingroom.admit(app.state.redis, settings, now=start + 3.06)
    assert await admitted() == 3 * rate + 1

    estimate = next(
        s for s in [await queue(client, b, sale) for b in buyers] if s["status"] == "queued"
    )
    assert estimate["estimated_wait_seconds"] is not None


async def test_an_idle_admitter_does_not_bank_turns(
    client: AsyncClient, app: FastAPI, sessionmaker: Sessions, settings: Settings
) -> None:
    """After a quiet hour, a sudden crowd must still queue, not all walk straight in."""
    sale = await guarded_sale(client, sessionmaker)
    first, *crowd = await make_buyers(sessionmaker, settings, 80)
    await queue(client, first, sale)
    start = time.time()
    await waitingroom.admit(app.state.redis, settings, now=start)
    await waitingroom.admit(app.state.redis, settings, now=start + 3600)  # an hour passes

    statuses = await asyncio.gather(*(queue(client, buyer, sale) for buyer in crowd))
    let_through = sum(s["status"] == "admitted" for s in statuses)
    # A small allowance is banked so a lone visitor is not kept waiting.
    assert 0 < let_through <= settings.waiting_room_admit_per_second


async def test_a_full_guarded_sale(
    client: AsyncClient, app: FastAPI, sessionmaker: Sessions, settings: Settings
) -> None:
    """120 buyers queue for 30 seats. Everyone gets a turn; nothing is oversold."""
    sale = await guarded_sale(client, sessionmaker, seats=30)
    buyers = await make_buyers(sessionmaker, settings, 120)
    wanted = {id(b): sale.seat_ids[n % 30] for n, b in enumerate(buyers)}
    outcomes: list[int] = []

    async def shop(buyer: Buyer) -> None:
        for _ in range(200):
            status = await queue(client, buyer, sale)
            if status["status"] == "admitted":
                response = await client.post(
                    "/holds",
                    json={"tournament": sale.slug, "seat_ids": [wanted[id(buyer)]]},
                    headers={
                        **buyer,
                        **key(),
                        "X-Admission-Token": str(status["admission_token"]),
                    },
                )
                outcomes.append(response.status_code)
                return
            await asyncio.sleep(0.01)
        outcomes.append(0)  # never admitted

    async def admitter() -> None:
        now = time.time()
        for tick in range(400):
            await waitingroom.admit(app.state.redis, settings, now=now + tick * 0.05)
            await asyncio.sleep(0.005)

    await asyncio.gather(admitter(), *(shop(buyer) for buyer in buyers))
    assert sorted(set(outcomes)) == [201, 409]  # everyone got their turn
    assert outcomes.count(201) == 30
    await assert_invariants(sessionmaker)
