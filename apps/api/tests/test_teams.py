"""Team registration and the waitlist."""

import asyncio
from collections import Counter
from typing import Any

from fastapi import FastAPI
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from lobbyd.config import Settings
from lobbyd.teams import service as teams
from lobbyd.worker import sweep
from tests.helpers import (
    Sale,
    buy,
    checkout,
    hold,
    key,
    make_buyers,
    open_sale,
    register_team,
    roster,
    settle,
)
from tests.invariants import assert_invariants, assert_settled
from tests.test_booking import available, lapse

Sessions = async_sessionmaker[AsyncSession]
Headers = dict[str, str]


async def housekeeping(app: FastAPI, sessionmaker: Sessions, settings: Settings) -> int:
    """What the worker does for teams every few seconds. Returns offers made."""
    await settle(app)
    await sweep(sessionmaker)
    async with sessionmaker.begin() as session:
        await teams.release_abandoned(session)
    async with sessionmaker.begin() as session:
        offers = await teams.promote_waitlist(session, settings)
    await settle(app)
    return offers


async def confirmed(client: AsyncClient, sale: Sale) -> list[str]:
    body = (await client.get(f"/tournaments/{sale.slug}/teams")).json()
    return sorted(team["name"] for team in body)


async def enter(client: AsyncClient, app: FastAPI, captain: Headers, sale: Sale, name: str) -> Any:
    """Register a team and pay its entry fee."""
    registered = await register_team(client, captain, sale, name=name, players=roster(name))
    assert registered.status_code == 201, registered.text
    order = await buy(client, app, captain, registered.json()["hold"]["id"])
    assert order["status"] == "paid"
    return registered.json()["team"]


async def wait(client: AsyncClient, captain: Headers, sale: Sale, name: str) -> Any:
    return await client.post(
        f"/tournaments/{sale.slug}/waitlist",
        json={"name": name, "roster": roster(name)},
        headers=captain,
    )


async def my_place(client: AsyncClient, captain: Headers, sale: Sale) -> Any:
    return (await client.get(f"/tournaments/{sale.slug}/waitlist/me", headers=captain)).json()


def codes(responses: list[Any]) -> Counter[str]:
    return Counter(r.json().get("code", "ok") if r.status_code >= 400 else "ok" for r in responses)


# --- registering ----------------------------------------------------------------


async def test_a_team_is_pending_until_its_entry_is_paid(
    client: AsyncClient, app: FastAPI, sessionmaker: Sessions, settings: Settings
) -> None:
    sale = await open_sale(client, sessionmaker, teams=4)
    (captain,) = await make_buyers(sessionmaker, settings, 1)

    registered = await register_team(
        client, captain, sale, name="Night Shift", players=["Ada", "Bo", "Cy", "Di", "Ed"]
    )
    assert registered.status_code == 201, registered.text
    body = registered.json()
    assert body["team"]["status"] == "pending"
    assert body["team"]["roster"] == ["Ada", "Bo", "Cy", "Di", "Ed"]  # as entered
    assert [item["ticket_type_name"] for item in body["hold"]["items"]] == ["Team entry"]
    assert await available(client, sale.slug) == {"reserved": 40, "general": 20, "competitor": 3}
    assert await confirmed(client, sale) == []  # not in until paid

    order = await buy(client, app, captain, body["hold"]["id"])
    assert order["status"] == "paid"
    assert await confirmed(client, sale) == ["Night Shift"]
    mine = (await client.get("/users/me/teams", headers=captain)).json()
    assert [(team["name"], team["status"]) for team in mine] == [("Night Shift", "confirmed")]
    await assert_settled(sessionmaker)


async def test_seats_can_ride_along_with_a_team_entry(
    client: AsyncClient, app: FastAPI, sessionmaker: Sessions, settings: Settings
) -> None:
    sale = await open_sale(client, sessionmaker)
    (captain,) = await make_buyers(sessionmaker, settings, 1)
    registered = await register_team(
        client,
        captain,
        sale,
        seats=sale.seat_ids[:2],
        items=[{"ticket_type_id": sale.general, "quantity": 3}],
    )
    assert registered.status_code == 201, registered.text
    held = registered.json()["hold"]
    assert held["total_cents"] == 5000 + 2 * 4000 + 3 * 1500
    order = await buy(client, app, captain, held["id"])
    assert order["status"] == "paid"
    assert len(order["tickets"]) == 6
    await assert_settled(sessionmaker)


