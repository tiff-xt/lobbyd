"""Holds, checkout, and tickets: the behaviour one buyer sees."""

from fastapi import FastAPI
from httpx import AsyncClient
from sqlalchemy import text, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from lobbyd.catalog.models import Tournament
from lobbyd.config import Settings
from lobbyd.worker import sweep
from tests.helpers import buy, checkout, hold, key, make_buyers, open_sale, settle
from tests.invariants import assert_invariants, assert_settled

Sessions = async_sessionmaker[AsyncSession]


async def available(client: AsyncClient, slug: str) -> dict[str, int]:
    body = (await client.get(f"/tournaments/{slug}/availability")).json()
    return {t["kind"]: t["available"] for t in body["ticket_types"]}


async def lapse(sessionmaker: Sessions, where: str = "true") -> None:
    """Move holds into the past, as if their ten minutes had run out."""
    async with sessionmaker.begin() as session:
        await session.execute(
            text(
                "UPDATE tournament_seats SET held_until = now() - interval '1 second' "
                f"WHERE hold_id IN (SELECT id FROM holds WHERE {where})"
            )
        )
        await session.execute(
            text(f"UPDATE holds SET expires_at = now() - interval '1 second' WHERE {where}")
        )


async def test_publishing_puts_inventory_on_sale(
    client: AsyncClient, sessionmaker: Sessions
) -> None:
    sale = await open_sale(client, sessionmaker, seats=40, general=20, teams=4)
    body = (await client.get(f"/tournaments/{sale.slug}/availability")).json()
    assert {t["kind"]: (t["capacity"], t["available"]) for t in body["ticket_types"]} == {
        "reserved": (40, 40),
        "general": (20, 20),
        "competitor": (4, 4),
    }
    assert body["taken_seat_ids"] == []


async def test_hold_then_checkout_issues_tickets(
    client: AsyncClient, app: FastAPI, sessionmaker: Sessions, settings: Settings
) -> None:
    sale = await open_sale(client, sessionmaker)
    (buyer,) = await make_buyers(sessionmaker, settings, 1)

    response = await hold(client, buyer, sale, seats=sale.seat_ids[:2], general=3, team=True)
    assert response.status_code == 201, response.text
    held = response.json()
    assert held["status"] == "active"
    assert held["currency"] == "EUR"
    assert held["total_cents"] == 2 * 4000 + 3 * 1500 + 5000
    assert len(held["items"]) == 4

    assert await available(client, sale.slug) == {"reserved": 38, "general": 17, "competitor": 3}
    taken = (await client.get(f"/tournaments/{sale.slug}/availability")).json()["taken_seat_ids"]
    assert sorted(taken) == sorted(sale.seat_ids[:2])
    assert (await client.get(f"/holds/{held['id']}", headers=buyer)).json() == held

    # Checkout answers at once with the order pending; payment follows.
    placed = await checkout(client, buyer, held["id"])
    assert placed.status_code == 201, placed.text
    assert placed.json()["status"] == "pending"
    assert placed.json()["tickets"] == []
    assert placed.json()["payment"] == {"status": "created", "method": "pm_success"}
    # While paying, the inventory stays theirs.
    assert await available(client, sale.slug) == {"reserved": 38, "general": 17, "competitor": 3}

    await settle(app)
    paid = (await client.get(f"/orders/{placed.json()['id']}", headers=buyer)).json()
    assert paid["status"] == "paid"
    assert paid["payment"] == {"status": "succeeded", "method": "pm_success"}
    assert paid["total_cents"] == held["total_cents"]
    assert paid["paid_at"] is not None
    # One ticket per seat, one per general admission place, one for the team.
    assert len(paid["tickets"]) == 2 + 3 + 1
    assert sorted(t["seat_id"] for t in paid["tickets"] if t["seat_id"]) == sorted(
        sale.seat_ids[:2]
    )

    # Sold inventory stays off sale, and the hold is spent.
    assert await available(client, sale.slug) == {"reserved": 38, "general": 17, "competitor": 3}
    assert (await client.get(f"/holds/{held['id']}", headers=buyer)).json()["status"] == "converted"
    assert (await client.get(f"/orders/{paid['id']}", headers=buyer)).json() == paid
    assert (await client.get("/users/me/orders", headers=buyer)).json() == [paid]
    await assert_settled(sessionmaker)


