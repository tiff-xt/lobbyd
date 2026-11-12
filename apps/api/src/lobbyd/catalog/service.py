"""Catalog logic. The only code that touches the catalog tables."""

import uuid
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import structlog
from sqlalchemy import delete, func, insert, select, text, tuple_, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import joinedload, selectinload

from lobbyd.catalog.hooks import run_publish_hooks
from lobbyd.catalog.models import (
    LISTED_SQL,
    MAX_DURATION,
    Seat,
    Section,
    TicketType,
    TicketTypeSection,
    Tournament,
    Venue,
)
from lobbyd.catalog.schemas import (
    GameCount,
    RowOut,
    SeatLabel,
    SeatMap,
    SeatOut,
    SectionLayout,
    SectionOut,
    TicketTypeCreate,
    TicketTypeOut,
    TournamentCreate,
    TournamentOut,
    TournamentRef,
    TournamentSummary,
    TournamentUpdate,
    VenueCreate,
    VenueOut,
    VenueSummary,
)
from lobbyd.catalog.slug import slugify, with_suffix
from lobbyd.identity.models import User
from lobbyd.platform.db import violated_constraint
from lobbyd.platform.errors import ProblemError
from lobbyd.platform.ids import uuid7
from lobbyd.platform.memo import Memo
from lobbyd.platform.pagination import Page, decode_cursor, encode_cursor

log = structlog.get_logger()

# Once published, people may have made plans around these, so they are frozen.
_EDITABLE_AFTER_PUBLISH = {"title", "description"}


def _is_owner(user: User | None, owner_id: uuid.UUID) -> bool:
    return user is not None and (user.role == "admin" or user.id == owner_id)


# --- venues ---------------------------------------------------------------


async def create_venue(session: AsyncSession, owner: User, data: VenueCreate) -> VenueOut:
    venue = Venue(
        id=uuid7(), owner_id=owner.id, name=data.name, city=data.city, timezone=data.timezone
    )
    session.add(venue)
    await session.flush()

    sections: list[dict[str, Any]] = []
    seats: list[dict[str, Any]] = []
    for sort, section in enumerate(data.sections):
        section_id = uuid7()
        sections.append(
            {
                "id": section_id,
                "venue_id": venue.id,
                "name": section.name,
                "sort": sort,
                "seat_count": sum(row.seats for row in section.rows),
            }
        )
        for row_sort, row in enumerate(section.rows):
            seats.extend(
                {
                    "id": uuid7(),
                    "venue_id": venue.id,
                    "section_id": section_id,
                    "row_label": row.label,
                    "row_sort": row_sort,
                    "number": number,
                }
                for number in range(1, row.seats + 1)
            )

    # Two multi-row inserts, not one statement per seat: an arena is 12,000 rows.
    await session.execute(insert(Section), sections)
    await session.execute(insert(Seat), seats)
    log.info("venue_created", venue_id=str(venue.id), seats=len(seats))
    return await get_venue(session, venue.id)


async def list_venues_owned_by(session: AsyncSession, owner: User) -> list[VenueOut]:
    venues = await session.scalars(
        select(Venue)
        .where(Venue.owner_id == owner.id)
        .options(selectinload(Venue.sections))
        .order_by(Venue.name)
        .limit(200)
    )
    return [_venue_out(venue) for venue in venues]


async def get_venue(session: AsyncSession, venue_id: uuid.UUID) -> VenueOut:
    venue = await session.scalar(
        select(Venue).where(Venue.id == venue_id).options(selectinload(Venue.sections))
    )
    if venue is None:
        raise _venue_not_found()
    return _venue_out(venue)


def _venue_out(venue: Venue) -> VenueOut:
    return VenueOut(
        id=venue.id,
        name=venue.name,
        city=venue.city,
        timezone=venue.timezone,
        capacity=sum(section.seat_count for section in venue.sections),
        sections=[SectionOut.model_validate(section) for section in venue.sections],
    )


