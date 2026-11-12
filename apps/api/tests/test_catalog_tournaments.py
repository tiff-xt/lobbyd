import asyncio
from datetime import UTC, datetime, timedelta

from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tests.helpers import TEAM_SLOTS, auth_headers, published, tournament_body, venue_body

Sessions = async_sessionmaker[AsyncSession]


async def test_create_starts_as_a_draft_only_its_organizer_can_see(
    client: AsyncClient, sessionmaker: Sessions
) -> None:
    owner = await auth_headers(client, sessionmaker)
    other = await auth_headers(client, sessionmaker, "other@example.com")

    response = await client.post("/tournaments", json=tournament_body(), headers=owner)
    assert response.status_code == 201, response.text
    draft = response.json()
    assert draft["slug"] == "starfall-open"
    assert draft["status"] == "draft"
    assert draft["venue"] is None

    assert (await client.get("/tournaments/starfall-open", headers=owner)).status_code == 200
    # To everyone else a draft does not exist: 404, not 403.
    assert (await client.get("/tournaments/starfall-open")).status_code == 404
    assert (await client.get("/tournaments/starfall-open", headers=other)).status_code == 404
    patch = await client.patch("/tournaments/starfall-open", json={"title": "Mine"}, headers=other)
    assert patch.status_code == 404
    assert (await client.get("/tournaments")).json()["items"] == []

    mine = (await client.get("/users/me/tournaments", headers=owner)).json()
    assert [t["slug"] for t in mine] == ["starfall-open"]
    assert (await client.get("/users/me/tournaments", headers=other)).json() == []


async def test_only_organizers_create_tournaments(
    client: AsyncClient, sessionmaker: Sessions
) -> None:
    assert (await client.post("/tournaments", json=tournament_body())).status_code == 401
    user = await auth_headers(client, sessionmaker, "user@example.com", role="user")
    assert (
        await client.post("/tournaments", json=tournament_body(), headers=user)
    ).status_code == 403


async def test_slugs_are_unique_even_when_titles_collide_at_the_same_moment(
    client: AsyncClient, sessionmaker: Sessions
) -> None:
    headers = await auth_headers(client, sessionmaker)
    body = tournament_body("Über Cup: Finals!")
    responses = await asyncio.gather(
        *(client.post("/tournaments", json=body, headers=headers) for _ in range(3))
    )
    assert [r.status_code for r in responses] == [201, 201, 201]
    slugs = sorted(r.json()["slug"] for r in responses)
    assert len(set(slugs)) == 3
    assert "uber-cup-finals" in slugs
    assert all(slug.startswith("uber-cup-finals") for slug in slugs)


async def test_date_rules(client: AsyncClient, sessionmaker: Sessions) -> None:
    headers = await auth_headers(client, sessionmaker)
    start = datetime.now(UTC) + timedelta(days=10)

    ends_first = tournament_body(ends_at=(start - timedelta(days=30)).isoformat())
    sales_late = tournament_body(sales_open_at=(start + timedelta(days=60)).isoformat())
    naive = tournament_body(starts_at="2030-01-01T10:00:00")  # no timezone
    too_long = tournament_body(ends_at=(start + timedelta(days=40)).isoformat())
    for body in (ends_first, sales_late, naive, too_long):
        response = await client.post("/tournaments", json=body, headers=headers)
        assert response.status_code == 422, response.text

    await client.post("/tournaments", json=tournament_body(), headers=headers)
    # A patch that is fine on its own but contradicts the stored dates.
    response = await client.patch(
        "/tournaments/starfall-open",
        json={"ends_at": (datetime.now(UTC) + timedelta(days=1)).isoformat()},
        headers=headers,
    )
    assert response.status_code == 422
    assert response.json()["code"] == "invalid_dates"


async def test_a_venue_can_only_be_used_by_its_owner(
    client: AsyncClient, sessionmaker: Sessions
) -> None:
    owner = await auth_headers(client, sessionmaker)
    other = await auth_headers(client, sessionmaker, "other@example.com")
    venue = (await client.post("/venues", json=venue_body(), headers=owner)).json()

    body = tournament_body(venue_id=venue["id"])
    refused = await client.post("/tournaments", json=body, headers=other)
    assert refused.status_code == 404
    assert refused.json()["code"] == "venue_not_found"

    created = await client.post("/tournaments", json=body, headers=owner)
    assert created.status_code == 201
    assert created.json()["venue"] == {
        "id": venue["id"],
        "name": "Test Hall",
        "city": "Berlin",
        "timezone": "Europe/Berlin",
    }