async def test_a_hold_is_all_or_nothing(
    client: AsyncClient, sessionmaker: Sessions, settings: Settings
) -> None:
    sale = await open_sale(client, sessionmaker, general=5)
    first, second = await make_buyers(sessionmaker, settings, 2)
    assert (await hold(client, first, sale, seats=sale.seat_ids[1:2])).status_code == 201

    # One of three seats is taken: none of the three is held, nor the standing places.
    response = await hold(client, second, sale, seats=sale.seat_ids[:3], general=2)
    assert response.status_code == 409
    assert response.json()["code"] == "seats_unavailable"
    assert response.json()["seat_ids"] == [sale.seat_ids[1]]
    assert await available(client, sale.slug) == {"reserved": 39, "general": 5, "competitor": 4}

    # Seats are free but general admission is short: again nothing is held.
    response = await hold(client, second, sale, seats=[sale.seat_ids[5]], general=6)
    assert response.status_code == 409
    assert response.json()["code"] == "sold_out"
    assert response.json()["ticket_type_id"] == sale.general
    assert await available(client, sale.slug) == {"reserved": 39, "general": 5, "competitor": 4}

    # The refused attempts left no hold behind, so the buyer can simply try again.
    assert (
        await hold(client, second, sale, seats=[sale.seat_ids[5]], general=5)
    ).status_code == 201
    await assert_invariants(sessionmaker)


async def test_one_open_hold_per_user_per_tournament(
    client: AsyncClient, app: FastAPI, sessionmaker: Sessions, settings: Settings
) -> None:
    sale = await open_sale(client, sessionmaker)
    other = await open_sale(
        client, sessionmaker, organizer_email="second@example.com", title="Other Cup"
    )
    (buyer,) = await make_buyers(sessionmaker, settings, 1)

    first = await hold(client, buyer, sale, seats=sale.seat_ids[:1])
    again = await hold(client, buyer, sale, seats=sale.seat_ids[1:2])
    assert again.status_code == 409
    assert again.json()["code"] == "hold_exists"
    assert again.json()["hold_id"] == first.json()["id"]
    # A different tournament is a different matter.
    assert (await hold(client, buyer, other, seats=other.seat_ids[:1])).status_code == 201

    # After checking out, they can come back for more.
    assert (await buy(client, app, buyer, first.json()["id"]))["status"] == "paid"
    assert (await hold(client, buyer, sale, seats=sale.seat_ids[1:2])).status_code == 201


async def test_releasing_a_hold_puts_everything_back(
    client: AsyncClient, sessionmaker: Sessions, settings: Settings
) -> None:
    sale = await open_sale(client, sessionmaker)
    buyer, other = await make_buyers(sessionmaker, settings, 2)
    held = (await hold(client, buyer, sale, seats=sale.seat_ids[:2], general=4, team=True)).json()

    assert (await client.delete(f"/holds/{held['id']}", headers=other)).status_code == 404
    assert (await client.delete(f"/holds/{held['id']}", headers=buyer)).status_code == 204
    assert (await client.delete(f"/holds/{held['id']}", headers=buyer)).status_code == 204  # again
    assert await available(client, sale.slug) == {"reserved": 40, "general": 20, "competitor": 4}
    assert (await client.get(f"/holds/{held['id']}", headers=buyer)).json()["status"] == "released"

    # Straight away, not after the ten minutes: someone else can have them now.
    assert (await hold(client, other, sale, seats=sale.seat_ids[:2])).status_code == 201
    refused = await checkout(client, buyer, held["id"])
    assert refused.status_code == 409
    assert refused.json()["code"] == "hold_expired"
    await assert_invariants(sessionmaker)


async def test_lapsed_seats_are_claimable_without_any_sweeper(
    client: AsyncClient, app: FastAPI, sessionmaker: Sessions, settings: Settings
) -> None:
    sale = await open_sale(client, sessionmaker)
    slow, quick = await make_buyers(sessionmaker, settings, 2)
    stale = (await hold(client, slow, sale, seats=sale.seat_ids[:2])).json()
    assert (await hold(client, quick, sale, seats=sale.seat_ids[:2])).status_code == 409

    await lapse(sessionmaker)  # time passes; no background job runs

    assert await available(client, sale.slug) == {"reserved": 40, "general": 20, "competitor": 4}
    assert (await client.get(f"/holds/{stale['id']}", headers=slow)).json()["status"] == "expired"
    fresh = await hold(client, quick, sale, seats=sale.seat_ids[:2])
    assert fresh.status_code == 201, fresh.text

    # The slow buyer's checkout must not win the seats back.
    late = await checkout(client, slow, stale["id"])
    assert late.status_code == 409
    assert late.json()["code"] == "hold_expired"
    assert (await buy(client, app, quick, fresh.json()["id"]))["status"] == "paid"

    # When the sweeper does come by, it must not disturb the new owner.
    await sweep(sessionmaker)
    await assert_settled(sessionmaker)
    assert await available(client, sale.slug) == {"reserved": 38, "general": 20, "competitor": 4}