async def test_a_refused_registration_takes_nothing(
    client: AsyncClient, app: FastAPI, sessionmaker: Sessions, settings: Settings
) -> None:
    sale = await open_sale(client, sessionmaker, teams=4)
    first, second, third = await make_buyers(sessionmaker, settings, 3)
    await enter(client, app, first, sale, "Night Shift")

    async def attempt(captain: Headers, **body: Any) -> tuple[int, str]:
        response = await register_team(client, captain, sale, seats=sale.seat_ids[:2], **body)
        return response.status_code, response.json()["code"]

    # Names are compared without regard to case.
    assert await attempt(second, name="night shift") == (409, "team_name_taken")
    # One of these players is already on Night Shift.
    borrowed = [*roster("Other")[:4], "NIGHT SHIFT-P1"]
    assert await attempt(second, players=borrowed) == (409, "player_already_registered")
    assert await attempt(first, name="Second Team") == (409, "already_registered")
    assert await attempt(second, players=roster("Short", 4)) == (422, "wrong_roster_size")
    assert await attempt(second, players=["Ada", "ada", "Bo", "Cy", "Di"]) == (
        422,
        "validation_error",
    )

    # Every refusal undid its slot claim and its seat claim.
    assert await available(client, sale.slug) == {"reserved": 40, "general": 20, "competitor": 3}
    assert (await register_team(client, third, sale, seats=sale.seat_ids[:2])).status_code == 201
    await assert_invariants(sessionmaker)


async def test_a_retried_registration_is_the_same_registration(
    client: AsyncClient, sessionmaker: Sessions, settings: Settings
) -> None:
    sale = await open_sale(client, sessionmaker, teams=4)
    (captain,) = await make_buyers(sessionmaker, settings, 1)
    idempotency = key()
    body: dict[str, Any] = {
        "name": "Night Shift",
        "players": roster("ns"),
        "idempotency": idempotency,
    }
    first = await register_team(client, captain, sale, **body)
    again = await register_team(client, captain, sale, **body)
    assert first.status_code == again.status_code == 201
    assert first.text == again.text
    assert await available(client, sale.slug) == {"reserved": 40, "general": 20, "competitor": 3}


async def test_fifty_captains_race_for_the_last_slots(
    client: AsyncClient, sessionmaker: Sessions, settings: Settings
) -> None:
    sale = await open_sale(client, sessionmaker, teams=3)
    captains = await make_buyers(sessionmaker, settings, 50)
    responses = await asyncio.gather(*(register_team(client, c, sale) for c in captains))
    assert codes(responses) == {"ok": 3, "sold_out": 47}
    async with sessionmaker() as session:
        # The losers left no team and no roster behind.
        assert await session.scalar(text("SELECT count(*) FROM teams")) == 3
        assert await session.scalar(text("SELECT count(*) FROM team_members")) == 15
    await assert_invariants(sessionmaker)


async def test_racing_for_one_name_and_one_player(
    client: AsyncClient, sessionmaker: Sessions, settings: Settings
) -> None:
    sale = await open_sale(client, sessionmaker, teams=16)
    captains = await make_buyers(sessionmaker, settings, 24)

    same_name = await asyncio.gather(
        *(register_team(client, c, sale, name="Night Shift") for c in captains[:12])
    )
    assert codes(same_name) == {"ok": 1, "team_name_taken": 11}

    # Twelve different teams that all list the same star player.
    same_player = await asyncio.gather(
        *(
            register_team(client, c, sale, players=[*roster(f"t{n}", 4), "Faker"])
            for n, c in enumerate(captains[12:])
        )
    )
    assert codes(same_player) == {"ok": 1, "player_already_registered": 11}
    assert (await available(client, sale.slug))["competitor"] == 14
    await assert_invariants(sessionmaker)


