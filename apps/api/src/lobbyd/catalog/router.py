"""HTTP endpoints for venues and tournaments."""

import hashlib
import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Response, status
from pydantic import AwareDatetime

from lobbyd.catalog import service
from lobbyd.catalog.schemas import (
    GameCount,
    SeatMap,
    TicketTypeCreate,
    TournamentCreate,
    TournamentOut,
    TournamentSummary,
    TournamentUpdate,
    VenueCreate,
    VenueOut,
)
from lobbyd.identity.deps import OptionalUser, require_role
from lobbyd.identity.models import User
from lobbyd.platform.cache import RedisDep, cached, invalidate
from lobbyd.platform.db import SessionDep
from lobbyd.platform.errors import ProblemError
from lobbyd.platform.pagination import Page
from lobbyd.platform.ratelimit import READS, by_ip

router = APIRouter()

_reads = [Depends(by_ip(READS))]

Organizer = Annotated[User, Depends(require_role("organizer"))]

# --- venues ---------------------------------------------------------------


@router.post(
    "/venues",
    status_code=status.HTTP_201_CREATED,
    tags=["venues"],
    summary="Create a venue with its seat map",
)
async def create_venue(body: VenueCreate, session: SessionDep, organizer: Organizer) -> VenueOut:
    return await service.create_venue(session, organizer, body)


@router.get(
    "/venues/{venue_id}", dependencies=_reads, tags=["venues"], summary="Venue and its sections"
)
async def get_venue(venue_id: uuid.UUID, session: SessionDep) -> VenueOut:
    return await service.get_venue(session, venue_id)


@router.get(
    "/venues/{venue_id}/seats",
    response_model=SeatMap,
    dependencies=_reads,
    tags=["venues"],
    summary="Every seat, grouped by section and row",
    description="The layout only. It never changes, so it is cached for a long time.",
)
async def get_seat_map(venue_id: uuid.UUID, session: SessionDep, redis: RedisDep) -> Response:
    async def load() -> str:
        return (await service.get_seat_map(session, venue_id)).model_dump_json()

    # 12,000 seats is 700 kB of JSON and a 12,000 row query. A venue's layout
    # is fixed once created, so build it once an hour, not once a request.
    body = await cached(redis, f"seatmap:{venue_id}", load, fresh_seconds=3600)
    return _json(body, max_age=3600)


def _json(body: str, *, max_age: int = 0) -> Response:
    """Send cached JSON text as it is, without parsing and re-serialising it."""
    headers = {"Cache-Control": f"public, max-age={max_age}"} if max_age else None
    return Response(body, media_type="application/json", headers=headers)


# --- tournaments ------------------------------------------------------------


@router.get(
    "/tournaments",
    response_model=Page[TournamentSummary],
    dependencies=_reads,
    tags=["tournaments"],
    summary="Browse and search published tournaments",
    description=(
        "Soonest first. Without `from`, tournaments that have already ended are left out. "
        "`q` understands quoted phrases, `or`, and `-word` to exclude."
    ),
)
async def list_tournaments(
    session: SessionDep,
    redis: RedisDep,
    q: Annotated[str | None, Query(max_length=100)] = None,
    game: Annotated[str | None, Query(max_length=60)] = None,
    starts_from: Annotated[AwareDatetime | None, Query(alias="from")] = None,
    starts_to: Annotated[AwareDatetime | None, Query(alias="to")] = None,
    cursor: Annotated[str | None, Query(max_length=200)] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
) -> Response:
    async def load() -> str:
        page = await service.list_tournaments(
            session,
            q=q,
            game=game,
            starts_from=starts_from,
            starts_to=starts_to,
            cursor=cursor,
            limit=limit,
        )
        return page.model_dump_json()

    # The most requested page on the site, and the costliest catalog query
    # (see docs/query-plans.md). Five seconds fresh, ten more while one
    # request refreshes it: under a spike it is computed once, not per visitor.
    query = f"{q}|{(game or '').lower()}|{starts_from}|{starts_to}|{cursor}|{limit}"
    key = "tournaments:" + hashlib.sha256(query.encode()).hexdigest()[:32]
    return _json(await cached(redis, key, load, fresh_seconds=5, stale_seconds=10))


