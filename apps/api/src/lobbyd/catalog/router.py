"""HTTP endpoints for venues and tournaments."""

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query, status
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
from lobbyd.platform.db import SessionDep
from lobbyd.platform.pagination import Page

router = APIRouter()

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


@router.get("/venues/{venue_id}", tags=["venues"], summary="Venue and its sections")
async def get_venue(venue_id: uuid.UUID, session: SessionDep) -> VenueOut:
    return await service.get_venue(session, venue_id)


@router.get(
    "/venues/{venue_id}/seats",
    tags=["venues"],
    summary="Every seat, grouped by section and row",
    description="The layout only. It never changes, so it can be cached for a long time.",
)
async def get_seat_map(venue_id: uuid.UUID, session: SessionDep) -> SeatMap:
    return await service.get_seat_map(session, venue_id)


# --- tournaments ------------------------------------------------------------


@router.get(
    "/tournaments",
    tags=["tournaments"],
    summary="Browse and search published tournaments",
    description=(
        "Soonest first. Without `from`, tournaments that have already ended are left out. "
        "`q` understands quoted phrases, `or`, and `-word` to exclude."
    ),
)
async def list_tournaments(
    session: SessionDep,
    q: Annotated[str | None, Query(max_length=100)] = None,
    game: Annotated[str | None, Query(max_length=60)] = None,
    starts_from: Annotated[AwareDatetime | None, Query(alias="from")] = None,
    starts_to: Annotated[AwareDatetime | None, Query(alias="to")] = None,
    cursor: Annotated[str | None, Query(max_length=200)] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
) -> Page[TournamentSummary]:
    return await service.list_tournaments(
        session,
        q=q,
        game=game,
        starts_from=starts_from,
        starts_to=starts_to,
        cursor=cursor,
        limit=limit,
    )


@router.get("/games", tags=["tournaments"], summary="Games with upcoming tournaments")
async def list_games(session: SessionDep) -> list[GameCount]:
    return await service.list_games(session)


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


@router.get("/tournaments/{slug}", tags=["tournaments"], summary="Tournament detail")
async def get_tournament(slug: str, session: SessionDep, viewer: OptionalUser) -> TournamentOut:
    return await service.get_tournament(session, slug, viewer)


@router.patch(
    "/tournaments/{slug}",
    tags=["tournaments"],
    summary="Edit a tournament",
    description="Everything is editable in a draft. After publishing, only title and description.",
)
async def update_tournament(
    slug: str, body: TournamentUpdate, session: SessionDep, organizer: Organizer
) -> TournamentOut:
    return await service.update_tournament(session, slug, organizer, body)


@router.post(
    "/tournaments/{slug}/ticket-types",
    status_code=status.HTTP_201_CREATED,
    tags=["tournaments"],
    summary="Add team slots, reserved seating, or general admission",
)
async def add_ticket_type(
    slug: str, body: TicketTypeCreate, session: SessionDep, organizer: Organizer
) -> TournamentOut:
    return await service.add_ticket_type(session, slug, organizer, body)


@router.delete(
    "/tournaments/{slug}/ticket-types/{ticket_type_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    tags=["tournaments"],
    summary="Remove a ticket type from a draft",
)
async def remove_ticket_type(
    slug: str, ticket_type_id: uuid.UUID, session: SessionDep, organizer: Organizer
) -> None:
    await service.remove_ticket_type(session, slug, organizer, ticket_type_id)


@router.post(
    "/tournaments/{slug}/publish",
    tags=["tournaments"],
    summary="Make a draft visible and bookable",
)
async def publish_tournament(slug: str, session: SessionDep, organizer: Organizer) -> TournamentOut:
    return await service.publish(session, slug, organizer)