# --- dropping out ---------------------------------------------------------------


async def test_an_unpaid_team_gives_way_when_its_hold_lapses(
    client: AsyncClient, app: FastAPI, sessionmaker: Sessions, settings: Settings
) -> None:
    sale = await open_sale(client, sessionmaker, teams=1)
    slow, quick = await make_buyers(sessionmaker, settings, 2)
    stale = await register_team(client, slow, sale, name="Night Shift", players=roster("ns"))
    assert stale.status_code == 201

    await lapse(sessionmaker)
    await housekeeping(app, sessionmaker, settings)

    # The slot, the name, and the players are all free again.
    fresh = await register_team(client, quick, sale, name="Night Shift", players=roster("ns"))
    assert fresh.status_code == 201, fresh.text
    assert (await client.get("/users/me/teams", headers=slow)).json() == []
    assert (await checkout(client, slow, stale.json()["hold"]["id"])).json()["code"] == (
        "hold_expired"
    )
    await assert_invariants(sessionmaker)


async def test_a_declined_payment_takes_the_team_out(
    client: AsyncClient, app: FastAPI, sessionmaker: Sessions, settings: Settings
) -> None:
    sale = await open_sale(client, sessionmaker, teams=1)
    captain, other = await make_buyers(sessionmaker, settings, 2)
    registered = await register_team(client, captain, sale, name="Night Shift")
    order = await buy(client, app, captain, registered.json()["hold"]["id"], method="pm_declined")
    assert order["status"] == "cancelled"

    assert (await client.get("/users/me/teams", headers=captain)).json() == []
    assert (await available(client, sale.slug))["competitor"] == 1
    assert (await register_team(client, other, sale, name="Night Shift")).status_code == 201
    await assert_invariants(sessionmaker)


async def test_withdrawing_a_confirmed_team_returns_its_slot(
    client: AsyncClient, app: FastAPI, sessionmaker: Sessions, settings: Settings
) -> None:
    sale = await open_sale(client, sessionmaker, teams=1)
    captain, stranger, other = await make_buyers(sessionmaker, settings, 3)
    team = await enter(client, app, captain, sale, "Night Shift")
    assert (await available(client, sale.slug))["competitor"] == 0

    # Only its captain can withdraw it.
    assert (await client.delete(f"/teams/{team['id']}", headers=stranger)).status_code == 404
    assert (await client.delete(f"/teams/{team['id']}", headers=captain)).status_code == 204
    assert (await client.delete(f"/teams/{team['id']}", headers=captain)).status_code == 404
    await settle(app)

    assert await confirmed(client, sale) == []
    assert (await available(client, sale.slug))["competitor"] == 1
    (order,) = (await client.get("/users/me/orders", headers=captain)).json()
    assert [ticket["status"] for ticket in order["tickets"]] == ["void"]

    await enter(client, app, other, sale, "Night Shift")
    await assert_settled(sessionmaker)


async def test_withdrawing_an_unpaid_team_releases_its_hold(
    client: AsyncClient, sessionmaker: Sessions, settings: Settings
) -> None:
    sale = await open_sale(client, sessionmaker, teams=1)
    (captain,) = await make_buyers(sessionmaker, settings, 1)
    registered = (await register_team(client, captain, sale, seats=sale.seat_ids[:2])).json()
    assert (
        await client.delete(f"/teams/{registered['team']['id']}", headers=captain)
    ).status_code == 204
    assert await available(client, sale.slug) == {"reserved": 40, "general": 20, "competitor": 1}
    assert (await checkout(client, captain, registered["hold"]["id"])).status_code == 409
    await assert_invariants(sessionmaker)


# --- the waitlist ---------------------------------------------------------------


