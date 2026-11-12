"""Set up a sale for a load test: `python bench/prepare.py --seats 2000 --buyers 1500`.

Wipes the database, creates one published tournament with reserved seats and
general admission, and a crowd of buyers. Writes what the load generator needs
(the tournament, its seats row by row, and a token per buyer) to a JSON file.
"""

import argparse
import asyncio
import json
import sys
from datetime import UTC, datetime, timedelta

from sqlalchemy import insert, text

from lobbyd.catalog import service as catalog
from lobbyd.catalog.schemas import (
    RowIn,
    SectionIn,
    TicketTypeCreate,
    TournamentCreate,
    VenueCreate,
)
from lobbyd.config import get_settings
from lobbyd.identity import security
from lobbyd.identity.models import User
from lobbyd.platform.cache import create_redis
from lobbyd.platform.db import Base, create_engine, create_sessionmaker
from lobbyd.platform.ids import uuid7
from lobbyd.platform.models import import_all_models
from lobbyd.wiring import wire_modules

ROWS, SEATS_PER_ROW = 20, 25  # 500 seats to a section


async def prepare(*, seats: int, general: int, buyers: int, waiting_room: bool, out: str) -> None:
    settings = get_settings()
    if settings.environment == "production":
        sys.exit("Refusing to wipe a production database.")
    wire_modules()
    import_all_models()
    engine = create_engine(settings)
    redis = create_redis(settings)
    await redis.flushdb()

    async with create_sessionmaker(engine).begin() as session:
        tables = ", ".join(f'"{t.name}"' for t in Base.metadata.sorted_tables)
        await session.execute(text(f"TRUNCATE {tables} CASCADE"))

        organizer = User(
            id=uuid7(),
            email="bench-organizer@example.com",
            display_name="Bench",
            password_hash="!",
            role="organizer",
        )
        session.add(organizer)
        await session.flush()

        sections = max(1, seats // (ROWS * SEATS_PER_ROW))
        last_rows = ROWS if seats >= ROWS * SEATS_PER_ROW else max(1, seats // SEATS_PER_ROW)
        venue = await catalog.create_venue(
            session,
            organizer,
            VenueCreate(
                name="Bench Arena",
                city="Berlin",
                timezone="Europe/Berlin",
                sections=[
                    SectionIn(
                        name=f"Section {n + 1}",
                        rows=[
                            RowIn(label=str(r + 1), seats=SEATS_PER_ROW) for r in range(last_rows)
                        ],
                    )
                    for n in range(sections)
                ],
            ),
        )
        starts = datetime.now(UTC) + timedelta(days=30)
        tournament = await catalog.create_tournament(
            session,
            organizer,
            TournamentCreate(
                title="Bench Final",
                game="Starfall Tactics",
                venue_id=venue.id,
                team_size=5,
                currency="EUR",
                starts_at=starts,
                ends_at=starts + timedelta(hours=10),
                sales_open_at=datetime.now(UTC) - timedelta(minutes=1),
                waiting_room=waiting_room,
            ),
        )
        slug = tournament.slug
        await catalog.add_ticket_type(
            session,
            slug,
            organizer,
            TicketTypeCreate(
                kind="reserved",
                name="Seated",
                price_cents=4000,
                section_ids=[section.id for section in venue.sections],
            ),
        )
        await catalog.add_ticket_type(
            session,
            slug,
            organizer,
            TicketTypeCreate(kind="general", name="Standing", price_cents=1500, capacity=general),
        )
        published = await catalog.publish(session, slug, organizer)
        general_type = next(t.id for t in published.ticket_types if t.kind == "general")
        seat_map = await catalog.get_seat_map(session, venue.id)

        ids = [uuid7() for _ in range(buyers)]
        for start in range(0, buyers, 5000):
            await session.execute(
                insert(User),
                [
                    {
                        "id": user_id,
                        "email": f"bench-{user_id}@example.com",
                        "display_name": "Buyer",
                        "password_hash": "!",
                        "role": "user",
                    }
                    for user_id in ids[start : start + 5000]
                ],
            )
        await session.execute(text("ANALYZE"))

    await redis.aclose()
    await engine.dispose()
    state = {
        "slug": slug,
        "venue_id": str(venue.id),
        "general_type": str(general_type),
        "rows": [
            [str(seat.id) for seat in row.seats]
            for section in seat_map.sections
            for row in section.rows
        ],
        "tokens": [security.create_access_token(settings, user_id, "user") for user_id in ids],
    }
    with open(out, "w") as file:
        json.dump(state, file)
    total = sum(len(row) for row in state["rows"])
    sys.stdout.write(f"Prepared {slug}: {total} seats, {general} standing, {buyers} buyers\n")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seats", type=int, default=2000)
    parser.add_argument("--general", type=int, default=500)
    parser.add_argument("--buyers", type=int, default=1500)
    parser.add_argument("--waiting-room", action="store_true")
    parser.add_argument("--out", default="bench/.state.json")
    args = parser.parse_args()
    asyncio.run(
        prepare(
            seats=args.seats,
            general=args.general,
            buyers=args.buyers,
            waiting_room=args.waiting_room,
            out=args.out,
        )
    )


if __name__ == "__main__":
    main()
