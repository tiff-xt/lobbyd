"""Ticket tokens and scanning at the door, online and offline."""

import asyncio
import base64
from collections import Counter
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

import pytest
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from fastapi import FastAPI
from httpx import AsyncClient
from pydantic import SecretStr
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from lobbyd.config import Settings
from lobbyd.tickets import tokens
from tests.helpers import Sale, buy, hold, make_buyers, open_sale, settle
from tests.invariants import assert_invariants
from tests.test_teams import enter

Sessions = async_sessionmaker[AsyncSession]
Headers = dict[str, str]


async def tickets_for(
    client: AsyncClient, app: FastAPI, buyer: Headers, sale: Sale, count: int
) -> list[str]:
    """Buy `count` standing tickets and return their tokens."""
    held = (await hold(client, buyer, sale, general=count)).json()
    order = await buy(client, app, buyer, held["id"])
    assert order["status"] == "paid"
    return [ticket["token"] for ticket in order["tickets"]]


async def scan(client: AsyncClient, sale: Sale, token: str, device: str = "door-1") -> Any:
    return await client.post(
        f"/checkin/{sale.slug}/scan",
        json={"token": token, "device_id": device},
        headers=sale.organizer,
    )


def offline(token: str, at: datetime) -> dict[str, str]:
    return {"id": str(uuid4()), "token": token, "scanned_at": at.isoformat()}


async def sync(client: AsyncClient, sale: Sale, device: str, scans: list[dict[str, str]]) -> Any:
    return await client.post(
        f"/checkin/{sale.slug}/scans",
        json={"device_id": device, "scans": scans},
        headers=sale.organizer,
    )


async def summary(client: AsyncClient, sale: Sale) -> Any:
    return (await client.get(f"/checkin/{sale.slug}/summary", headers=sale.organizer)).json()


