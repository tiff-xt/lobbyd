"""Bracket generation, results, and live updates across API instances."""

import asyncio
import json
from collections import Counter
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import uvicorn
from fastapi import FastAPI
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from websockets.asyncio.client import ClientConnection, connect

from lobbyd.brackets.service import round_name, seed_order
from lobbyd.config import Settings
from lobbyd.main import create_app
from lobbyd.platform import live
from lobbyd.platform.live import MAX_BACKLOG, Hub
from tests.helpers import Sale, make_buyers, open_sale, work
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


async def next_message(socket: ClientConnection) -> Json:
    message: Json = json.loads(await asyncio.wait_for(socket.recv(), timeout=5))
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