async def test_the_waitlist_is_only_for_a_full_tournament(
    client: AsyncClient, app: FastAPI, sessionmaker: Sessions, settings: Settings
) -> None:
    sale = await open_sale(client, sessionmaker, teams=1)
    first, second, third = await make_buyers(sessionmaker, settings, 3)

    early = await wait(client, second, sale, "Too Early")
    assert (early.status_code, early.json()["code"]) == (409, "slots_available")
    await enter(client, app, first, sale, "Night Shift")

    joined = await wait(client, second, sale, "Second")
    assert joined.status_code == 201, joined.text
    assert (joined.json()["status"], joined.json()["position"]) == ("waiting", 1)
    again = await wait(client, second, sale, "Second Again")
    assert (again.status_code, again.json()["code"]) == (409, "already_waiting")
    assert (await wait(client, third, sale, "Third")).json()["position"] == 2

    # Leaving moves everyone behind up one.
    assert (
        await client.delete(f"/tournaments/{sale.slug}/waitlist/me", headers=second)
    ).status_code == 204
    assert (await my_place(client, second, sale))["status"] == "left"
    assert (await my_place(client, third, sale))["position"] == 1
    missing = await client.get(f"/tournaments/{sale.slug}/waitlist/me", headers=first)
    assert (missing.status_code, missing.json()["code"]) == (404, "not_on_waitlist")


async def test_a_freed_slot_is_offered_to_the_next_in_line(
    client: AsyncClient, app: FastAPI, sessionmaker: Sessions, settings: Settings
) -> None:
    sale = await open_sale(client, sessionmaker, teams=1)
    first, second, third, passerby = await make_buyers(sessionmaker, settings, 4)
    team = await enter(client, app, first, sale, "Night Shift")
    await wait(client, second, sale, "Second")
    await wait(client, third, sale, "Third")
    assert await housekeeping(app, sessionmaker, settings) == 0  # nothing free yet

    await client.delete(f"/teams/{team['id']}", headers=first)
    assert await housekeeping(app, sessionmaker, settings) == 1

    # The slot went to the waitlist, not back on general sale.
    walk_in = await register_team(client, passerby, sale)
    assert (walk_in.status_code, walk_in.json()["code"]) == (409, "sold_out")
    offer = await my_place(client, second, sale)
    assert (offer["status"], offer["position"]) == ("offered", None)
    assert (await my_place(client, third, sale))["position"] == 1
    async with sessionmaker() as session:
        kinds = list(await session.scalars(text("SELECT kind FROM notifications ORDER BY id")))
        # Offers are held longer than a basket, so there is time to see the email.
        seconds = await session.scalar(
            text("SELECT extract(epoch FROM expires_at - created_at) FROM holds WHERE id = :id"),
            {"id": offer["offered_hold_id"]},
        )
    assert kinds.count("waitlist_offer") == 1
    assert round(seconds) == settings.waitlist_offer_seconds

    order = await buy(client, app, second, offer["offered_hold_id"])
    assert order["status"] == "paid"
    assert await confirmed(client, sale) == ["Second"]
    assert (await my_place(client, second, sale))["status"] == "accepted"
    await assert_settled(sessionmaker)


async def test_an_offer_not_taken_moves_to_the_next_captain(
    client: AsyncClient, app: FastAPI, sessionmaker: Sessions, settings: Settings
) -> None:
    sale = await open_sale(client, sessionmaker, teams=1)
    first, second, third = await make_buyers(sessionmaker, settings, 3)
    team = await enter(client, app, first, sale, "Night Shift")
    await wait(client, second, sale, "Second")
    await wait(client, third, sale, "Third")
    await client.delete(f"/teams/{team['id']}", headers=first)
    assert await housekeeping(app, sessionmaker, settings) == 1

    await lapse(sessionmaker)  # Second never checks out
    assert await housekeeping(app, sessionmaker, settings) == 1

    lapsed = await my_place(client, second, sale)
    assert lapsed["status"] == "lapsed"
    assert lapsed["note"]
    offer = await my_place(client, third, sale)
    assert offer["status"] == "offered"
    assert (await buy(client, app, third, offer["offered_hold_id"]))["status"] == "paid"
    assert await confirmed(client, sale) == ["Third"]
    await assert_settled(sessionmaker)