async def get_seat_map(session: AsyncSession, venue_id: uuid.UUID) -> SeatMap:
    """Every seat in the venue, grouped by section and row.

    Selects plain columns, not ORM objects: building 12,000 tracked objects
    just to serialise them again costs far more than the query itself.
    """
    sections = (
        await session.execute(
            select(Section.id, Section.name)
            .where(Section.venue_id == venue_id)
            .order_by(Section.sort)
        )
    ).all()
    if not sections:
        raise _venue_not_found()

    rows: dict[uuid.UUID, dict[str, list[SeatOut]]] = defaultdict(dict)
    seats = await session.execute(
        select(Seat.section_id, Seat.row_label, Seat.id, Seat.number)
        .where(Seat.venue_id == venue_id)
        .order_by(Seat.section_id, Seat.row_sort, Seat.number)
    )
    for section_id, row_label, seat_id, number in seats:
        rows[section_id].setdefault(row_label, []).append(
            SeatOut.model_construct(id=seat_id, number=number)
        )

    return SeatMap(
        venue_id=venue_id,
        sections=[
            SectionLayout(
                id=section_id,
                name=name,
                rows=[
                    RowOut.model_construct(label=label, seats=row_seats)
                    for label, row_seats in rows[section_id].items()
                ],
            )
            for section_id, name in sections
        ],
    )


def _venue_not_found() -> ProblemError:
    return ProblemError(404, "venue_not_found", "No such venue.")


# --- tournaments: reading -------------------------------------------------


def _summary_fields(tournament: Tournament) -> dict[str, Any]:
    return {
        "id": tournament.id,
        "slug": tournament.slug,
        "title": tournament.title,
        "game": tournament.game,
        "status": tournament.status,
        "team_size": tournament.team_size,
        "currency": tournament.currency,
        "starts_at": tournament.starts_at,
        "ends_at": tournament.ends_at,
        "sales_open_at": tournament.sales_open_at,
        "waiting_room": tournament.waiting_room,
        "venue": VenueSummary.model_validate(tournament.venue) if tournament.venue else None,
    }


def _detail(tournament: Tournament) -> TournamentOut:
    return TournamentOut(
        **_summary_fields(tournament),
        description=tournament.description,
        ticket_types=[
            TicketTypeOut(
                id=ticket_type.id,
                kind=ticket_type.kind,
                name=ticket_type.name,
                price_cents=ticket_type.price_cents,
                capacity=ticket_type.capacity,
                section_ids=[link.section_id for link in ticket_type.sections],
            )
            for ticket_type in tournament.ticket_types
        ],
    )


_DETAIL_OPTIONS = (
    selectinload(Tournament.venue),
    selectinload(Tournament.ticket_types).selectinload(TicketType.sections),
)


async def _load(session: AsyncSession, slug: str, *, lock: bool = False) -> Tournament:
    statement = (
        select(Tournament)
        .where(Tournament.slug == slug)
        .options(*_DETAIL_OPTIONS)
        .execution_options(populate_existing=True)
    )
    if lock:
        # Serialises edits to one tournament (two ticket types added at once,
        # or an edit racing a publish) without blocking readers.
        statement = statement.with_for_update(of=Tournament)
    tournament = await session.scalar(statement)
    if tournament is None:
        raise _tournament_not_found()
    return tournament


def _tournament_not_found() -> ProblemError:
    return ProblemError(404, "tournament_not_found", "No such tournament.")


async def get_tournament(session: AsyncSession, slug: str, viewer: User | None) -> TournamentOut:
    tournament = await _load(session, slug)
    if tournament.status == "draft" and not _is_owner(viewer, tournament.organizer_id):
        # A draft does not exist as far as anyone else is concerned.
        raise _tournament_not_found()
    return _detail(tournament)


