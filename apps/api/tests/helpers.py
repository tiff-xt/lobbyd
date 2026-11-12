"""Shared test helpers."""

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

from fastapi import FastAPI
from httpx import AsyncClient
from sqlalchemy import insert, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from lobbyd.config import Settings
from lobbyd.identity import security
from lobbyd.identity.models import User
from lobbyd.platform.events import EventContext, consume_once
from lobbyd.platform.ids import uuid7
from lobbyd.platform.outbox import relay_once

PASSWORD = "correct horse battery"

Json = dict[str, Any]


async def auth_headers(
    client: AsyncClient,
    sessionmaker: async_sessionmaker[AsyncSession],
    email: str = "organizer@example.com",
    role: str = "organizer",
) -> dict[str, str]:
    """Create an account with the given role and return its Authorization header."""
    body = {"email": email, "display_name": email.split("@")[0], "password": PASSWORD}
    assert (await client.post("/auth/signup", json=body)).status_code == 201
    async with sessionmaker.begin() as session:
        await session.execute(update(User).where(User.email == email).values(role=role))
    tokens = (await client.post("/auth/login", json={"email": email, "password": PASSWORD})).json()
    return {"Authorization": f"Bearer {tokens['access_token']}"}


def venue_body(sections: int = 2, rows: int = 3, seats: int = 4, name: str = "Test Hall") -> Json:
    return {
        "name": name,
        "city": "Berlin",
        "timezone": "Europe/Berlin",
        "sections": [
            {
                "name": f"Section {s + 1}",
                "rows": [{"label": str(r + 1), "seats": seats} for r in range(rows)],
            }
            for s in range(sections)
        ],
    }


def tournament_body(title: str = "Starfall Open", days_ahead: float = 30, **overrides: Any) -> Json:
    starts_at = datetime.now(UTC) + timedelta(days=days_ahead)
    return {
        "title": title,
        "game": "Starfall Tactics",
        "description": "Five a side.",
        "team_size": 5,
        "currency": "EUR",
        "starts_at": starts_at.isoformat(),
        "ends_at": (starts_at + timedelta(hours=10)).isoformat(),
        "sales_open_at": (starts_at - timedelta(days=45)).isoformat(),
        **overrides,
    }


TEAM_SLOTS: Json = {"kind": "competitor", "name": "Team entry", "price_cents": 5000, "capacity": 16}


async def published(client: AsyncClient, headers: dict[str, str], **kwargs: Any) -> Json:
    """Create a tournament with team slots and publish it."""
    created = await client.post("/tournaments", json=tournament_body(**kwargs), headers=headers)
    assert created.status_code == 201, created.text
    slug = created.json()["slug"]
    added = await client.post(f"/tournaments/{slug}/ticket-types", json=TEAM_SLOTS, headers=headers)
    assert added.status_code == 201, added.text
    response = await client.post(f"/tournaments/{slug}/publish", headers=headers)
    assert response.status_code == 200, response.text
    body: Json = response.json()
    return body


# --- booking -------------------------------------------------------------------

Sessions = async_sessionmaker[AsyncSession]


async def make_buyers(
    sessionmaker: Sessions, settings: Settings, count: int
) -> list[dict[str, str]]:
    """Many accounts at once, as Authorization headers.

    Inserted directly with tokens minted here: signing up hundreds of users
    through the API would spend the whole test hashing passwords.
    """
    ids = [uuid7() for _ in range(count)]
    async with sessionmaker.begin() as session:
        await session.execute(
            insert(User),
            [
                {
                    "id": user_id,
                    "email": f"buyer-{user_id}@example.com",
                    "display_name": "Buyer",
                    "password_hash": "!",
                    "role": "user",
                }
                for user_id in ids
            ],
        )
    return [
        {"Authorization": f"Bearer {security.create_access_token(settings, user_id, 'user')}"}
        for user_id in ids
    ]


def key() -> dict[str, str]:
    """A fresh Idempotency-Key header."""
    return {"Idempotency-Key": str(uuid4())}


