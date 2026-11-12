"""Fill a development database with realistic data: `lobbyd-seed`.

Creates an organizer account, three venues (one 12,000 seat arena), and a set
of tournaments with ticket types. Game and event names are invented.
Deterministic for a given --seed, so query plans and benchmarks are repeatable.
"""

import argparse
import asyncio
import random
import sys
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import insert, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from lobbyd.catalog import service as catalog
from lobbyd.catalog.models import TicketType, TicketTypeSection, Tournament
from lobbyd.catalog.schemas import RowIn, SectionIn, VenueCreate, VenueOut
from lobbyd.catalog.slug import slugify
from lobbyd.config import get_settings
from lobbyd.identity import service as identity
from lobbyd.identity.models import User
from lobbyd.platform.db import Base, create_engine, create_sessionmaker
from lobbyd.platform.ids import uuid7
from lobbyd.platform.models import import_all_models

ORGANIZER_EMAIL = "organizer@example.com"
ORGANIZER_PASSWORD = "organizer-demo-password"  # noqa: S105 - local demo data only

GAMES = [
    ("Starfall Tactics", 5),
    ("Rift Runners", 3),
    ("Neon Circuit", 1),
    ("Ironclad Arena", 5),
    ("Hexbound", 2),
    ("Sky Raiders", 4),
    ("Pixel Brawl", 1),
    ("Voidstrike", 5),
]
EVENTS = ["Open", "Invitational", "Masters", "Showdown", "Cup", "Clash", "Championship", "LAN"]
SEASONS = ["Spring", "Summer", "Autumn", "Winter"]
CITIES = ["Berlin", "Austin", "Seoul", "Toronto", "Lisbon", "Osaka", "Denver", "Stockholm"]


def _rows(count: int, seats: int) -> list[RowIn]:
    return [RowIn(label=str(n), seats=seats) for n in range(1, count + 1)]


VENUES = [
    # 24 sections x 20 rows x 25 seats = 12,000
    VenueCreate(
        name="Nightfall Arena",
        city="Berlin",
        timezone="Europe/Berlin",
        sections=[
            SectionIn(name=f"{tier} {n}", rows=_rows(20, 25))
            for tier in ("Floor", "Lower", "Upper")
            for n in range(1, 9)
        ],
    ),
    # 8 x 15 x 20 = 2,400
    VenueCreate(
        name="Foundry Hall",
        city="Austin",
        timezone="America/Chicago",
        sections=[SectionIn(name=f"Block {c}", rows=_rows(15, 20)) for c in "ABCDEFGH"],
    ),
    # 3 x 10 x 10 = 300
    VenueCreate(
        name="Respawn LAN Centre",
        city="Toronto",
        timezone="America/Toronto",
        sections=[SectionIn(name=name, rows=_rows(10, 10)) for name in ("Left", "Centre", "Right")],
    ),
]


