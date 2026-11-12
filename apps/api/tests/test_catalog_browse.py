from datetime import UTC, datetime, timedelta

import pytest
from httpx import AsyncClient
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from lobbyd.catalog.models import Tournament
from lobbyd.platform.pagination import decode_cursor, encode_cursor
from tests.helpers import auth_headers, published, tournament_body

Sessions = async_sessionmaker[AsyncSession]


async def titles(client: AsyncClient, **params: object) -> list[str]:
    response = await client.get("/tournaments", params=params)  # type: ignore[arg-type]
    assert response.status_code == 200, response.text
    return [t["title"] for t in response.json()["items"]]


async def test_listing_is_soonest_first_and_hides_drafts_and_finished_events(
    client: AsyncClient, sessionmaker: Sessions
) -> None:
    headers = await auth_headers(client, sessionmaker)
    await published(client, headers, title="Later", days_ahead=20)
    await published(client, headers, title="Sooner", days_ahead=5)
    await published(client, headers, title="Finished", days_ahead=3)
    await client.post("/tournaments", json=tournament_body("Draft"), headers=headers)
    running = await published(client, headers, title="Running now", days_ahead=2)

    # Move two of them into the past: one finished, one started but still going.
    now = datetime.now(UTC)
    async with sessionmaker.begin() as session:
        for title, start, end in (
            ("Finished", now - timedelta(days=3), now - timedelta(days=2)),
            ("Running now", now - timedelta(hours=2), now + timedelta(hours=6)),
        ):
            await session.execute(
                update(Tournament)
                .where(Tournament.title == title)
                .values(sales_open_at=start - timedelta(days=9), starts_at=start, ends_at=end)
            )

    assert await titles(client) == ["Running now", "Sooner", "Later"]
    # An explicit `from` in the past brings history back.
    since = (now - timedelta(days=30)).isoformat()
    assert await titles(client, **{"from": since}) == [
        "Finished",
        "Running now",
        "Sooner",
        "Later",
    ]
    assert running["status"] == "published"


async def test_date_window(client: AsyncClient, sessionmaker: Sessions) -> None:
    headers = await auth_headers(client, sessionmaker)
    for title, days in (("Week 1", 7), ("Week 2", 14), ("Week 3", 21)):
        await published(client, headers, title=title, days_ahead=days)
    now = datetime.now(UTC)
    window = {
        "from": (now + timedelta(days=10)).isoformat(),
        "to": (now + timedelta(days=18)).isoformat(),
    }
    assert await titles(client, **window) == ["Week 2"]


async def test_pagination_visits_every_row_exactly_once(
    client: AsyncClient, sessionmaker: Sessions
) -> None:
    headers = await auth_headers(client, sessionmaker)
    for n in range(7):
        await published(client, headers, title=f"Event {n}", days_ahead=10 + n)
    # Give several the same start time: the id has to break the tie, or rows
    # at a page boundary would be skipped or repeated.
    same = datetime.now(UTC) + timedelta(days=12)
    async with sessionmaker.begin() as session:
        await session.execute(
            update(Tournament)
            .where(Tournament.title.in_(["Event 1", "Event 2", "Event 3", "Event 4"]))
            .values(starts_at=same, ends_at=same + timedelta(hours=5))
        )

    seen: list[str] = []
    cursor = None
    pages = 0
    while True:
        params = {"limit": 2, **({"cursor": cursor} if cursor else {})}
        page = (await client.get("/tournaments", params=params)).json()
        assert len(page["items"]) <= 2
        seen += [t["slug"] for t in page["items"]]
        pages += 1
        cursor = page["next_cursor"]
        if cursor is None:
            break

    assert pages == 4
    assert len(seen) == len(set(seen)) == 7
    everything = (await client.get("/tournaments", params={"limit": 100})).json()
    assert seen == [t["slug"] for t in everything["items"]]
    assert everything["next_cursor"] is None


