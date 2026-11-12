"""Many buyers at the same instant. These are the tests the project exists for.

Every test ends by checking the full set of invariants (tests/invariants.py),
and no response may be a 500: a deadlock or a broken constraint would
surface as one.
"""

import asyncio
import random
from collections import Counter
from typing import Any

from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from lobbyd.config import Settings
from lobbyd.worker import sweep
from tests.helpers import Sale, checkout, hold, key, make_buyers, open_sale
from tests.invariants import assert_invariants

Sessions = async_sessionmaker[AsyncSession]


def statuses(responses: list[Any]) -> Counter[int]:
    return Counter(r.status_code for r in responses)


async def test_many_buyers_one_seat(
    client: AsyncClient, sessionmaker: Sessions, settings: Settings
) -> None:
    sale = await open_sale(client, sessionmaker)
    buyers = await make_buyers(sessionmaker, settings, 100)

    responses = await asyncio.gather(
        *(hold(client, buyer, sale, seats=sale.seat_ids[:1]) for buyer in buyers)
    )
    assert statuses(responses) == {201: 1, 409: 99}
    await assert_invariants(sessionmaker)


async def test_overlapping_multi_seat_claims(
    client: AsyncClient, sessionmaker: Sessions, settings: Settings
) -> None:
    """300 buyers each want a random run of 1 to 4 seats out of 60."""
    sale = await open_sale(client, sessionmaker, seats=60)
    buyers = await make_buyers(sessionmaker, settings, 300)
    rng = random.Random(7)
    wanted = []
    for _ in buyers:
        size = rng.randint(1, 4)
        start = rng.randrange(0, 60 - size + 1)
        seats = sale.seat_ids[start : start + size]
        rng.shuffle(seats)  # buyers do not ask in a helpful order
        wanted.append(seats)

    responses = await asyncio.gather(
        *(
            hold(client, buyer, sale, seats=seats)
            for buyer, seats in zip(buyers, wanted, strict=True)
        )
    )
    assert set(statuses(responses)) <= {201, 409}, statuses(responses)

    held: list[str] = []
    for response, seats in zip(responses, wanted, strict=True):
        if response.status_code == 201:
            # All or nothing: a winner got exactly what they asked for.
            assert sorted(i["seat_id"] for i in response.json()["items"]) == sorted(seats)
            held += seats
        else:
            assert response.json()["code"] == "seats_unavailable"
    assert len(held) == len(set(held)), "a seat was held twice"

    # Nobody was refused seats that were in fact all free: every loser
    # overlaps at least one seat that somebody holds.
    for response, seats in zip(responses, wanted, strict=True):
        if response.status_code == 409:
            assert set(seats) & set(held)

    availability = (await client.get(f"/tournaments/{sale.slug}/availability")).json()
    assert sorted(availability["taken_seat_ids"]) == sorted(held)
    await assert_invariants(sessionmaker)


async def test_claims_in_opposite_order_do_not_deadlock(
    client: AsyncClient, sessionmaker: Sessions, settings: Settings
) -> None:
    sale = await open_sale(client, sessionmaker, seats=8)
    buyers = await make_buyers(sessionmaker, settings, 60)
    forwards, backwards = sale.seat_ids[:8], sale.seat_ids[:8][::-1]

    responses = await asyncio.gather(
        *(
            # Seats only. Adding counted items would take a counter lock first
            # and queue everyone behind it, hiding any seat-order problem.
            hold(client, buyer, sale, seats=forwards if n % 2 else backwards)
            for n, buyer in enumerate(buyers)
        )
    )
    assert statuses(responses) == {201: 1, 409: 59}
    await assert_invariants(sessionmaker)


async def test_mixed_baskets_in_opposite_order_do_not_deadlock(
    client: AsyncClient, sessionmaker: Sessions, settings: Settings
) -> None:
    """Seats, standing places, and a team slot together, asked for in both orders."""
    sale = await open_sale(client, sessionmaker, seats=8, general=200, teams=100)
    buyers = await make_buyers(sessionmaker, settings, 60)
    forwards, backwards = sale.seat_ids[:8], sale.seat_ids[:8][::-1]

    responses = await asyncio.gather(
        *(
            hold(
                client,
                buyer,
                sale,
                seats=forwards if n % 2 else backwards,
                general=1,
                team=n % 3 == 0,
            )
            for n, buyer in enumerate(buyers)
        )
    )
    assert statuses(responses) == {201: 1, 409: 59}
    await assert_invariants(sessionmaker)


async def test_counted_inventory_never_oversells_or_undersells(
    client: AsyncClient, sessionmaker: Sessions, settings: Settings
) -> None:
    """300 buyers want 1 to 3 standing places each; there are 100."""
    sale = await open_sale(client, sessionmaker, general=100)
    buyers = await make_buyers(sessionmaker, settings, 300)
    rng = random.Random(11)
    quantities = [rng.randint(1, 3) for _ in buyers]

    responses = await asyncio.gather(
        *(hold(client, b, sale, general=q) for b, q in zip(buyers, quantities, strict=True))
    )
    assert set(statuses(responses)) <= {201, 409}
    held = sum(q for r, q in zip(responses, quantities, strict=True) if r.status_code == 201)
    refused = [q for r, q in zip(responses, quantities, strict=True) if r.status_code == 409]

    assert held <= 100, "oversold"
    # No underselling either: anyone refused asked for more than was left.
    assert refused
    assert 100 - held < min(refused)
    await assert_invariants(sessionmaker)


async def test_last_team_slot(
    client: AsyncClient, sessionmaker: Sessions, settings: Settings
) -> None:
    sale = await open_sale(client, sessionmaker, teams=3)
    buyers = await make_buyers(sessionmaker, settings, 50)
    responses = await asyncio.gather(*(hold(client, b, sale, team=True) for b in buyers))
    assert statuses(responses) == {201: 3, 409: 47}
    await assert_invariants(sessionmaker)


