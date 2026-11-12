"""Shared test helpers."""

from datetime import UTC, datetime, timedelta
from typing import Any

from httpx import AsyncClient
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from lobbyd.identity.models import User

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