def unb64(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


# --- tokens ---------------------------------------------------------------------


async def test_a_scanner_can_verify_a_ticket_with_only_the_public_key(
    client: AsyncClient, app: FastAPI, sessionmaker: Sessions, settings: Settings
) -> None:
    sale = await open_sale(client, sessionmaker)
    (buyer,) = await make_buyers(sessionmaker, settings, 1)
    (token,) = await tickets_for(client, app, buyer, sale, 1)

    manifest = (await client.get(f"/checkin/{sale.slug}/manifest", headers=sale.organizer)).json()
    assert [(t["ticket_type_name"], t["checked_in_at"]) for t in manifest["tickets"]] == [
        ("Standing", None)
    ]

    # What an offline scanner does, with nothing from this codebase.
    prefix, key_id, payload, signature = token.split(".")
    (key,) = [k for k in manifest["keys"] if k["key_id"] == key_id]
    public = Ed25519PublicKey.from_public_bytes(unb64(key["public_key"]))
    public.verify(unb64(signature), f"{prefix}.{key_id}.{payload}".encode())
    assert unb64(payload)[:16].hex() == manifest["tickets"][0]["id"].replace("-", "")

    # Changing which ticket it names breaks the signature.
    other = base64.urlsafe_b64encode(uuid4().bytes + unb64(payload)[16:]).rstrip(b"=").decode()
    with pytest.raises(InvalidSignature):
        public.verify(unb64(signature), f"{prefix}.{key_id}.{other}".encode())


async def test_what_the_door_refuses(
    client: AsyncClient, app: FastAPI, sessionmaker: Sessions, settings: Settings
) -> None:
    sale = await open_sale(client, sessionmaker)
    elsewhere = await open_sale(
        client, sessionmaker, organizer_email="other@example.com", title="Other Cup"
    )
    (buyer,) = await make_buyers(sessionmaker, settings, 1)
    (token,) = await tickets_for(client, app, buyer, sale, 1)
    (foreign,) = await tickets_for(client, app, buyer, elsewhere, 1)

    prefix, key_id, payload, signature = token.split(".")
    forged = f"{prefix}.{key_id}.{foreign.split('.')[2]}.{signature}"
    for bad in (forged, "not a ticket", token[:-4], f"lbd1.deadbeef.{payload}.{signature}"):
        refused = await scan(client, sale, bad)
        assert (refused.status_code, refused.json()["code"]) == (422, "invalid_ticket_token")

    wrong_event = await scan(client, sale, foreign)
    assert (wrong_event.status_code, wrong_event.json()["code"]) == (409, "wrong_tournament")

    # Only the people running this tournament can scan for it.
    for outsider in (buyer, elsewhere.organizer):
        response = await client.post(
            f"/checkin/{sale.slug}/scan", json={"token": token, "device_id": "x"}, headers=outsider
        )
        assert response.status_code == 403
        assert (
            await client.get(f"/checkin/{sale.slug}/manifest", headers=outsider)
        ).status_code == 403
    assert (await summary(client, sale))["checked_in"] == 0


# --- online ---------------------------------------------------------------------


async def test_a_ticket_admits_once(
    client: AsyncClient, app: FastAPI, sessionmaker: Sessions, settings: Settings
) -> None:
    sale = await open_sale(client, sessionmaker)
    (buyer,) = await make_buyers(sessionmaker, settings, 1)
    held = (await hold(client, buyer, sale, seats=sale.seat_ids[:1])).json()
    order = await buy(client, app, buyer, held["id"])
    token = order["tickets"][0]["token"]

    first = (await scan(client, sale, token)).json()
    assert first["result"] == "admitted"
    assert first["seat"] == order["tickets"][0]["seat"]  # so the door can point the way
    again = (await scan(client, sale, token, device="door-2")).json()
    assert (again["result"], again["checked_in_at"]) == ("duplicate", first["checked_in_at"])

    report = await summary(client, sale)
    assert (report["tickets"], report["checked_in"], report["duplicates"]) == (1, 1, 1)
    (flagged,) = report["flagged"]
    assert (flagged["device_id"], flagged["admitted_by"]) == ("door-2", "door-1")
    await assert_invariants(sessionmaker)


async def test_thirty_scanners_race_for_one_ticket(
    client: AsyncClient, app: FastAPI, sessionmaker: Sessions, settings: Settings
) -> None:
    sale = await open_sale(client, sessionmaker)
    (buyer,) = await make_buyers(sessionmaker, settings, 1)
    (token,) = await tickets_for(client, app, buyer, sale, 1)

    responses = await asyncio.gather(
        *(scan(client, sale, token, device=f"door-{n}") for n in range(30))
    )
    assert Counter(r.json()["result"] for r in responses) == {"admitted": 1, "duplicate": 29}
    await assert_invariants(sessionmaker)


async def test_a_cancelled_ticket_is_refused(
    client: AsyncClient, app: FastAPI, sessionmaker: Sessions, settings: Settings
) -> None:
    sale = await open_sale(client, sessionmaker)
    (captain,) = await make_buyers(sessionmaker, settings, 1)
    team = await enter(client, app, captain, sale, "Night Shift")
    (order,) = (await client.get("/users/me/orders", headers=captain)).json()
    await client.delete(f"/teams/{team['id']}", headers=captain)
    await settle(app)

    assert (await scan(client, sale, order["tickets"][0]["token"])).json()["result"] == "void"
    manifest = (await client.get(f"/checkin/{sale.slug}/manifest", headers=sale.organizer)).json()
    assert manifest["tickets"] == []  # an offline scanner refuses what is not listed


# --- offline ---------------------------------------------------------------------


async def test_an_offline_batch_syncs_and_can_be_sent_twice(
    client: AsyncClient, app: FastAPI, sessionmaker: Sessions, settings: Settings
) -> None:
    sale = await open_sale(client, sessionmaker)
    (buyer,) = await make_buyers(sessionmaker, settings, 1)
    first, second, third = await tickets_for(client, app, buyer, sale, 3)
    assert (await scan(client, sale, third)).json()["result"] == "admitted"  # before the outage
    async with sessionmaker.begin() as session:  # ...which was ten minutes ago
        await session.execute(
            text("UPDATE tickets SET checked_in_at = checked_in_at - interval '10 minutes'")
        )
        await session.execute(
            text("UPDATE checkin_scans SET scanned_at = scanned_at - interval '10 minutes'")
        )

    now = datetime.now(UTC)
    batch = [
        offline(first, now - timedelta(minutes=5)),
        offline(second, now - timedelta(minutes=4)),
        offline(first, now - timedelta(minutes=3)),  # the same code shown again
        offline(third, now - timedelta(minutes=2)),  # used before this scanner went offline
        offline("lbd1.junk.junk.junk", now - timedelta(minutes=1)),
    ]
    synced = await sync(client, sale, "door-1", batch)
    assert synced.status_code == 200, synced.text
    expected = ["admitted", "admitted", "duplicate", "duplicate", "rejected"]
    assert [s["result"] for s in synced.json()["scans"]] == expected

    # The connection dropped before the answer arrived, so the scanner sends it all again.
    again = await sync(client, sale, "door-1", batch)
    assert again.json() == synced.json()
    async with sessionmaker() as session:
        assert await session.scalar(text("SELECT count(*) FROM checkin_scans")) == 5  # 1 + 4
    report = await summary(client, sale)
    assert (report["checked_in"], report["duplicates"]) == (3, 2)
    await assert_invariants(sessionmaker)


async def test_the_earliest_scan_wins_whatever_order_scans_arrive_in(
    client: AsyncClient, app: FastAPI, sessionmaker: Sessions, settings: Settings
) -> None:
    """One copied QR code, shown at two doors while both were offline."""
    sale = await open_sale(client, sessionmaker)
    (buyer,) = await make_buyers(sessionmaker, settings, 1)
    (token,) = await tickets_for(client, app, buyer, sale, 1)
    now = datetime.now(UTC)
    earlier = offline(token, now - timedelta(minutes=9))
    later = offline(token, now - timedelta(minutes=6))

    # The door that scanned second gets its connection back first.
    assert (await sync(client, sale, "door-2", [later])).json()["scans"][0]["result"] == "admitted"
    assert (await sync(client, sale, "door-1", [earlier])).json()["scans"][0]["result"] == (
        "admitted"
    )

    report = await summary(client, sale)
    assert (report["checked_in"], report["duplicates"]) == (1, 1)
    (flagged,) = report["flagged"]
    # The record now says what happened: door-1 let them in, door-2 saw a copy.
    assert (flagged["device_id"], flagged["admitted_by"]) == ("door-2", "door-1")
    assert flagged["offline"] is True
    # Asked again, door-2 hears how its scan ended up.
    assert (await sync(client, sale, "door-2", [later])).json()["scans"][0]["result"] == "duplicate"
    await assert_invariants(sessionmaker)


async def test_a_clock_that_cannot_be_right_is_flagged_and_not_trusted(
    client: AsyncClient, app: FastAPI, sessionmaker: Sessions, settings: Settings
) -> None:
    sale = await open_sale(client, sessionmaker)
    (buyer,) = await make_buyers(sessionmaker, settings, 1)
    (token,) = await tickets_for(client, app, buyer, sale, 1)
    now = datetime.now(UTC)

    honest = await sync(client, sale, "door-1", [offline(token, now - timedelta(minutes=3))])
    assert honest.json()["scans"][0] == {
        "id": honest.json()["scans"][0]["id"],
        "result": "admitted",
        "clock_suspect": False,
    }
    # A phone set to last year claims it scanned first. It does not get to
    # rewrite who was admitted.
    wrong = await sync(client, sale, "door-2", [offline(token, now - timedelta(days=400))])
    assert (wrong.json()["scans"][0]["result"], wrong.json()["scans"][0]["clock_suspect"]) == (
        "duplicate",
        True,
    )
    ahead = await sync(client, sale, "door-3", [offline(token, now + timedelta(hours=3))])
    assert ahead.json()["scans"][0]["clock_suspect"] is True
    assert [f["clock_suspect"] for f in (await summary(client, sale))["flagged"]] == [True, True]
    await assert_invariants(sessionmaker)


async def test_doors_syncing_the_same_tickets_at_once(
    client: AsyncClient, app: FastAPI, sessionmaker: Sessions, settings: Settings
) -> None:
    sale = await open_sale(client, sessionmaker, general=20)
    buyers = await make_buyers(sessionmaker, settings, 3)
    tokens = [t for b in buyers for t in await tickets_for(client, app, b, sale, 6)]
    now = datetime.now(UTC)

    def batch(door: int) -> list[dict[str, str]]:
        # Every door saw every ticket, each in its own order.
        order = tokens[door:] + tokens[:door]
        return [
            offline(token, now - timedelta(minutes=30) + timedelta(seconds=7 * n + door))
            for n, token in enumerate(order)
        ]

    responses = await asyncio.gather(
        *(sync(client, sale, f"door-{door}", batch(door)) for door in range(6))
    )
    assert {r.status_code for r in responses} == {200}  # nobody deadlocked
    report = await summary(client, sale)
    assert (report["checked_in"], report["duplicates"]) == (18, 18 * 5)
    await assert_invariants(sessionmaker)


def test_a_signing_key_is_accepted_in_either_base64_alphabet() -> None:
    seed = bytes(range(250, 256)) * 5 + b"\xfb\xff"  # encodes with +, /, and padding
    standard = base64.b64encode(seed).decode()
    urlsafe = base64.urlsafe_b64encode(seed).rstrip(b"=").decode()
    assert "+" in standard or "/" in standard
    keys = []
    for encoded in (standard, urlsafe):
        tokens.configure(Settings(ticket_signing_key=SecretStr(encoded)))
        keys.append(tokens.signer().public_key)
    assert keys[0] == keys[1]
    with pytest.raises(ValueError, match="32 bytes"):
        tokens.configure(Settings(ticket_signing_key=SecretStr("dG9vIHNob3J0")))
    tokens.configure(Settings())  # back to the test key