def _not_ended() -> tuple[Any, Any]:
    """Conditions for "still running or yet to start".

    `ends_at >= now` alone cannot use the browse indexes, which are ordered by
    starts_at: the scan would begin at the oldest tournament ever published
    and step over all of history. Nothing lasts longer than MAX_DURATION, so
    anything not yet ended started after `now - MAX_DURATION`. That second
    condition is redundant logically, and it is what gives the index scan a
    place to start.
    """
    now = datetime.now(UTC)
    return Tournament.starts_at >= now - MAX_DURATION, Tournament.ends_at >= now


async def list_tournaments(
    session: AsyncSession,
    *,
    q: str | None,
    game: str | None,
    starts_from: datetime | None,
    starts_to: datetime | None,
    cursor: str | None,
    limit: int,
) -> Page[TournamentSummary]:
    statement = (
        select(Tournament)
        .options(joinedload(Tournament.venue))
        # Written as literal SQL, not bound parameters, so the planner can
        # always match it to the partial indexes that carry the same condition.
        .where(text(LISTED_SQL))
        .order_by(Tournament.starts_at, Tournament.id)
        .limit(limit + 1)  # one extra row tells us whether another page exists
    )
    if q:
        statement = statement.where(
            Tournament.search.op("@@")(func.websearch_to_tsquery("english", q))
        )
    if game:
        statement = statement.where(Tournament.game == game)
    if starts_from is not None:
        statement = statement.where(Tournament.starts_at >= starts_from)
    else:
        statement = statement.where(*_not_ended())
    if starts_to is not None:
        statement = statement.where(Tournament.starts_at < starts_to)
    if cursor:
        after_starts_at, after_id = decode_cursor(cursor, 2)
        try:
            position = (datetime.fromisoformat(after_starts_at), uuid.UUID(after_id))
        except ValueError as exc:
            raise ProblemError(422, "invalid_cursor", "The cursor is not valid.") from exc
        statement = statement.where(tuple_(Tournament.starts_at, Tournament.id) > position)

    tournaments = list(await session.scalars(statement))
    next_cursor = None
    if len(tournaments) > limit:
        tournaments = tournaments[:limit]
        last = tournaments[-1]
        next_cursor = encode_cursor(last.starts_at.isoformat(), last.id)
    return Page(
        items=[TournamentSummary(**_summary_fields(t)) for t in tournaments],
        next_cursor=next_cursor,
    )


async def list_organized_by(session: AsyncSession, organizer: User) -> list[TournamentSummary]:
    tournaments = await session.scalars(
        select(Tournament)
        .options(joinedload(Tournament.venue))
        .where(Tournament.organizer_id == organizer.id)
        .order_by(Tournament.created_at.desc())
        .limit(200)
    )
    return [TournamentSummary(**_summary_fields(t)) for t in tournaments]


async def list_games(session: AsyncSession) -> list[GameCount]:
    count = func.count().label("tournaments")
    rows = await session.execute(
        select(Tournament.game, count)
        .where(text(LISTED_SQL), *_not_ended())
        .group_by(Tournament.game)
        .order_by(count.desc(), Tournament.game)
        .limit(50)
    )
    return [GameCount(game=game, tournaments=total) for game, total in rows]


# --- tournaments: writing -------------------------------------------------


async def create_tournament(
    session: AsyncSession, organizer: User, data: TournamentCreate
) -> TournamentOut:
    if data.venue_id is not None:
        owner_id = await session.scalar(select(Venue.owner_id).where(Venue.id == data.venue_id))
        if owner_id is None or not _is_owner(organizer, owner_id):
            raise _venue_not_found()

    base = slugify(data.title)
    for candidate in (base, with_suffix(base), with_suffix(base)):
        try:
            # A savepoint, so a taken slug undoes only this insert.
            async with session.begin_nested():
                session.add(
                    Tournament(organizer_id=organizer.id, slug=candidate, **data.model_dump())
                )
        except IntegrityError as exc:
            if violated_constraint(exc) != "tournaments_slug_key":
                raise
            continue
        log.info("tournament_created", slug=candidate)
        return _detail(await _load(session, candidate))
    raise ProblemError(409, "slug_unavailable", "Could not find a free URL for this title.")


