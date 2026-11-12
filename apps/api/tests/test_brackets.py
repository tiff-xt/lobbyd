"""Bracket generation, results, and live updates across API instances."""

import asyncio
import json
import random
from collections import Counter
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any
from uuid import uuid4

import uvicorn
from fastapi import FastAPI
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from websockets.asyncio.client import ClientConnection, connect

from lobbyd.brackets.service import plan, round_name, seed_order
from lobbyd.config import Settings
from lobbyd.main import create_app
from lobbyd.platform import live
from lobbyd.platform.live import MAX_BACKLOG, Hub
from tests.helpers import Sale, hold, make_buyers, open_sale, work
from tests.invariants import assert_invariants
from tests.test_teams import enter

Sessions = async_sessionmaker[AsyncSession]
Json = dict[str, Any]


async def with_teams(
    client: AsyncClient, app: FastAPI, sessionmaker: Sessions, settings: Settings, count: int
) -> Sale:
    """A tournament with `count` confirmed teams, named T1, T2, ... in registration order."""
    sale = await open_sale(client, sessionmaker, teams=max(count, 2))
    captains = await make_buyers(sessionmaker, settings, count)
    for n, captain in enumerate(captains):
        await enter(client, app, captain, sale, f"T{n + 1}")
    return sale


async def generate(client: AsyncClient, sale: Sale) -> Any:
    return await client.post(f"/tournaments/{sale.slug}/bracket", headers=sale.organizer)


async def bracket(client: AsyncClient, sale: Sale) -> Json:
    body: Json = (await client.get(f"/tournaments/{sale.slug}/bracket")).json()
    return body


def pairings(round_: Json) -> list[tuple[str | None, str | None]]:
    return [
        (m["team_a"] and m["team_a"]["name"], m["team_b"] and m["team_b"]["name"])
        for m in round_["matches"]
    ]


async def report(
    client: AsyncClient, sale: Sale, match: Json, a: int, b: int, version: int | None = None
) -> Any:
    return await client.post(
        f"/matches/{match['id']}/result",
        json={
            "score_a": a,
            "score_b": b,
            "version": match["version"] if version is None else version,
        },
        headers=sale.organizer,
    )


# --- shape ----------------------------------------------------------------------


def test_seeding_keeps_the_top_seeds_apart() -> None:
    assert seed_order(2) == [1, 2]
    assert seed_order(8) == [1, 8, 4, 5, 2, 7, 3, 6]
    order = seed_order(64)
    assert sorted(order) == list(range(1, 65))
    # In every first-round match the seeds add up to 65: best plays worst.
    assert {order[i] + order[i + 1] for i in range(0, 64, 2)} == {65}
    assert [round_name(r, 5) for r in range(1, 6)] == [
        "Round of 32",
        "Round of 16",
        "Quarterfinals",
        "Semifinals",
        "Final",
    ]


async def test_five_teams_make_an_eight_place_bracket_with_byes_for_the_top_seeds(
    client: AsyncClient, app: FastAPI, sessionmaker: Sessions, settings: Settings
) -> None:
    sale = await with_teams(client, app, sessionmaker, settings, 5)
    assert (await client.get(f"/tournaments/{sale.slug}/bracket")).json()["code"] == (
        "bracket_not_found"
    )
    created = await generate(client, sale)
    assert created.status_code == 201, created.text
    quarters, semis, final = created.json()["rounds"]

    assert [r["name"] for r in created.json()["rounds"]] == ["Quarterfinals", "Semifinals", "Final"]
    assert pairings(quarters) == [("T1", None), ("T4", "T5"), ("T2", None), ("T3", None)]
    assert [m["status"] for m in quarters["matches"]] == [
        "completed",
        "scheduled",
        "completed",
        "completed",
    ]
    # Teams with a bye are already waiting in the next round.
    assert pairings(semis) == [("T1", None), ("T2", "T3")]
    assert pairings(final) == [(None, None)]
    assert created.json()["champion"] is None

    again = await generate(client, sale)
    assert (again.status_code, again.json()["code"]) == (409, "bracket_exists")
    await assert_invariants(sessionmaker)


async def test_who_can_draw_a_bracket_and_when(
    client: AsyncClient, app: FastAPI, sessionmaker: Sessions, settings: Settings
) -> None:
    sale = await with_teams(client, app, sessionmaker, settings, 1)
    (stranger,) = await make_buyers(sessionmaker, settings, 1)
    too_few = await generate(client, sale)
    assert (too_few.status_code, too_few.json()["code"]) == (409, "not_enough_teams")
    denied = await client.post(f"/tournaments/{sale.slug}/bracket", headers=stranger)
    assert denied.status_code == 403