async def test_a_row_inserted_mid_way_does_not_shift_later_pages(
    client: AsyncClient, sessionmaker: Sessions
) -> None:
    headers = await auth_headers(client, sessionmaker)
    for n in range(4):
        await published(client, headers, title=f"Event {n}", days_ahead=10 + n)

    first = (await client.get("/tournaments", params={"limit": 2})).json()
    # Someone publishes an earlier event while the reader is between pages.
    # With OFFSET this would push "Event 1" onto page two a second time.
    await published(client, headers, title="Squeezed in", days_ahead=1)
    second = (
        await client.get("/tournaments", params={"limit": 2, "cursor": first["next_cursor"]})
    ).json()

    assert [t["title"] for t in first["items"]] == ["Event 0", "Event 1"]
    assert [t["title"] for t in second["items"]] == ["Event 2", "Event 3"]


@pytest.mark.parametrize(
    "cursor",
    ["garbage", "!!!", encode_cursor("only-one-part"), encode_cursor("not-a-date", "not-a-uuid")],
)
async def test_bad_cursors_are_rejected_cleanly(client: AsyncClient, cursor: str) -> None:
    response = await client.get("/tournaments", params={"cursor": cursor})
    assert response.status_code == 422
    assert response.json()["code"] == "invalid_cursor"


async def test_limit_bounds(client: AsyncClient) -> None:
    for limit in (0, 101, -1):
        assert (await client.get("/tournaments", params={"limit": limit})).status_code == 422


def test_cursor_round_trip() -> None:
    cursor = encode_cursor("2030-01-01T00:00:00+00:00", "abc")
    assert "=" not in cursor  # safe to drop into a URL as is
    assert decode_cursor(cursor, 2) == ["2030-01-01T00:00:00+00:00", "abc"]


async def test_search(client: AsyncClient, sessionmaker: Sessions) -> None:
    headers = await auth_headers(client, sessionmaker)
    await published(
        client,
        headers,
        title="Winter Masters",
        game="Rift Runners",
        description="Three players per team, racing through rifts.",
        days_ahead=5,
    )
    await published(
        client,
        headers,
        title="Summer Open",
        game="Starfall Tactics",
        description="Bring your own keyboard.",
        days_ahead=6,
    )
    await published(client, headers, title="Rift Championship", game="Hexbound", days_ahead=7)

    assert await titles(client, q="masters") == ["Winter Masters"]
    assert await titles(client, q="starfall") == ["Summer Open"]  # matches the game
    assert await titles(client, q="keyboard") == ["Summer Open"]  # matches the description
    assert await titles(client, q="RACING") == ["Winter Masters"]  # stemming: racing ~ race
    assert await titles(client, q="rift") == ["Winter Masters", "Rift Championship"]
    assert await titles(client, q="rift -masters") == ["Rift Championship"]
    assert await titles(client, q='"summer open"') == ["Summer Open"]
    assert await titles(client, q="masters or championship") == [
        "Winter Masters",
        "Rift Championship",
    ]
    assert await titles(client, q="nothing-matches-this") == []

    # Search combines with the other filters.
    assert await titles(client, q="rift", game="hexbound") == ["Rift Championship"]


@pytest.mark.parametrize("q", ["'", '"', "a & | !", "((", "-", "%_\\", "x" * 100, ":*", "<->"])
async def test_search_input_is_never_an_error(client: AsyncClient, q: str) -> None:
    assert (await client.get("/tournaments", params={"q": q})).status_code == 200


async def test_game_filter_ignores_case_and_games_are_counted(
    client: AsyncClient, sessionmaker: Sessions
) -> None:
    headers = await auth_headers(client, sessionmaker)
    await published(client, headers, title="One", game="Hexbound", days_ahead=5)
    await published(client, headers, title="Two", game="hexbound", days_ahead=6)
    await published(client, headers, title="Three", game="Voidstrike", days_ahead=7)
    await client.post(
        "/tournaments", json=tournament_body("Draft", game="Pixel Brawl"), headers=headers
    )

    assert await titles(client, game="HEXBOUND") == ["One", "Two"]
    assert await titles(client, game="Hex") == []  # exact, not prefix

    games = (await client.get("/games")).json()
    assert [(g["game"].lower(), g["tournaments"]) for g in games] == [
        ("hexbound", 2),
        ("voidstrike", 1),
    ]