async def _load_owned(session: AsyncSession, slug: str, user: User) -> Tournament:
    tournament = await _load(session, slug, lock=True)
    if not _is_owner(user, tournament.organizer_id):
        if tournament.status == "draft":
            raise _tournament_not_found()
        raise ProblemError(403, "forbidden", "You do not have permission to do this.")
    return tournament


def _require_draft(tournament: Tournament) -> None:
    if tournament.status != "draft":
        raise ProblemError(
            409,
            "tournament_not_editable",
            "This can only be changed while the tournament is a draft.",
        )


async def update_tournament(
    session: AsyncSession, slug: str, user: User, data: TournamentUpdate
) -> TournamentOut:
    tournament = await _load_owned(session, slug, user)
    changes = data.model_dump(exclude_unset=True, exclude_none=True)
    if tournament.status != "draft" and not changes.keys() <= _EDITABLE_AFTER_PUBLISH:
        _require_draft(tournament)

    for field, value in changes.items():
        setattr(tournament, field, value)
    # Each date was valid on its own; check them together with the stored ones.
    if tournament.ends_at <= tournament.starts_at:
        raise ProblemError(422, "invalid_dates", "ends_at must be after starts_at.")
    if tournament.ends_at - tournament.starts_at > MAX_DURATION:
        raise ProblemError(
            422, "invalid_dates", f"A tournament can last at most {MAX_DURATION.days} days."
        )
    if tournament.sales_open_at >= tournament.starts_at:
        raise ProblemError(422, "invalid_dates", "sales_open_at must be before starts_at.")
    await session.flush()
    return _detail(await _load(session, slug))


async def add_ticket_type(
    session: AsyncSession, slug: str, user: User, data: TicketTypeCreate
) -> TournamentOut:
    tournament = await _load_owned(session, slug, user)
    _require_draft(tournament)

    capacity = data.capacity
    section_ids = data.section_ids or []
    if data.kind == "reserved":
        if tournament.venue_id is None:
            raise ProblemError(
                422, "venue_required", "Reserved seating needs a tournament with a venue."
            )
        found = (
            await session.execute(
                select(func.count(), func.coalesce(func.sum(Section.seat_count), 0)).where(
                    Section.venue_id == tournament.venue_id, Section.id.in_(section_ids)
                )
            )
        ).one()
        if found[0] != len(section_ids):
            raise ProblemError(
                422, "unknown_section", "Every section must belong to the tournament's venue."
            )
        capacity = int(found[1])

    ticket_type_id = uuid7()
    try:
        await session.execute(
            insert(TicketType).values(
                id=ticket_type_id,
                tournament_id=tournament.id,
                kind=data.kind,
                name=data.name,
                price_cents=data.price_cents,
                capacity=capacity,
            )
        )
        if section_ids:
            await session.execute(
                insert(TicketTypeSection),
                [
                    {
                        "ticket_type_id": ticket_type_id,
                        "section_id": section_id,
                        "tournament_id": tournament.id,
                    }
                    for section_id in section_ids
                ],
            )
    except IntegrityError as exc:
        await session.rollback()
        problems = {
            "ticket_types_tournament_id_name_key": (
                "ticket_type_name_taken",
                "This tournament already has a ticket type with that name.",
            ),
            "ticket_types_one_competitor_idx": (
                "competitor_type_exists",
                "A tournament has one set of team slots.",
            ),
            "ticket_type_sections_tournament_id_section_id_key": (
                "section_already_assigned",
                "One of these sections is already sold under another ticket type.",
            ),
        }
        problem = problems.get(violated_constraint(exc) or "")
        if problem is None:
            raise
        raise ProblemError(409, *problem) from exc
    return _detail(await _load(session, slug))


async def remove_ticket_type(
    session: AsyncSession, slug: str, user: User, ticket_type_id: uuid.UUID
) -> None:
    tournament = await _load_owned(session, slug, user)
    _require_draft(tournament)
    result = await session.execute(
        delete(TicketType)
        .where(TicketType.id == ticket_type_id, TicketType.tournament_id == tournament.id)
        .returning(TicketType.id)
    )
    if result.one_or_none() is None:
        raise ProblemError(404, "ticket_type_not_found", "No such ticket type.")


