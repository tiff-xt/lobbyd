import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tests.helpers import auth_headers, venue_body

Sessions = async_sessionmaker[AsyncSession]


async def test_create_venue_builds_the_whole_seat_map(
    client: AsyncClient, sessionmaker: Sessions
) -> None:
    headers = await auth_headers(client, sessionmaker)
    response = await client.post("/venues", json=venue_body(2, 3, 4), headers=headers)
    assert response.status_code == 201, response.text
    venue = response.json()
    assert venue["capacity"] == 24
    assert [s["name"] for s in venue["sections"]] == ["Section 1", "Section 2"]
    assert [s["seat_count"] for s in venue["sections"]] == [12, 12]

    # Anyone can read a venue and its layout.
    assert (await client.get(f"/venues/{venue['id']}")).json() == venue
    seat_map = (await client.get(f"/venues/{venue['id']}/seats")).json()
    assert [s["name"] for s in seat_map["sections"]] == ["Section 1", "Section 2"]
    first = seat_map["sections"][0]
    assert [row["label"] for row in first["rows"]] == ["1", "2", "3"]
    assert [seat["number"] for seat in first["rows"][0]["seats"]] == [1, 2, 3, 4]
    seat_ids = [seat["id"] for s in seat_map["sections"] for r in s["rows"] for seat in r["seats"]]
    assert len(set(seat_ids)) == 24


async def test_rows_keep_the_order_they_were_given_in(
    client: AsyncClient, sessionmaker: Sessions
) -> None:
    headers = await auth_headers(client, sessionmaker)
    body = venue_body(1)
    # Sorting these as text would give AA, B, C or 1, 10, 2.
    body["sections"][0]["rows"] = [{"label": label, "seats": 1} for label in ("C", "B", "AA", "10")]
    venue = (await client.post("/venues", json=body, headers=headers)).json()
    seat_map = (await client.get(f"/venues/{venue['id']}/seats")).json()
    assert [r["label"] for r in seat_map["sections"][0]["rows"]] == ["C", "B", "AA", "10"]


async def test_a_twelve_thousand_seat_arena(client: AsyncClient, sessionmaker: Sessions) -> None:
    headers = await auth_headers(client, sessionmaker)
    response = await client.post("/venues", json=venue_body(24, 20, 25), headers=headers)
    assert response.status_code == 201
    assert response.json()["capacity"] == 12_000

    seat_map = (await client.get(f"/venues/{response.json()['id']}/seats")).json()
    assert sum(len(r["seats"]) for s in seat_map["sections"] for r in s["rows"]) == 12_000


async def test_only_organizers_create_venues(client: AsyncClient, sessionmaker: Sessions) -> None:
    assert (await client.post("/venues", json=venue_body())).status_code == 401
    user = await auth_headers(client, sessionmaker, "user@example.com", role="user")
    assert (await client.post("/venues", json=venue_body(), headers=user)).status_code == 403


@pytest.mark.parametrize(
    "mutate",
    [
        lambda b: b.update(timezone="Mars/Olympus"),
        lambda b: b["sections"].append(b["sections"][0]),  # duplicate section name
        lambda b: b["sections"][0]["rows"].append(b["sections"][0]["rows"][0]),  # duplicate row
        lambda b: b["sections"][0]["rows"][0].update(seats=0),
        lambda b: b.update(sections=[]),
        lambda b: b.update(
            sections=[
                {"name": f"S{i}", "rows": [{"label": str(r), "seats": 500} for r in range(101)]}
                for i in range(2)
            ]
        ),  # 101,000 seats
    ],
    ids=["timezone", "dup-section", "dup-row", "empty-row", "no-sections", "too-many-seats"],
)
async def test_venue_validation(client: AsyncClient, sessionmaker: Sessions, mutate) -> None:  # type: ignore[no-untyped-def]
    headers = await auth_headers(client, sessionmaker)
    body = venue_body()
    mutate(body)
    response = await client.post("/venues", json=body, headers=headers)
    assert response.status_code == 422, response.text
    assert response.json()["code"] == "validation_error"


async def test_unknown_venue_is_404(client: AsyncClient) -> None:
    missing = "00000000-0000-7000-8000-000000000000"
    for path in (f"/venues/{missing}", f"/venues/{missing}/seats"):
        response = await client.get(path)
        assert response.status_code == 404
        assert response.json()["code"] == "venue_not_found"