async def test_two_organizers_drawing_at_once_make_one_bracket(
    client: AsyncClient, app: FastAPI, sessionmaker: Sessions, settings: Settings
) -> None:
    sale = await with_teams(client, app, sessionmaker, settings, 4)
    responses = await asyncio.gather(*(generate(client, sale) for _ in range(6)))
    assert Counter(r.status_code for r in responses) == {201: 1, 409: 5}
    assert sum(len(r["matches"]) for r in (await bracket(client, sale))["rounds"]) == 3
    await assert_invariants(sessionmaker)


# --- results --------------------------------------------------------------------


async def test_playing_a_bracket_through_to_a_champion(
    client: AsyncClient, app: FastAPI, sessionmaker: Sessions, settings: Settings
) -> None:
    sale = await with_teams(client, app, sessionmaker, settings, 4)
    semis, final = (await generate(client, sale)).json()["rounds"]
    assert pairings(semis) == [("T1", "T4"), ("T2", "T3")]
    top, bottom = semis["matches"]
    decider = final["matches"][0]

    not_ready = await report(client, sale, decider, 2, 0)
    assert (not_ready.status_code, not_ready.json()["code"]) == (409, "match_not_ready")
    draw = await report(client, sale, top, 1, 1)
    assert (draw.status_code, draw.json()["code"]) == (422, "draw_not_allowed")

    started = await client.post(
        f"/matches/{top['id']}/start", json={"version": top["version"]}, headers=sale.organizer
    )
    assert started.json()["status"] == "live"
    # The page this organizer has open is now out of date.
    stale = await report(client, sale, top, 2, 0)
    assert (stale.status_code, stale.json()["code"]) == (409, "stale_version")
    assert stale.json()["current_version"] == started.json()["version"]

    won = await report(client, sale, started.json(), 2, 1)
    assert won.status_code == 200, won.text
    assert (won.json()["status"], won.json()["winner_id"]) == ("completed", top["team_a"]["id"])
    await report(client, sale, bottom, 0, 2)
    final = (await bracket(client, sale))["rounds"][1]
    assert pairings(final) == [("T1", "T3")]

    crowned = await report(client, sale, final["matches"][0], 1, 3)
    assert crowned.status_code == 200, crowned.text
    assert (await bracket(client, sale))["champion"]["name"] == "T3"
    await assert_invariants(sessionmaker)


async def test_a_result_can_be_corrected_until_the_next_match_is_played(
    client: AsyncClient, app: FastAPI, sessionmaker: Sessions, settings: Settings
) -> None:
    sale = await with_teams(client, app, sessionmaker, settings, 4)
    semis, _ = (await generate(client, sale)).json()["rounds"]
    top, bottom = semis["matches"]

    entered = (await report(client, sale, top, 2, 0)).json()  # T1 beat T4
    assert pairings((await bracket(client, sale))["rounds"][1]) == [("T1", None)]
    # Typed the wrong way round. Correcting it changes who is in the final.
    fixed = await report(client, sale, entered, 0, 2)
    assert fixed.status_code == 200, fixed.text
    assert pairings((await bracket(client, sale))["rounds"][1]) == [("T4", None)]

    await report(client, sale, bottom, 2, 0)
    final = (await bracket(client, sale))["rounds"][1]["matches"][0]
    await report(client, sale, final, 3, 1)
    # The final has been played with T4 in it. That cannot be rewritten from here.
    locked = await report(client, sale, fixed.json(), 2, 0)
    assert (locked.status_code, locked.json()["code"]) == (409, "result_locked")
    after = await bracket(client, sale)
    assert after["rounds"][0]["matches"][0]["score_a"] == 0  # the refused edit left nothing behind
    assert after["champion"]["name"] == "T4"
    await assert_invariants(sessionmaker)


async def test_two_people_entering_the_same_result_at_once(
    client: AsyncClient, app: FastAPI, sessionmaker: Sessions, settings: Settings
) -> None:
    sale = await with_teams(client, app, sessionmaker, settings, 2)
    (final,) = (await generate(client, sale)).json()["rounds"][0]["matches"]
    # Eight tabs with the same match open disagree about the score.
    responses = await asyncio.gather(
        *(report(client, sale, final, n % 2 * 3, 1 + n % 2 * -1 + 1) for n in range(8))
    )
    assert Counter(r.status_code for r in responses) == {200: 1, 409: 7}
    assert {r.json()["code"] for r in responses if r.status_code == 409} == {"stale_version"}
    (winner,) = [r.json() for r in responses if r.status_code == 200]
    stored = (await bracket(client, sale))["rounds"][0]["matches"][0]
    assert (stored["score_a"], stored["score_b"], stored["version"]) == (
        winner["score_a"],
        winner["score_b"],
        final["version"] + 1,
    )