async def test_a_waitlist_roster_is_checked_again_when_the_slot_comes_up(
    client: AsyncClient, app: FastAPI, sessionmaker: Sessions, settings: Settings
) -> None:
    sale = await open_sale(client, sessionmaker, teams=2)
    first, second, third, fourth = await make_buyers(sessionmaker, settings, 4)
    leaving = await enter(client, app, first, sale, "Leaving")
    staying = await register_team(
        client, second, sale, name="Staying", players=[*roster("st", 4), "Faker"]
    )
    await buy(client, app, second, staying.json()["hold"]["id"])

    # Third lists a player who is already in the tournament. Nobody checks
    # yet: Staying might withdraw before a slot comes up.
    clash = await client.post(
        f"/tournaments/{sale.slug}/waitlist",
        json={"name": "Third", "roster": [*roster("th", 4), "faker"]},
        headers=third,
    )
    assert clash.status_code == 201, clash.text
    await wait(client, fourth, sale, "Fourth")

    await client.delete(f"/teams/{leaving['id']}", headers=first)
    assert await housekeeping(app, sessionmaker, settings) == 1

    passed_over = await my_place(client, third, sale)
    assert passed_over["status"] == "lapsed"
    assert "already on another team" in passed_over["note"]
    assert (await my_place(client, fourth, sale))["status"] == "offered"
    await assert_invariants(sessionmaker)


async def test_a_captain_mid_purchase_keeps_their_place(
    client: AsyncClient, app: FastAPI, sessionmaker: Sessions, settings: Settings
) -> None:
    sale = await open_sale(client, sessionmaker, teams=1)
    first, second, third = await make_buyers(sessionmaker, settings, 3)
    team = await enter(client, app, first, sale, "Night Shift")
    await wait(client, second, sale, "Second")
    await wait(client, third, sale, "Third")
    # Second is in the middle of buying spectator tickets when the slot frees up.
    basket = (await hold(client, second, sale, general=2)).json()

    await client.delete(f"/teams/{team['id']}", headers=first)
    assert await housekeeping(app, sessionmaker, settings) == 1
    assert (await my_place(client, third, sale))["status"] == "offered"
    still = await my_place(client, second, sale)
    assert (still["status"], still["position"]) == ("waiting", 1)
    assert (await buy(client, app, second, basket["id"]))["status"] == "paid"
    await assert_invariants(sessionmaker)


async def test_two_workers_promoting_at_once_make_one_offer(
    client: AsyncClient, app: FastAPI, sessionmaker: Sessions, settings: Settings
) -> None:
    sale = await open_sale(client, sessionmaker, teams=1)
    first, *waiting = await make_buyers(sessionmaker, settings, 7)
    team = await enter(client, app, first, sale, "Night Shift")
    for n, captain in enumerate(waiting):
        await wait(client, captain, sale, f"Waiting {n}")
    await client.delete(f"/teams/{team['id']}", headers=first)
    await settle(app)

    async def promote() -> int:
        async with sessionmaker.begin() as session:
            return await teams.promote_waitlist(session, settings)

    assert sum(await asyncio.gather(*(promote() for _ in range(6)))) == 1
    places = [await my_place(client, captain, sale) for captain in waiting]
    assert Counter(place["status"] for place in places) == {"offered": 1, "waiting": 5}
    assert places[0]["status"] == "offered"  # the one who has waited longest
    await assert_invariants(sessionmaker)


async def test_team_entry_closes_when_the_tournament_starts(
    client: AsyncClient, sessionmaker: Sessions, settings: Settings
) -> None:
    sale = await open_sale(client, sessionmaker, teams=1)
    captain, spectator = await make_buyers(sessionmaker, settings, 2)
    async with sessionmaker.begin() as session:
        await session.execute(
            text(
                "UPDATE tournaments SET starts_at = now() - interval '1 hour', "
                "ends_at = now() + interval '5 hours'"
            )
        )

    late = await register_team(client, captain, sale)
    assert (late.status_code, late.json()["code"]) == (409, "registration_closed")
    waiting = await wait(client, captain, sale, "Late")
    assert (waiting.status_code, waiting.json()["code"]) == (409, "registration_closed")
    # Spectators can still buy while the matches are on.
    assert (await hold(client, spectator, sale, general=1)).status_code == 201
    assert (await available(client, sale.slug))["competitor"] == 1