@router.get(
    "/games",
    response_model=list[GameCount],
    dependencies=_reads,
    tags=["tournaments"],
    summary="Games with upcoming tournaments",
)
async def list_games(session: SessionDep, redis: RedisDep) -> Response:
    async def load() -> str:
        games = await service.list_games(session)
        return "[" + ",".join(game.model_dump_json() for game in games) + "]"

    return _json(await cached(redis, "games", load, fresh_seconds=30, stale_seconds=60))


@router.get(
    "/users/me/tournaments",
    tags=["tournaments"],
    summary="Tournaments I organize, drafts included",
)
async def list_my_tournaments(session: SessionDep, organizer: Organizer) -> list[TournamentSummary]:
    return await service.list_organized_by(session, organizer)


@router.post(
    "/tournaments",
    status_code=status.HTTP_201_CREATED,
    tags=["tournaments"],
    summary="Create a draft tournament",
)
async def create_tournament(
    body: TournamentCreate, session: SessionDep, organizer: Organizer
) -> TournamentOut:
    return await service.create_tournament(session, organizer, body)


@router.get(
    "/tournaments/{slug}",
    response_model=TournamentOut,
    dependencies=_reads,
    tags=["tournaments"],
    summary="Tournament detail",
)
async def get_tournament(
    slug: str, session: SessionDep, redis: RedisDep, viewer: OptionalUser
) -> Response:
    async def load_public() -> str:
        # What anyone may see. A draft raises 404 here and is never cached.
        return (await service.get_tournament(session, slug, None)).model_dump_json()

    try:
        return _json(await cached(redis, _detail_key(slug), load_public, fresh_seconds=30))
    except ProblemError as exc:
        if exc.status != 404 or viewer is None:
            raise
    # Not public: perhaps the viewer's own draft. Always read fresh.
    return _json((await service.get_tournament(session, slug, viewer)).model_dump_json())


def _detail_key(slug: str) -> str:
    return f"tournament:{slug}"


@router.patch(
    "/tournaments/{slug}",
    tags=["tournaments"],
    summary="Edit a tournament",
    description="Everything is editable in a draft. After publishing, only title and description.",
)
async def update_tournament(
    slug: str, body: TournamentUpdate, session: SessionDep, redis: RedisDep, organizer: Organizer
) -> TournamentOut:
    result = await service.update_tournament(session, slug, organizer, body)
    await invalidate(redis, _detail_key(slug))
    return result


@router.post(
    "/tournaments/{slug}/ticket-types",
    status_code=status.HTTP_201_CREATED,
    tags=["tournaments"],
    summary="Add team slots, reserved seating, or general admission",
)
async def add_ticket_type(
    slug: str, body: TicketTypeCreate, session: SessionDep, redis: RedisDep, organizer: Organizer
) -> TournamentOut:
    result = await service.add_ticket_type(session, slug, organizer, body)
    await invalidate(redis, _detail_key(slug))
    return result


@router.delete(
    "/tournaments/{slug}/ticket-types/{ticket_type_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    tags=["tournaments"],
    summary="Remove a ticket type from a draft",
)
async def remove_ticket_type(
    slug: str,
    ticket_type_id: uuid.UUID,
    session: SessionDep,
    redis: RedisDep,
    organizer: Organizer,
) -> None:
    await service.remove_ticket_type(session, slug, organizer, ticket_type_id)
    await invalidate(redis, _detail_key(slug))


@router.post(
    "/tournaments/{slug}/publish",
    tags=["tournaments"],
    summary="Make a draft visible and bookable",
)
async def publish_tournament(
    slug: str, session: SessionDep, redis: RedisDep, organizer: Organizer
) -> TournamentOut:
    result = await service.publish(session, slug, organizer)
    await invalidate(redis, _detail_key(slug))
    return result