# --- double elimination ----------------------------------------------------------


def draw_double(client: AsyncClient, sale: Sale) -> Any:
    return client.post(
        f"/tournaments/{sale.slug}/bracket", json={"format": "double"}, headers=sale.organizer
    )


def find(bracket_: Json, section: str, round_name_: str) -> list[Json]:
    (found,) = [
        r["matches"]
        for r in bracket_["rounds"]
        if (r["bracket"], r["name"]) == (section, round_name_)
    ]
    matches: list[Json] = found
    return matches


async def test_a_team_is_out_after_its_second_loss_not_its_first(
    client: AsyncClient, app: FastAPI, sessionmaker: Sessions, settings: Settings
) -> None:
    sale = await with_teams(client, app, sessionmaker, settings, 4)
    drawn = await draw_double(client, sale)
    assert drawn.status_code == 201, drawn.text
    assert drawn.json()["format"] == "double"
    assert [(r["bracket"], r["name"]) for r in drawn.json()["rounds"]] == [
        ("winners", "Semifinals"),
        ("winners", "Winners final"),
        ("losers", "Losers round 1"),
        ("losers", "Losers final"),
        ("final", "Grand final"),  # the reset is not shown until it is needed
    ]

    top, bottom = find(drawn.json(), "winners", "Semifinals")  # T1 v T4, T2 v T3
    await report(client, sale, top, 2, 0)  # T4 loses once
    await report(client, sale, bottom, 2, 1)  # T3 loses once
    now = await bracket(client, sale)
    assert pairings({"matches": find(now, "winners", "Winners final")}) == [("T1", "T2")]
    # Losing did not send them home: they play each other for a second chance.
    assert pairings({"matches": find(now, "losers", "Losers round 1")}) == [("T4", "T3")]

    await report(client, sale, find(now, "losers", "Losers round 1")[0], 0, 2)  # T4 is out
    await report(client, sale, find(now, "winners", "Winners final")[0], 0, 2)  # T1 drops down
    now = await bracket(client, sale)
    assert pairings({"matches": find(now, "losers", "Losers final")}) == [("T3", "T1")]
    await report(client, sale, find(now, "losers", "Losers final")[0], 1, 2)  # T3 is out

    now = await bracket(client, sale)
    (grand,) = find(now, "final", "Grand final")
    assert pairings({"matches": [grand]}) == [("T2", "T1")]  # unbeaten T2 on side A
    assert now["champion"] is None

    # T1 beats T2. That is T2's first loss, so it is not over: they play again.
    first = (await report(client, sale, grand, 1, 2)).json()
    now = await bracket(client, sale)
    assert now["champion"] is None
    (reset,) = find(now, "final", "Grand final reset")
    assert pairings({"matches": [reset]}) == [("T2", "T1")]

    # Entered the wrong way round? Correcting it takes the reset away again...
    corrected = (await report(client, sale, first, 2, 1)).json()
    now = await bracket(client, sale)
    assert now["champion"]["name"] == "T2"
    assert [r["name"] for r in now["rounds"] if r["bracket"] == "final"] == ["Grand final"]
    # ...and putting it back brings it back.
    await report(client, sale, corrected, 1, 2)
    (reset,) = find(await bracket(client, sale), "final", "Grand final reset")
    await report(client, sale, reset, 0, 3)
    assert (await bracket(client, sale))["champion"]["name"] == "T1"

    # With the reset played, the first final can no longer be rewritten.
    grand = find(await bracket(client, sale), "final", "Grand final")[0]
    locked = await report(client, sale, grand, 5, 0)
    assert (locked.status_code, locked.json()["code"]) == (409, "result_locked")
    await assert_invariants(sessionmaker)