async def publish(session: AsyncSession, slug: str, user: User) -> TournamentOut:
    tournament = await _load_owned(session, slug, user)
    _require_draft(tournament)
    if not tournament.ticket_types:
        raise ProblemError(
            409, "tournament_not_publishable", "Add at least one ticket type before publishing."
        )
    if tournament.ends_at <= datetime.now(UTC):
        raise ProblemError(
            409, "tournament_not_publishable", "This tournament's dates are in the past."
        )
    await session.execute(
        update(Tournament).where(Tournament.id == tournament.id).values(status="published")
    )
    # Other modules prepare what they need (inventory puts the seats on sale)
    # in this same transaction.
    _sale_info.drop(slug)
    await run_publish_hooks(session, await get_sale_info(session, slug))
    log.info("tournament_published", slug=slug)
    return _detail(await _load(session, slug))


# --- interface for other modules ---------------------------------------------
# Modules above the catalog use these instead of reading its tables.


@dataclass(frozen=True)
class TicketTypeInfo:
    id: uuid.UUID
    kind: str
    name: str
    price_cents: int
    capacity: int


@dataclass(frozen=True)
class SaleInfo:
    """What the booking modules need to know about a tournament."""

    tournament_id: uuid.UUID
    slug: str
    status: str
    currency: str
    sales_open_at: datetime
    starts_at: datetime
    ends_at: datetime
    waiting_room: bool
    team_size: int
    ticket_types: dict[uuid.UUID, TicketTypeInfo]


async def get_sale_info(session: AsyncSession, slug: str) -> SaleInfo:
    """Raises tournament_not_found for a missing tournament or a draft.

    Read on every hold attempt, and nearly constant once a tournament is
    published, so it is remembered for a couple of seconds per process. The
    worst a stale copy can do is let a hold start in the two seconds after a
    sale is cancelled or closed.
    """
    remembered = _sale_info.get(slug)
    if remembered is not None:
        return remembered
    tournament = (
        await session.execute(
            select(
                Tournament.id,
                Tournament.status,
                Tournament.currency,
                Tournament.sales_open_at,
                Tournament.starts_at,
                Tournament.ends_at,
                Tournament.waiting_room,
                Tournament.team_size,
            ).where(Tournament.slug == slug, Tournament.status != "draft")
        )
    ).one_or_none()
    if tournament is None:
        raise _tournament_not_found()
    ticket_types = await session.execute(
        select(
            TicketType.id,
            TicketType.kind,
            TicketType.name,
            TicketType.price_cents,
            TicketType.capacity,
        ).where(TicketType.tournament_id == tournament.id)
    )
    info = SaleInfo(
        tournament_id=tournament.id,
        slug=slug,
        status=tournament.status,
        currency=tournament.currency,
        sales_open_at=tournament.sales_open_at,
        starts_at=tournament.starts_at,
        ends_at=tournament.ends_at,
        waiting_room=tournament.waiting_room,
        team_size=tournament.team_size,
        ticket_types={row.id: TicketTypeInfo(*row) for row in ticket_types},
    )
    _sale_info.put(slug, info)
    return info


async def seat_assignments(
    session: AsyncSession, tournament_id: uuid.UUID
) -> list[tuple[uuid.UUID, uuid.UUID]]:
    """(seat_id, ticket_type_id) for every seat sold as reserved seating."""
    rows = await session.execute(
        select(Seat.id, TicketTypeSection.ticket_type_id)
        .join(TicketTypeSection, TicketTypeSection.section_id == Seat.section_id)
        .where(TicketTypeSection.tournament_id == tournament_id)
    )
    return [(seat_id, ticket_type_id) for seat_id, ticket_type_id in rows]