# --- ticket types ------------------------------------------------------------


async def test_ticket_types_of_each_kind(client: AsyncClient, sessionmaker: Sessions) -> None:
    headers = await auth_headers(client, sessionmaker)
    venue = (await client.post("/venues", json=venue_body(3, 2, 5), headers=headers)).json()
    sections = [s["id"] for s in venue["sections"]]
    await client.post("/tournaments", json=tournament_body(venue_id=venue["id"]), headers=headers)
    url = "/tournaments/starfall-open/ticket-types"

    assert (await client.post(url, json=TEAM_SLOTS, headers=headers)).status_code == 201
    general = {"kind": "general", "name": "Standing", "price_cents": 1500, "capacity": 200}
    assert (await client.post(url, json=general, headers=headers)).status_code == 201
    reserved = {
        "kind": "reserved",
        "name": "Premium",
        "price_cents": 6000,
        "section_ids": sections[:2],
    }
    response = await client.post(url, json=reserved, headers=headers)
    assert response.status_code == 201, response.text

    types = {t["name"]: t for t in response.json()["ticket_types"]}
    assert types["Team entry"]["capacity"] == 16
    assert types["Standing"]["capacity"] == 200
    # Capacity of reserved seating is the seats in its sections: 2 x (2 rows x 5).
    assert types["Premium"]["capacity"] == 20
    assert sorted(types["Premium"]["section_ids"]) == sorted(sections[:2])


async def test_ticket_type_rules(client: AsyncClient, sessionmaker: Sessions) -> None:
    headers = await auth_headers(client, sessionmaker)
    venue = (await client.post("/venues", json=venue_body(2), headers=headers)).json()
    elsewhere = (
        await client.post("/venues", json=venue_body(1, name="Elsewhere"), headers=headers)
    ).json()
    section, other_section = (s["id"] for s in venue["sections"])
    await client.post("/tournaments", json=tournament_body(venue_id=venue["id"]), headers=headers)
    url = "/tournaments/starfall-open/ticket-types"

    async def add(**body: object) -> tuple[int, str]:
        response = await client.post(url, json=body, headers=headers)
        return response.status_code, response.json().get("code", "")

    def reserved(name: str, *section_ids: str) -> dict[str, object]:
        return {"kind": "reserved", "name": name, "price_cents": 1, "section_ids": [*section_ids]}

    assert (await add(**TEAM_SLOTS))[0] == 201
    assert (await add(**reserved("Premium", section)))[0] == 201

    # One set of team slots per tournament.
    assert await add(**{**TEAM_SLOTS, "name": "More teams"}) == (409, "competitor_type_exists")
    # Names are unique within a tournament.
    assert await add(kind="general", name="Premium", price_cents=1, capacity=5) == (
        409,
        "ticket_type_name_taken",
    )
    # A section has one price: it cannot be sold under two types.
    assert await add(**reserved("Also premium", section, other_section)) == (
        409,
        "section_already_assigned",
    )
    # Sections must belong to this tournament's venue.
    assert await add(**reserved("Wrong venue", elsewhere["sections"][0]["id"])) == (
        422,
        "unknown_section",
    )
    # Shape rules.
    for bad in (
        {"kind": "reserved", "name": "x", "price_cents": 1},
        {
            "kind": "reserved",
            "name": "x",
            "price_cents": 1,
            "section_ids": [section],
            "capacity": 5,
        },
        {"kind": "general", "name": "x", "price_cents": 1},
        {"kind": "general", "name": "x", "price_cents": 1, "capacity": 5, "section_ids": [section]},
        {"kind": "general", "name": "x", "price_cents": -1, "capacity": 5},
        {"kind": "vip", "name": "x", "price_cents": 1, "capacity": 5},
    ):
        assert await add(**bad) == (422, "validation_error"), bad

    # A failed attempt left nothing half-created behind.
    detail = (await client.get("/tournaments/starfall-open", headers=headers)).json()
    assert sorted(t["name"] for t in detail["ticket_types"]) == ["Premium", "Team entry"]
    # The section freed up by nothing is still free.
    assert (await add(**reserved("Standard", other_section)))[0] == 201


async def test_reserved_seating_needs_a_venue(client: AsyncClient, sessionmaker: Sessions) -> None:
    headers = await auth_headers(client, sessionmaker)
    venue = (await client.post("/venues", json=venue_body(), headers=headers)).json()
    await client.post("/tournaments", json=tournament_body(), headers=headers)  # online only
    response = await client.post(
        "/tournaments/starfall-open/ticket-types",
        json={
            "kind": "reserved",
            "name": "Seats",
            "price_cents": 1,
            "section_ids": [venue["sections"][0]["id"]],
        },
        headers=headers,
    )
    assert response.status_code == 422
    assert response.json()["code"] == "venue_required"