async def test_a_result_cannot_change_once_the_loser_has_played_on(
    client: AsyncClient, app: FastAPI, sessionmaker: Sessions, settings: Settings
) -> None:
    sale = await with_teams(client, app, sessionmaker, settings, 4)
    drawn = (await draw_double(client, sale)).json()
    top, bottom = find(drawn, "winners", "Semifinals")
    played = (await report(client, sale, top, 2, 0)).json()
    await report(client, sale, bottom, 2, 0)
    losers_match = find(await bracket(client, sale), "losers", "Losers round 1")[0]
    await report(client, sale, losers_match, 2, 0)

    # T4 has since played (and won) in the losers bracket on the strength of
    # this result. Flipping it now would put T1 there instead, after the fact.
    locked = await report(client, sale, played, 0, 2)
    assert (locked.status_code, locked.json()["code"]) == (409, "result_locked")
    assert (
        find(await bracket(client, sale), "winners", "Semifinals")[0]["winner_id"]
        == (top["team_a"]["id"])
    )
    await assert_invariants(sessionmaker)


def test_every_plan_gives_every_team_exactly_two_lives() -> None:
    """Play out brackets of every size with random results, in memory.

    Whatever happens, everyone but the champion must lose exactly twice, the
    champion at most once, and every match that exists must get played.
    """
    rng = random.Random(7)
    for teams in [*range(2, 18), 31, 32, 33, 64]:
        for _ in range(4):
            entrants = [uuid4() for _ in range(teams)]
            rows = {r["id"]: r for r in plan(uuid4(), entrants, double=True)}
            reset = next(r for r in rows.values() if (r["bracket"], r["round"]) == ("final", 2))
            losses: Counter[Any] = Counter()
            champion = None
            while True:
                ready = [
                    r
                    for r in rows.values()
                    if r["status"] != "completed" and r["team_a_id"] and r["team_b_id"]
                ]
                if not ready:
                    break
                match = rng.choice(ready)
                a_wins = rng.random() < 0.5
                winner = match["team_a_id"] if a_wins else match["team_b_id"]
                loser = match["team_b_id"] if a_wins else match["team_a_id"]
                match["status"], match["winner_id"] = "completed", winner
                losses[loser] += 1
                if match["bracket"] == "final":
                    if match["round"] == 1 and not a_wins:
                        reset["team_a_id"], reset["team_b_id"] = (
                            match["team_a_id"],
                            match["team_b_id"],
                        )
                    else:
                        champion = winner
                    continue
                if match["next_match_id"]:
                    rows[match["next_match_id"]][f"team_{match['next_slot']}_id"] = winner
                if match["loser_next_match_id"]:
                    target = rows[match["loser_next_match_id"]]
                    target[f"team_{match['loser_next_slot']}_id"] = loser

            assert champion is not None, teams
            assert losses[champion] <= 1, teams
            assert {team: losses[team] for team in entrants if team != champion} == {
                team: 2 for team in entrants if team != champion
            }, teams
            # Nothing was left unplayed except, sometimes, the reset.
            unplayed = [r for r in rows.values() if r["status"] != "completed"]
            assert unplayed in ([], [reset]), teams
            # No two matches claim the same side of the same match.
            feeds = Counter(
                (r[f"{kind}_match_id"], r[f"{kind}_slot"])
                for r in rows.values()
                for kind in ("next", "loser_next")
                if r[f"{kind}_match_id"]
            )
            assert max(feeds.values()) == 1, teams


async def test_double_elimination_with_byes_plays_through(
    client: AsyncClient, app: FastAPI, sessionmaker: Sessions, settings: Settings
) -> None:
    """Five teams, through the API: the same property, checked by the database rules."""
    sale = await with_teams(client, app, sessionmaker, settings, 5)
    assert (await draw_double(client, sale)).status_code == 201
    for _ in range(20):
        now = await bracket(client, sale)
        ready = [
            m
            for r in now["rounds"]
            for m in r["matches"]
            if m["status"] != "completed" and m["team_a"] and m["team_b"]
        ]
        if not ready:
            break
        # The lower side always wins: every upset the format allows.
        assert (await report(client, sale, ready[0], 0, 1)).status_code == 200
        await assert_invariants(sessionmaker)
    assert now["champion"] is not None
    played = sum(1 for r in now["rounds"] for m in r["matches"] if m["team_a"] and m["team_b"])
    assert played in (8, 9)  # 2n - 2 matches, or 2n - 1 with the reset


# --- live -----------------------------------------------------------------------


@asynccontextmanager
async def served(app: FastAPI) -> AsyncIterator[str]:
    """Run `app` on a real port in this event loop. Yields its ws:// address."""
    server = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=0, lifespan="off", log_level="warning")
    )
    task = asyncio.create_task(server.serve())
    try:
        while not server.started:  # noqa: ASYNC110
            await asyncio.sleep(0.01)
        port = server.servers[0].sockets[0].getsockname()[1]
        yield f"ws://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        await task