class Sale:
    """A published tournament with reserved seats, general admission, and team slots."""

    def __init__(self, slug: str, seat_ids: list[str], types: dict[str, str]) -> None:
        self.slug = slug
        self.seat_ids = seat_ids  # in section, row, seat order
        self.types = types  # name -> ticket type id

    @property
    def general(self) -> str:
        return self.types["Standing"]

    @property
    def team(self) -> str:
        return self.types["Team entry"]


async def open_sale(
    client: AsyncClient,
    sessionmaker: Sessions,
    *,
    seats: int = 40,
    general: int = 20,
    teams: int = 4,
    team_price: int = 5000,
    organizer_email: str = "organizer@example.com",
    title: str = "Starfall Open",
) -> Sale:
    headers = await auth_headers(client, sessionmaker, organizer_email)
    venue = (
        await client.post("/venues", json=venue_body(1, 1, seats, name=title), headers=headers)
    ).json()
    created = await client.post(
        "/tournaments", json=tournament_body(title, venue_id=venue["id"]), headers=headers
    )
    slug = created.json()["slug"]
    for ticket_type in (
        {
            "kind": "reserved",
            "name": "Seated",
            "price_cents": 4000,
            "section_ids": [venue["sections"][0]["id"]],
        },
        {"kind": "general", "name": "Standing", "price_cents": 1500, "capacity": general},
        {"kind": "competitor", "name": "Team entry", "price_cents": team_price, "capacity": teams},
    ):
        added = await client.post(
            f"/tournaments/{slug}/ticket-types", json=ticket_type, headers=headers
        )
        assert added.status_code == 201, added.text
    published_ = await client.post(f"/tournaments/{slug}/publish", headers=headers)
    assert published_.status_code == 200, published_.text

    seat_map = (await client.get(f"/venues/{venue['id']}/seats")).json()
    seat_ids = [s["id"] for s in seat_map["sections"][0]["rows"][0]["seats"]]
    types = {t["name"]: t["id"] for t in published_.json()["ticket_types"]}
    return Sale(slug, seat_ids, types)


async def hold(
    client: AsyncClient,
    buyer: dict[str, str],
    sale: Sale,
    *,
    seats: list[str] | None = None,
    general: int = 0,
    team: bool = False,
    idempotency: dict[str, str] | None = None,
) -> Any:
    items = []
    if general:
        items.append({"ticket_type_id": sale.general, "quantity": general})
    if team:
        items.append({"ticket_type_id": sale.team, "quantity": 1})
    return await client.post(
        "/holds",
        json={"tournament": sale.slug, "seat_ids": seats or [], "items": items},
        headers={**buyer, **(idempotency or key())},
    )


async def checkout(
    client: AsyncClient,
    buyer: dict[str, str],
    hold_id: str,
    idempotency: dict[str, str] | None = None,
    method: str = "pm_success",
) -> Any:
    """Start checkout. The order comes back pending; see settle()."""
    return await client.post(
        "/orders",
        json={"hold_id": hold_id, "payment_method": method},
        headers={**buyer, **(idempotency or key())},
    )


async def work(app: FastAPI) -> int:
    """One pass of what the workers do: relay the outbox, handle events."""
    events: EventContext = app.state.events
    return await relay_once(events.sessionmaker, events.redis) + await consume_once(events)


async def settle(app: FastAPI, *, deliver: bool = True) -> None:
    """Run all background work to completion.

    Loops over the outbox relay, the event handlers, and (unless `deliver` is
    False) the simulator's pending webhooks until a full pass does nothing.
    With deliver=False, payments get created at the provider but no news about
    them comes back yet, which lets a test do something in between.
    """
    for _ in range(50):
        done = await work(app)
        if deliver:
            done += await app.state.simulator.deliver_due(everything=True)
        if done == 0:
            return
    raise AssertionError("background work did not settle")


async def buy(
    client: AsyncClient,
    app: FastAPI,
    buyer: dict[str, str],
    hold_id: str,
    method: str = "pm_success",
) -> Json:
    """Check out, let the payment finish, and return the order as it ended up."""
    placed = await checkout(client, buyer, hold_id, method=method)
    assert placed.status_code == 201, placed.text
    await settle(app)
    order: Json = (await client.get(f"/orders/{placed.json()['id']}", headers=buyer)).json()
    return order