async def test_two_simultaneous_team_slot_types_create_exactly_one(
    client: AsyncClient, sessionmaker: Sessions
) -> None:
    headers = await auth_headers(client, sessionmaker)
    await client.post("/tournaments", json=tournament_body(), headers=headers)
    url = "/tournaments/starfall-open/ticket-types"
    responses = await asyncio.gather(
        *(
            client.post(url, json={**TEAM_SLOTS, "name": f"Teams {n}"}, headers=headers)
            for n in range(6)
        )
    )
    assert sorted(r.status_code for r in responses) == [201] + [409] * 5


async def test_remove_ticket_type(client: AsyncClient, sessionmaker: Sessions) -> None:
    headers = await auth_headers(client, sessionmaker)
    await client.post("/tournaments", json=tournament_body(), headers=headers)
    url = "/tournaments/starfall-open/ticket-types"
    added = (await client.post(url, json=TEAM_SLOTS, headers=headers)).json()
    type_id = added["ticket_types"][0]["id"]

    assert (await client.delete(f"{url}/{type_id}", headers=headers)).status_code == 204
    assert (await client.delete(f"{url}/{type_id}", headers=headers)).status_code == 404
    detail = (await client.get("/tournaments/starfall-open", headers=headers)).json()
    assert detail["ticket_types"] == []


# --- publishing --------------------------------------------------------------


async def test_publish_needs_something_to_sell(client: AsyncClient, sessionmaker: Sessions) -> None:
    headers = await auth_headers(client, sessionmaker)
    await client.post("/tournaments", json=tournament_body(), headers=headers)
    response = await client.post("/tournaments/starfall-open/publish", headers=headers)
    assert response.status_code == 409
    assert response.json()["code"] == "tournament_not_publishable"


async def test_publishing_makes_it_public_and_freezes_the_facts(
    client: AsyncClient, sessionmaker: Sessions
) -> None:
    owner = await auth_headers(client, sessionmaker)
    other = await auth_headers(client, sessionmaker, "other@example.com")
    tournament = await published(client, owner)
    assert tournament["status"] == "published"
    slug = tournament["slug"]

    public = await client.get(f"/tournaments/{slug}")
    assert public.status_code == 200
    assert public.json()["ticket_types"][0]["name"] == "Team entry"
    assert [t["slug"] for t in (await client.get("/tournaments")).json()["items"]] == [slug]

    # Wording can still be fixed.
    renamed = await client.patch(f"/tournaments/{slug}", json={"title": "Renamed"}, headers=owner)
    assert renamed.status_code == 200
    assert renamed.json()["title"] == "Renamed"
    assert renamed.json()["slug"] == slug  # links already shared keep working

    # Facts people plan around cannot change.
    later = (datetime.now(UTC) + timedelta(days=90)).isoformat()
    type_id = tournament["ticket_types"][0]["id"]
    for response in (
        await client.patch(f"/tournaments/{slug}", json={"team_size": 3}, headers=owner),
        await client.patch(f"/tournaments/{slug}", json={"starts_at": later}, headers=owner),
        await client.post(
            f"/tournaments/{slug}/ticket-types",
            json={"kind": "general", "name": "Late", "price_cents": 1, "capacity": 5},
            headers=owner,
        ),
        await client.delete(f"/tournaments/{slug}/ticket-types/{type_id}", headers=owner),
        await client.post(f"/tournaments/{slug}/publish", headers=owner),
    ):
        assert response.status_code == 409, response.text
        assert response.json()["code"] == "tournament_not_editable"

    # Another organizer can see it but not touch it.
    refused = await client.patch(f"/tournaments/{slug}", json={"title": "Hijack"}, headers=other)
    assert refused.status_code == 403


async def test_concurrent_publishes_succeed_once(
    client: AsyncClient, sessionmaker: Sessions
) -> None:
    headers = await auth_headers(client, sessionmaker)
    await client.post("/tournaments", json=tournament_body(), headers=headers)
    await client.post("/tournaments/starfall-open/ticket-types", json=TEAM_SLOTS, headers=headers)
    responses = await asyncio.gather(
        *(client.post("/tournaments/starfall-open/publish", headers=headers) for _ in range(5))
    )
    assert sorted(r.status_code for r in responses) == [200] + [409] * 4