async def next_message(socket: ClientConnection, *, seats: bool = False) -> Json:
    """The next bracket message, or with `seats` the next seat-map one.

    Both kinds share the connection, so each test reads past the other's.
    """
    while True:
        message: Json = json.loads(await asyncio.wait_for(socket.recv(), timeout=5))
        if (message["type"] == "availability") == seats:
            return message


async def test_a_result_entered_on_one_instance_reaches_viewers_on_another(
    client: AsyncClient, app: FastAPI, sessionmaker: Sessions, settings: Settings
) -> None:
    sale = await with_teams(client, app, sessionmaker, settings, 4)
    # A second API process in all but name: its own database pool, its own
    # Redis connections, its own hub. Only Redis connects it to the first.
    second = create_app(settings, provider=app.state.provider)
    async with second.router.lifespan_context(second), served(second) as address:
        assert second.state.live is not app.state.live
        async with connect(f"{address}/tournaments/{sale.slug}/live") as viewer:
            assert await next_message(viewer) == {"type": "bracket", "bracket": None}

            # Everything below happens on the first instance (`client`).
            semis, _ = (await generate(client, sale)).json()["rounds"]
            await work(app)
            assert await next_message(viewer) == {"type": "reload"}

            won = (await report(client, sale, semis["matches"][0], 2, 0)).json()
            await work(app)
            update = await next_message(viewer)
            assert update["type"] == "matches"
            changed, following = update["matches"]
            assert changed == won
            assert (following["round"], following["team_a"]["name"]) == (2, "T1")

        # Someone who connects later starts from the current state.
        async with connect(f"{address}/tournaments/{sale.slug}/live") as latecomer:
            snapshot = await next_message(latecomer)
            assert snapshot["bracket"]["rounds"][0]["matches"][0] == won
        refused = await connect(f"{address}/tournaments/no-such-tournament/live")
        await refused.wait_closed()
        assert refused.close_code == 1008


async def test_seats_taken_and_freed_are_pushed_to_viewers(
    client: AsyncClient, app: FastAPI, sessionmaker: Sessions, settings: Settings
) -> None:
    sale = await open_sale(client, sessionmaker)
    first, second = await make_buyers(sessionmaker, settings, 2)
    async with served(app) as address, connect(f"{address}/tournaments/{sale.slug}/live") as viewer:
        assert (await next_message(viewer))["type"] == "bracket"

        held = (await hold(client, first, sale, seats=sale.seat_ids[:3], general=1)).json()
        # A refused hold changes nothing, so it announces nothing.
        assert (await hold(client, second, sale, seats=sale.seat_ids[2:5])).status_code == 409
        assert await next_message(viewer, seats=True) == {
            "type": "availability",
            "taken": sorted(sale.seat_ids[:3]),
            "freed": [],
        }

        released = await client.delete(f"/holds/{held['id']}", headers=first)
        assert released.status_code == 204
        assert await next_message(viewer, seats=True) == {
            "type": "availability",
            "taken": [],
            "freed": sorted(sale.seat_ids[:3]),
        }

        # Standing tickets have no seats to name: the message just says "look again".
        await hold(client, second, sale, general=2)
        nothing_named = {"type": "availability", "taken": [], "freed": []}
        assert await next_message(viewer, seats=True) == nothing_named


async def test_a_viewer_that_falls_behind_is_cut_loose(app: FastAPI) -> None:
    hub = Hub(app.state.redis)
    await hub.start()
    try:
        keeping_up, stuck = hub.subscribe("t"), hub.subscribe("t")
        received = []

        async def read() -> None:
            async for message in keeping_up:
                received.append(json.loads(message)["n"])

        reader = asyncio.create_task(read())
        for n in range(MAX_BACKLOG + 10):
            await live.publish(app.state.redis, "t", {"n": n})
            await asyncio.sleep(0)
        while len(received) < MAX_BACKLOG + 10:  # noqa: ASYNC110
            await asyncio.sleep(0.01)

        assert received == list(range(MAX_BACKLOG + 10))  # in order, none lost
        # The one that never read was dropped instead of holding memory or
        # slowing the others. Its stream just ends; the client reconnects.
        assert len([m async for m in stuck]) < MAX_BACKLOG
        assert hub.clients == 1
        keeping_up.close()
        await reader
    finally:
        await hub.stop()