async def test_checkout_cannot_start_on_a_lapsed_hold(
    client: AsyncClient, sessionmaker: Sessions, settings: Settings
) -> None:
    sale = await open_sale(client, sessionmaker)
    (buyer,) = await make_buyers(sessionmaker, settings, 1)
    held = (await hold(client, buyer, sale, seats=sale.seat_ids[:2])).json()
    await lapse(sessionmaker)

    late = await checkout(client, buyer, held["id"])
    assert (late.status_code, late.json()["code"]) == (409, "hold_expired")


async def test_starting_checkout_extends_the_hold(
    client: AsyncClient, sessionmaker: Sessions, settings: Settings
) -> None:
    sale = await open_sale(client, sessionmaker)
    (buyer,) = await make_buyers(sessionmaker, settings, 1)
    held = (await hold(client, buyer, sale, seats=sale.seat_ids[:2])).json()
    # Nine and a half of the ten minutes are gone when they press "pay".
    async with sessionmaker.begin() as session:
        await session.execute(text("UPDATE holds SET expires_at = now() + interval '30 seconds'"))
        await session.execute(
            text(
                "UPDATE tournament_seats SET held_until = now() + interval '30 seconds' "
                "WHERE status = 'held'"
            )
        )

    assert (await checkout(client, buyer, held["id"])).status_code == 201

    async with sessionmaker() as session:
        hold_left = await session.scalar(
            text("SELECT extract(epoch FROM expires_at - now()) FROM holds")
        )
        seats_left = await session.scalar(
            text(
                "SELECT min(extract(epoch FROM held_until - now())) FROM tournament_seats "
                "WHERE status = 'held'"
            )
        )
    # Both the hold and its seats now last the payment window (five minutes).
    assert 290 < hold_left <= 300
    assert 290 < seats_left <= 300


async def test_lapsed_counted_inventory_is_reclaimed_on_demand(
    client: AsyncClient, sessionmaker: Sessions, settings: Settings
) -> None:
    sale = await open_sale(client, sessionmaker, general=3, teams=1)
    slow, quick = await make_buyers(sessionmaker, settings, 2)
    stale = (await hold(client, slow, sale, general=3, team=True)).json()
    assert (await hold(client, quick, sale, general=1)).status_code == 409

    await lapse(sessionmaker)  # still no sweeper

    # Counters cannot see the clock, so the claim itself closes the lapsed hold.
    fresh = await hold(client, quick, sale, general=3, team=True)
    assert fresh.status_code == 201, fresh.text
    assert (await checkout(client, slow, stale["id"])).json()["code"] == "hold_expired"
    assert await available(client, sale.slug) == {"reserved": 40, "general": 0, "competitor": 0}
    await assert_invariants(sessionmaker)


async def test_a_lapsed_hold_does_not_block_its_owner_from_trying_again(
    client: AsyncClient, sessionmaker: Sessions, settings: Settings
) -> None:
    sale = await open_sale(client, sessionmaker, general=2)
    (buyer,) = await make_buyers(sessionmaker, settings, 1)
    await hold(client, buyer, sale, seats=sale.seat_ids[:1], general=2)
    await lapse(sessionmaker)

    again = await hold(client, buyer, sale, seats=sale.seat_ids[:1], general=2)
    assert again.status_code == 201, again.text
    await assert_invariants(sessionmaker)


async def test_sweeper_closes_lapsed_holds_and_tidies_seats(
    client: AsyncClient, sessionmaker: Sessions, settings: Settings
) -> None:
    sale = await open_sale(client, sessionmaker)
    lapsed, current = await make_buyers(sessionmaker, settings, 2)
    old = (await hold(client, lapsed, sale, seats=sale.seat_ids[:3], general=5, team=True)).json()
    kept = (await hold(client, current, sale, seats=sale.seat_ids[3:5], general=1)).json()
    await lapse(sessionmaker, f"id = '{old['id']}'")

    assert await sweep(sessionmaker) == (1, 3)
    assert await sweep(sessionmaker) == (0, 0)  # nothing left to do

    assert (await client.get(f"/holds/{old['id']}", headers=lapsed)).json()["status"] == "expired"
    assert (await client.get(f"/holds/{kept['id']}", headers=current)).json()["status"] == "active"
    assert await available(client, sale.slug) == {"reserved": 38, "general": 19, "competitor": 4}
    async with sessionmaker() as session:
        stale_rows = await session.scalar(
            text(
                "SELECT count(*) FROM tournament_seats WHERE status = 'held' AND held_until < now()"
            )
        )
    assert stale_rows == 0
    await assert_invariants(sessionmaker)