# A seat's label and a ticket type's name never change once created, and both
# are looked up on almost every booking request. Remembering them in the
# process removes two to four queries from each of those requests.
_seat_labels: Memo[uuid.UUID, SeatLabel] = Memo(ttl_seconds=None)
_ticket_type_names: Memo[uuid.UUID, str] = Memo(ttl_seconds=None, max_entries=10_000)
# These can change (a title is edited, a sale is cancelled), so they are only
# trusted briefly. Set from settings.catalog_memo_seconds at startup.
_tournament_refs: Memo[uuid.UUID, TournamentRef] = Memo(ttl_seconds=30)
_sale_info: Memo[str, "SaleInfo"] = Memo(ttl_seconds=2, max_entries=2_000)


def configure_memos(seconds: float) -> None:
    """How long changeable catalog facts may be remembered. Zero turns it off."""
    _sale_info.ttl = seconds
    _tournament_refs.ttl = seconds * 15
    _sale_info.clear()
    _tournament_refs.clear()


async def seat_labels(
    session: AsyncSession, seat_ids: list[uuid.UUID]
) -> dict[uuid.UUID, SeatLabel]:
    async def load(missing: list[uuid.UUID]) -> dict[uuid.UUID, SeatLabel]:
        rows = await session.execute(
            select(Seat.id, Section.name, Seat.row_label, Seat.number)
            .join(Section, Section.id == Seat.section_id)
            .where(Seat.id.in_(missing))
        )
        return {
            seat_id: SeatLabel(section=section, row=row, number=number)
            for seat_id, section, row, number in rows
        }

    return await _seat_labels.get_many(seat_ids, load)


@dataclass(frozen=True)
class Managed:
    """A tournament, as far as the people running it on the day need to know."""

    tournament_id: uuid.UUID
    slug: str
    title: str
    team_size: int
    starts_at: datetime


async def require_owner(
    session: AsyncSession, user: User, *, slug: str | None = None, id: uuid.UUID | None = None
) -> Managed:
    """The tournament, if `user` runs it (or is an admin). Otherwise 403, or 404 for a draft."""
    where = Tournament.slug == slug if slug is not None else Tournament.id == id
    row = (
        await session.execute(
            select(
                Tournament.id,
                Tournament.slug,
                Tournament.title,
                Tournament.team_size,
                Tournament.starts_at,
                Tournament.status,
                Tournament.organizer_id,
            ).where(where)
        )
    ).one_or_none()
    if row is None:
        raise _tournament_not_found()
    if not _is_owner(user, row.organizer_id):
        if row.status == "draft":
            raise _tournament_not_found()
        raise ProblemError(403, "forbidden", "You do not have permission to do this.")
    return Managed(row.id, row.slug, row.title, row.team_size, row.starts_at)


async def tournament_refs(
    session: AsyncSession, tournament_ids: list[uuid.UUID]
) -> dict[uuid.UUID, TournamentRef]:
    async def load(missing: list[uuid.UUID]) -> dict[uuid.UUID, TournamentRef]:
        rows = await session.execute(
            select(
                Tournament.id,
                Tournament.slug,
                Tournament.title,
                Tournament.starts_at,
                Venue.timezone,
            )
            .outerjoin(Venue, Venue.id == Tournament.venue_id)
            .where(Tournament.id.in_(missing))
        )
        return {
            tournament_id: TournamentRef(
                slug=slug, title=title, starts_at=starts_at, timezone=timezone
            )
            for tournament_id, slug, title, starts_at, timezone in rows
        }

    return await _tournament_refs.get_many(tournament_ids, load)


async def ticket_type_names(
    session: AsyncSession, ticket_type_ids: list[uuid.UUID]
) -> dict[uuid.UUID, str]:
    async def load(missing: list[uuid.UUID]) -> dict[uuid.UUID, str]:
        rows = await session.execute(
            select(TicketType.id, TicketType.name).where(TicketType.id.in_(missing))
        )
        return {ticket_type_id: name for ticket_type_id, name in rows}

    return await _ticket_type_names.get_many(ticket_type_ids, load)