async def _tournaments(
    session: AsyncSession, organizer: User, venues: list[VenueOut], count: int, rng: random.Random
) -> None:
    now = datetime.now(UTC).replace(minute=0, second=0, microsecond=0)
    tournaments: list[dict[str, Any]] = []
    ticket_types: list[dict[str, Any]] = []
    links: list[dict[str, Any]] = []

    for index in range(count):
        game, team_size = rng.choice(GAMES)
        venue = rng.choice([*venues, None])  # a quarter are online only
        title = (
            f"{game} {rng.choice(SEASONS)} {rng.choice(EVENTS)} "
            f"{venue.city if venue else rng.choice(CITIES)} #{index + 1}"
        )
        # Spread from a month ago to a year ahead, so "upcoming" filters have work to do.
        starts_at = now + timedelta(hours=rng.randint(-30 * 24, 365 * 24))
        tournament_id = uuid7()
        tournaments.append(
            {
                "id": tournament_id,
                "organizer_id": organizer.id,
                "venue_id": venue.id if venue else None,
                "slug": f"{slugify(title)}",
                "title": title,
                "game": game,
                "description": (
                    f"{team_size}v{team_size} {game} tournament. "
                    f"Double elimination bracket, prize pool, and a live final."
                ),
                "team_size": team_size,
                "currency": "EUR" if venue is None or venue.city == "Berlin" else "USD",
                "starts_at": starts_at,
                "ends_at": starts_at + timedelta(hours=rng.choice([8, 12, 36, 60])),
                "sales_open_at": starts_at - timedelta(days=rng.randint(14, 60)),
                "status": "draft" if rng.random() < 0.1 else "published",
            }
        )
        ticket_types.append(
            {
                "id": uuid7(),
                "tournament_id": tournament_id,
                "kind": "competitor",
                "name": "Team entry",
                "price_cents": rng.choice([0, 2500, 5000, 10000]),
                "capacity": rng.choice([8, 16, 32, 64]),
            }
        )
        if venue is None:
            continue
        # Front third of the sections is premium, the rest standard.
        split = max(1, len(venue.sections) // 3)
        for name, price, sections in (
            ("Premium", rng.choice([6000, 9000]), venue.sections[:split]),
            ("Standard", rng.choice([2500, 4000]), venue.sections[split:]),
        ):
            ticket_type_id = uuid7()
            ticket_types.append(
                {
                    "id": ticket_type_id,
                    "tournament_id": tournament_id,
                    "kind": "reserved",
                    "name": name,
                    "price_cents": price,
                    "capacity": sum(section.seat_count for section in sections),
                }
            )
            links.extend(
                {
                    "ticket_type_id": ticket_type_id,
                    "section_id": section.id,
                    "tournament_id": tournament_id,
                }
                for section in sections
            )

    for table, rows in (
        (Tournament, tournaments),
        (TicketType, ticket_types),
        (TicketTypeSection, links),
    ):
        for start in range(0, len(rows), 5000):
            await session.execute(insert(table), rows[start : start + 5000])


async def seed(*, tournaments: int, random_seed: int, reset: bool) -> None:
    settings = get_settings()
    if settings.environment == "production":
        sys.exit("Refusing to seed a production database.")

    engine = create_engine(settings)
    try:
        async with create_sessionmaker(engine).begin() as session:
            if reset:
                import_all_models()
                tables = ", ".join(f'"{t.name}"' for t in Base.metadata.sorted_tables)
                await session.execute(text(f"TRUNCATE {tables} CASCADE"))
            elif await session.scalar(select(User.id).where(User.email == ORGANIZER_EMAIL)):
                sys.exit("Already seeded. Run with --reset to wipe the database and start over.")

            organizer = await identity.create_user(
                session,
                email=ORGANIZER_EMAIL,
                display_name="Demo Organizer",
                password=ORGANIZER_PASSWORD,
            )
            organizer.role = "organizer"
            venues = [await catalog.create_venue(session, organizer, venue) for venue in VENUES]
            await _tournaments(session, organizer, venues, tournaments, random.Random(random_seed))  # noqa: S311
            # Fresh statistics, so the planner sees the data it is about to query.
            await session.execute(text("ANALYZE"))
    finally:
        await engine.dispose()

    seats = sum(venue.capacity for venue in venues)
    sys.stdout.write(
        f"Seeded {len(venues)} venues ({seats:,} seats) and {tournaments:,} tournaments.\n"
        f"Organizer login: {ORGANIZER_EMAIL} / {ORGANIZER_PASSWORD}\n"
    )


def main() -> None:
    parser = argparse.ArgumentParser(prog="lobbyd-seed", description=__doc__)
    parser.add_argument("--tournaments", type=int, default=60)
    parser.add_argument("--seed", type=int, default=1)
    parser.add_argument("--reset", action="store_true", help="Wipe ALL data first")
    args = parser.parse_args()
    asyncio.run(seed(tournaments=args.tournaments, random_seed=args.seed, reset=args.reset))


if __name__ == "__main__":
    main()