async def test_what_can_be_held(
    client: AsyncClient, sessionmaker: Sessions, settings: Settings
) -> None:
    sale = await open_sale(client, sessionmaker)
    other = await open_sale(
        client, sessionmaker, organizer_email="second@example.com", title="Other Cup"
    )
    (buyer,) = await make_buyers(sessionmaker, settings, 1)

    async def attempt(**body: object) -> tuple[int, str]:
        response = await client.post(
            "/holds", json={"tournament": sale.slug, **body}, headers={**buyer, **key()}
        )
        return response.status_code, response.json().get("code", "")

    reserved_type = sale.types["Seated"]
    assert await attempt() == (422, "validation_error")  # nothing asked for
    assert await attempt(seat_ids=[sale.seat_ids[0]] * 2) == (422, "validation_error")
    assert await attempt(seat_ids=sale.seat_ids[:9]) == (422, "validation_error")  # more than 8
    assert await attempt(seat_ids=[other.seat_ids[0]]) == (422, "unknown_seat")
    assert await attempt(items=[{"ticket_type_id": sale.general, "quantity": 9}]) == (
        422,
        "validation_error",
    )
    assert await attempt(items=[{"ticket_type_id": other.general, "quantity": 1}]) == (
        422,
        "unknown_ticket_type",
    )
    # Reserved seating is bought by seat, not by quantity.
    assert await attempt(items=[{"ticket_type_id": reserved_type, "quantity": 1}]) == (
        422,
        "unknown_ticket_type",
    )
    assert await attempt(items=[{"ticket_type_id": sale.team, "quantity": 2}]) == (
        422,
        "invalid_quantity",
    )
    missing = await client.post(
        "/holds",
        json={"tournament": "no-such-tournament", "seat_ids": sale.seat_ids[:1]},
        headers={**buyer, **key()},
    )
    assert (missing.status_code, missing.json()["code"]) == (404, "tournament_not_found")
    assert (await hold(client, {}, sale, seats=sale.seat_ids[:1])).status_code == 401
    assert await available(client, sale.slug) == {"reserved": 40, "general": 20, "competitor": 4}


async def test_sales_window(
    client: AsyncClient, sessionmaker: Sessions, settings: Settings
) -> None:
    sale = await open_sale(client, sessionmaker)
    (buyer,) = await make_buyers(sessionmaker, settings, 1)

    async def set_dates(sql: str) -> None:
        async with sessionmaker.begin() as session:
            await session.execute(text(f"UPDATE tournaments SET {sql}"))

    await set_dates("sales_open_at = now() + interval '1 hour'")
    early = await hold(client, buyer, sale, seats=sale.seat_ids[:1])
    assert (early.status_code, early.json()["code"]) == (409, "sales_not_open")
    assert "opens_at" in early.json()

    await set_dates(
        "sales_open_at = now() - interval '3 days', starts_at = now() - interval '2 days', "
        "ends_at = now() - interval '1 day'"
    )
    late = await hold(client, buyer, sale, seats=sale.seat_ids[:1])
    assert (late.status_code, late.json()["code"]) == (409, "sales_closed")

    await set_dates("starts_at = now() + interval '5 days', ends_at = now() + interval '6 days'")
    async with sessionmaker.begin() as session:
        await session.execute(update(Tournament).values(status="cancelled"))
    cancelled = await hold(client, buyer, sale, seats=sale.seat_ids[:1])
    assert (cancelled.status_code, cancelled.json()["code"]) == (409, "sales_closed")


async def test_holds_and_orders_are_private(
    client: AsyncClient, sessionmaker: Sessions, settings: Settings
) -> None:
    sale = await open_sale(client, sessionmaker)
    owner, stranger = await make_buyers(sessionmaker, settings, 2)
    held = (await hold(client, owner, sale, seats=sale.seat_ids[:1])).json()

    assert (await client.get(f"/holds/{held['id']}", headers=stranger)).status_code == 404
    stolen = await checkout(client, stranger, held["id"])
    assert (stolen.status_code, stolen.json()["code"]) == (404, "hold_not_found")

    order = (await checkout(client, owner, held["id"])).json()
    assert (await client.get(f"/orders/{order['id']}", headers=owner)).status_code == 200
    assert (await client.get(f"/orders/{order['id']}", headers=stranger)).status_code == 404
    assert (await client.get("/users/me/orders", headers=stranger)).json() == []