async def test_one_user_double_clicking(
    client: AsyncClient, sessionmaker: Sessions, settings: Settings
) -> None:
    """The same user, different keys, at once: one hold, then one order."""
    sale = await open_sale(client, sessionmaker)
    (buyer,) = await make_buyers(sessionmaker, settings, 1)

    holds = await asyncio.gather(
        *(hold(client, buyer, sale, seats=[sale.seat_ids[n]], general=1) for n in range(8))
    )
    assert statuses(holds) == {201: 1, 409: 7}
    hold_id = next(r.json()["id"] for r in holds if r.status_code == 201)

    orders = await asyncio.gather(*(checkout(client, buyer, hold_id) for _ in range(8)))
    assert statuses(orders) == {201: 1, 409: 7}
    assert {r.json()["code"] for r in orders if r.status_code == 409} == {"hold_already_used"}
    await assert_invariants(sessionmaker)


async def _lapse_all(sessionmaker: Sessions) -> None:
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


async def test_checkout_racing_a_new_claim_for_lapsed_inventory(
    client: AsyncClient, sessionmaker: Sessions, settings: Settings
) -> None:
    """Forty lapsed holds. At the same instant their owners check out, new
    buyers claim the same inventory, and the sweeper runs. Each seat and each
    place must end up with exactly one owner."""
    sale = await open_sale(client, sessionmaker, seats=40, general=40, teams=1)
    old = await make_buyers(sessionmaker, settings, 40)
    new = await make_buyers(sessionmaker, settings, 40)

    first = await asyncio.gather(
        *(hold(client, b, sale, seats=[sale.seat_ids[n]], general=1) for n, b in enumerate(old))
    )
    assert statuses(first) == {201: 40}
    await _lapse_all(sessionmaker)

    results = await asyncio.gather(
        *(checkout(client, b, r.json()["id"]) for b, r in zip(old, first, strict=True)),
        *(hold(client, b, sale, seats=[sale.seat_ids[n]], general=1) for n, b in enumerate(new)),
        sweep(sessionmaker),
        sweep(sessionmaker),
    )
    checkouts, claims = results[:40], results[40:80]
    assert set(statuses(checkouts)) <= {201, 409}, statuses(checkouts)
    assert set(statuses(claims)) <= {201, 409}, statuses(claims)

    for n in range(40):
        # A seat cannot have gone to both the old owner and the new one.
        assert not (checkouts[n].status_code == 201 and claims[n].status_code == 201)
    await assert_invariants(sessionmaker)

    # Everyone who now holds something can still check out.
    winners = [(b, r) for b, r in zip(new, claims, strict=True) if r.status_code == 201]
    final = await asyncio.gather(*(checkout(client, b, r.json()["id"]) for b, r in winners))
    assert statuses(final) in ({201: len(winners)}, Counter())
    await assert_invariants(sessionmaker)


async def _full_sale(
    client: AsyncClient, sale: Sale, buyers: list[dict[str, str]], rng: random.Random
) -> tuple[int, int]:
    """Every buyer tries to hold something, then checks out twice at once."""

    async def shop(buyer: dict[str, str]) -> tuple[int, int]:
        size = rng.randint(1, 3)
        start = rng.randrange(0, len(sale.seat_ids) - size + 1)
        response = await hold(
            client,
            buyer,
            sale,
            seats=sale.seat_ids[start : start + size],
            general=rng.randint(0, 2),
            team=rng.random() < 0.1,
        )
        assert response.status_code in (201, 409), response.text
        if response.status_code != 201:
            return 0, 0
        same = key()
        # An impatient double submit: one retry with the same key, one without.
        orders = await asyncio.gather(
            checkout(client, buyer, response.json()["id"], same),
            checkout(client, buyer, response.json()["id"], same),
            checkout(client, buyer, response.json()["id"]),
        )
        assert all(o.status_code in (201, 409) for o in orders), [o.text for o in orders]
        paid = {o.json()["id"] for o in orders if o.status_code == 201}
        assert len(paid) == 1, "one hold produced more than one order"
        return 1, len(next(o for o in orders if o.status_code == 201).json()["tickets"])

    outcomes = await asyncio.gather(*(shop(buyer) for buyer in buyers))
    return sum(o[0] for o in outcomes), sum(o[1] for o in outcomes)


async def test_a_whole_sale_end_to_end(
    client: AsyncClient, sessionmaker: Sessions, settings: Settings
) -> None:
    """400 buyers, 120 seats, 60 standing places, 4 team slots, mixed baskets,
    double-submitted checkouts, and the sweeper running throughout."""
    sale = await open_sale(client, sessionmaker, seats=120, general=60, teams=4)
    buyers = await make_buyers(sessionmaker, settings, 400)

    (orders, tickets), *_ = await asyncio.gather(
        _full_sale(client, sale, buyers, random.Random(3)),
        sweep(sessionmaker),
        sweep(sessionmaker),
    )
    assert orders > 0
    await assert_invariants(sessionmaker)

    async with sessionmaker() as session:
        stored_orders = await session.scalar(text("SELECT count(*) FROM orders"))
        stored_tickets = await session.scalar(text("SELECT count(*) FROM tickets"))
        sold_seats = await session.scalar(
            text("SELECT count(*) FROM tournament_seats WHERE status = 'sold'")
        )
        seated_tickets = await session.scalar(
            text("SELECT count(*) FROM tickets WHERE seat_id IS NOT NULL")
        )
    assert stored_orders == orders
    assert stored_tickets == tickets
    assert sold_seats == seated_tickets
    assert sold_seats <= 120
