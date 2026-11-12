"""Holds and availability. The only code that touches the inventory tables.

See docs/adr/0002-claiming-inventory.md for the reasoning. Two rules shape
everything here.

**Claims are conditional writes.** A seat or a counter is changed by one
UPDATE whose WHERE clause states the precondition, so "is it free?" and
"take it" cannot be separated by another buyer.

**Locks are always taken in the same order**, so two transactions can never
each hold what the other is waiting for:

    1. the caller's own hold row
    2. other people's lapsed holds, with SKIP LOCKED (never waited for)
    3. counter rows, ascending by ticket_type_id
    4. seat rows, ascending by seat_id

Optional tidying (the sweeper freeing lapsed seats) uses SKIP LOCKED, so it
never waits and cannot take part in a deadlock.
"""

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta

import structlog
from sqlalchemy import (
    ColumnElement,
    and_,
    case,
    func,
    insert,
    literal,
    or_,
    select,
    tuple_,
    update,
)
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from lobbyd.catalog import service as catalog
from lobbyd.catalog.service import SaleInfo
from lobbyd.config import Settings
from lobbyd.inventory.models import Hold, HoldItem, InventoryCounter, TournamentSeat
from lobbyd.inventory.schemas import (
    Availability,
    HoldCreate,
    HoldItemOut,
    HoldOut,
    TicketTypeAvailability,
)
from lobbyd.platform.db import violated_constraint
from lobbyd.platform.errors import ProblemError
from lobbyd.platform.ids import uuid7

log = structlog.get_logger()

# A seat can be claimed if nobody has it, or if whoever had it ran out of time.
# Used both to read availability and as the precondition of the claim itself.
_SEAT_IS_FREE = or_(
    TournamentSeat.status == "available",
    and_(TournamentSeat.status == "held", TournamentSeat.held_until < func.now()),
)


async def _now(session: AsyncSession) -> datetime:
    """The database's clock, fixed for the transaction. Never the app server's:
    several servers can disagree about the time, one database cannot."""
    return (await session.execute(select(func.now()))).scalar_one()


# --- putting a tournament on sale --------------------------------------------


async def open_inventory(session: AsyncSession, info: SaleInfo) -> None:
    """Create the sellable inventory for a tournament. Runs when it is published."""
    counters = [
        {"ticket_type_id": t.id, "tournament_id": info.tournament_id, "capacity": t.capacity}
        for t in info.ticket_types.values()
        if t.kind != "reserved"
    ]
    if counters:
        await session.execute(insert(InventoryCounter), counters)

    seats = await catalog.seat_assignments(session, info.tournament_id)
    for start in range(0, len(seats), 5000):
        await session.execute(
            insert(TournamentSeat),
            [
                {
                    "tournament_id": info.tournament_id,
                    "seat_id": seat,
                    "ticket_type_id": ticket_type,
                }
                for seat, ticket_type in seats[start : start + 5000]
            ],
        )
    log.info("inventory_opened", slug=info.slug, seats=len(seats), counters=len(counters))


# --- reading -------------------------------------------------------------------


async def availability(session: AsyncSession, slug: str) -> Availability:
    info = await catalog.get_sale_info(session, slug)

    counted = await session.execute(
        select(
            InventoryCounter.ticket_type_id,
            InventoryCounter.capacity - InventoryCounter.held_count - InventoryCounter.sold_count,
        ).where(InventoryCounter.tournament_id == info.tournament_id)
    )
    available = {ticket_type_id: free for ticket_type_id, free in counted}

    free_seats = await session.execute(
        select(TournamentSeat.ticket_type_id, func.count().filter(_SEAT_IS_FREE))
        .where(TournamentSeat.tournament_id == info.tournament_id)
        .group_by(TournamentSeat.ticket_type_id)
    )
    available.update({ticket_type_id: free for ticket_type_id, free in free_seats})

    taken = await session.scalars(
        select(TournamentSeat.seat_id).where(
            TournamentSeat.tournament_id == info.tournament_id, ~_SEAT_IS_FREE
        )
    )
    return Availability(
        tournament_id=info.tournament_id,
        ticket_types=[
            TicketTypeAvailability(
                ticket_type_id=t.id,
                kind=t.kind,
                capacity=t.capacity,
                available=available.get(t.id, 0),
            )
            for t in info.ticket_types.values()
        ],
        taken_seat_ids=list(taken),
    )


async def _hold_out(session: AsyncSession, hold: Hold) -> HoldOut:
    items = (
        await session.execute(
            select(
                HoldItem.ticket_type_id,
                HoldItem.seat_id,
                HoldItem.quantity,
                HoldItem.unit_price_cents,
            )
            .where(HoldItem.hold_id == hold.id)
            .order_by(HoldItem.id)
        )
    ).all()
    seats = await catalog.seat_labels(session, [i.seat_id for i in items if i.seat_id])
    names = await catalog.ticket_type_names(session, [i.ticket_type_id for i in items])
    tournament = (await catalog.tournament_refs(session, [hold.tournament_id]))[hold.tournament_id]
    return HoldOut(
        id=hold.id,
        tournament_id=hold.tournament_id,
        tournament=tournament,
        status=hold.status,
        expires_at=hold.expires_at,
        items=[
            HoldItemOut(
                **item._asdict(),
                ticket_type_name=names[item.ticket_type_id],
                seat=seats.get(item.seat_id) if item.seat_id else None,
            )
            for item in items
        ],
        total_cents=sum(item.quantity * item.unit_price_cents for item in items),
        currency=hold.currency,
    )


def _hold_not_found() -> ProblemError:
    return ProblemError(404, "hold_not_found", "No such hold.")


async def get_hold(session: AsyncSession, user_id: uuid.UUID, hold_id: uuid.UUID) -> HoldOut:
    hold = await session.scalar(select(Hold).where(Hold.id == hold_id, Hold.user_id == user_id))
    if hold is None:
        raise _hold_not_found()
    out = await _hold_out(session, hold)
    if hold.status == "active" and hold.expires_at <= await _now(session):
        # Out of time, even if the sweeper has not recorded it yet.
        out.status = "expired"
    return out


# --- claiming ------------------------------------------------------------------


def _check_on_sale(info: SaleInfo, now: datetime) -> None:
    if info.status not in ("published", "live") or now >= info.ends_at:
        raise ProblemError(409, "sales_closed", "This tournament is no longer on sale.")
    if now < info.sales_open_at:
        raise ProblemError(
            409,
            "sales_not_open",
            "Sales for this tournament have not opened yet.",
            opens_at=info.sales_open_at.isoformat(),
        )


async def create_hold(
    session: AsyncSession, settings: Settings, user_id: uuid.UUID, data: HoldCreate
) -> HoldOut:
    """Take everything asked for off sale for a few minutes, or nothing at all.

    Any refusal raises, and the caller's savepoint undoes whatever part of the
    claim had already been written.
    """
    info = await catalog.get_sale_info(session, data.tournament)
    now = await _now(session)
    _check_on_sale(info, now)

    counted = []
    for item in sorted(data.items, key=lambda i: i.ticket_type_id):  # lock order
        ticket_type = info.ticket_types.get(item.ticket_type_id)
        if ticket_type is None or ticket_type.kind == "reserved":
            raise ProblemError(
                422,
                "unknown_ticket_type",
                "items must name team slots or general admission for this tournament.",
            )
        if ticket_type.kind == "competitor" and item.quantity != 1:
            raise ProblemError(422, "invalid_quantity", "A team takes exactly one slot.")
        counted.append((ticket_type, item.quantity))

    # 1. Our own hold row.
    hold = await _open_hold(session, settings, user_id, info, now)

    # 2 and 3. Counted inventory.
    if counted:
        await _reclaim_if_short(session, info.tournament_id, counted)
    items: list[dict[str, object]] = []
    for ticket_type, quantity in counted:
        claimed = await session.execute(
            update(InventoryCounter)
            .where(
                InventoryCounter.ticket_type_id == ticket_type.id,
                InventoryCounter.held_count + InventoryCounter.sold_count + quantity
                <= InventoryCounter.capacity,
            )
            .values(held_count=InventoryCounter.held_count + quantity)
            .returning(InventoryCounter.ticket_type_id)
        )
        if claimed.first() is None:
            raise ProblemError(
                409,
                "sold_out",
                f"Not enough left of {ticket_type.name!r}.",
                ticket_type_id=str(ticket_type.id),
            )
        items.append(
            {
                "ticket_type_id": ticket_type.id,
                "seat_id": None,
                "quantity": quantity,
                "unit_price_cents": ticket_type.price_cents,
            }
        )

    # 4. Seats.
    if data.seat_ids:
        for seat_id, ticket_type_id in await _claim_seats(session, info, hold, data.seat_ids):
            items.append(
                {
                    "ticket_type_id": ticket_type_id,
                    "seat_id": seat_id,
                    "quantity": 1,
                    "unit_price_cents": info.ticket_types[ticket_type_id].price_cents,
                }
            )

    await session.execute(
        insert(HoldItem), [{"id": uuid7(), "hold_id": hold.id, **item} for item in items]
    )
    return await _hold_out(session, hold)


async def _open_hold(
    session: AsyncSession, settings: Settings, user_id: uuid.UUID, info: SaleInfo, now: datetime
) -> Hold:
    existing = (
        await session.execute(
            select(Hold.id, Hold.expires_at)
            .where(
                Hold.user_id == user_id,
                Hold.tournament_id == info.tournament_id,
                Hold.status == "active",
            )
            .with_for_update()
        )
    ).one_or_none()
    if existing is not None:
        if existing.expires_at > now:
            raise _hold_exists(existing.id)
        # Their old hold ran out and the sweeper has not got to it. Close it
        # here so it does not block them from trying again.
        await _lock_counters(session, info.tournament_id)
        await _close_holds(session, [existing.id], "expired")

    hold = Hold(
        id=uuid7(),
        user_id=user_id,
        tournament_id=info.tournament_id,
        currency=info.currency,
        status="active",
        expires_at=now + timedelta(seconds=settings.hold_ttl_seconds),
    )
    try:
        async with session.begin_nested():
            session.add(hold)
    except IntegrityError as exc:
        if violated_constraint(exc) != "holds_one_active_idx":
            raise
        # A second request from the same user got there first.
        raise ProblemError(
            409, "hold_exists", "You already have a hold for this tournament."
        ) from exc
    return hold


def _hold_exists(hold_id: uuid.UUID) -> ProblemError:
    return ProblemError(
        409,
        "hold_exists",
        "You already have a hold for this tournament. Check out or release it first.",
        hold_id=str(hold_id),
    )


async def _claim_seats(
    session: AsyncSession, info: SaleInfo, hold: Hold, seat_ids: list[uuid.UUID]
) -> list[tuple[uuid.UUID, uuid.UUID]]:
    # Lock the rows in a fixed order first. Two overlapping claims then queue
    # behind each other instead of deadlocking, and what we read is final.
    rows = (
        await session.execute(
            select(
                TournamentSeat.seat_id,
                TournamentSeat.ticket_type_id,
                case((_SEAT_IS_FREE, literal(True)), else_=literal(False)).label("free"),
            )
            .where(
                TournamentSeat.tournament_id == info.tournament_id,
                TournamentSeat.seat_id.in_(seat_ids),
            )
            .order_by(TournamentSeat.seat_id)
            .with_for_update()
        )
    ).all()
    if len(rows) != len(seat_ids):
        raise ProblemError(422, "unknown_seat", "Every seat must be on sale for this tournament.")
    taken = [str(row.seat_id) for row in rows if not row.free]
    if taken:
        raise ProblemError(
            409, "seats_unavailable", "Some of these seats are no longer available.", seat_ids=taken
        )

    # The claim itself: still conditional, so it is correct on its own even
    # if the check above were ever removed or bypassed.
    claimed = await session.execute(
        update(TournamentSeat)
        .where(
            TournamentSeat.tournament_id == info.tournament_id,
            TournamentSeat.seat_id.in_(seat_ids),
            _SEAT_IS_FREE,
        )
        .values(status="held", hold_id=hold.id, held_until=hold.expires_at)
        .returning(TournamentSeat.seat_id)
    )
    if len(claimed.all()) != len(seat_ids):
        raise ProblemError(409, "seats_unavailable", "Some of these seats are no longer available.")
    return [(row.seat_id, row.ticket_type_id) for row in rows]


# --- letting go ------------------------------------------------------------------


async def _lock_hold(
    session: AsyncSession, hold_id: uuid.UUID, user_id: uuid.UUID | None
) -> Hold | None:
    """Lock a hold. With `user_id`, only if it is theirs; without, on the system's behalf."""
    query = select(Hold).where(Hold.id == hold_id).with_for_update()
    if user_id is not None:
        query = query.where(Hold.user_id == user_id)
    return await session.scalar(query)


async def release_hold(
    session: AsyncSession, hold_id: uuid.UUID, *, user_id: uuid.UUID | None = None
) -> None:
    """Give a hold back early. Releasing one that is already closed is not an error."""
    hold = await _lock_hold(session, hold_id, user_id)
    if hold is None:
        raise _hold_not_found()
    if hold.status != "active":
        return
    await _close_holds(session, [hold.id], "released")
    # The user is giving these back now, before the deadline, so free the seat
    # rows here; nothing else would until held_until passes.
    seats = select(TournamentSeat.seat_id).where(
        TournamentSeat.tournament_id == hold.tournament_id, TournamentSeat.hold_id == hold.id
    )
    await session.execute(seats.order_by(TournamentSeat.seat_id).with_for_update())
    await session.execute(
        update(TournamentSeat)
        .where(
            TournamentSeat.tournament_id == hold.tournament_id, TournamentSeat.hold_id == hold.id
        )
        .values(status="available", hold_id=None, held_until=None)
    )


async def _close_holds(session: AsyncSession, hold_ids: list[uuid.UUID], status: str) -> None:
    """Mark holds closed and return their counted inventory.

    The caller must already hold the row locks on these holds. Seat rows are
    left alone: once held_until passes they are claimable as they stand.
    """
    await session.execute(update(Hold).where(Hold.id.in_(hold_ids)).values(status=status))
    returned = (
        await session.execute(
            select(HoldItem.ticket_type_id, func.sum(HoldItem.quantity))
            .where(HoldItem.hold_id.in_(hold_ids), HoldItem.seat_id.is_(None))
            .group_by(HoldItem.ticket_type_id)
            .order_by(HoldItem.ticket_type_id)  # lock order
        )
    ).all()
    for ticket_type_id, quantity in returned:
        await session.execute(
            update(InventoryCounter)
            .where(InventoryCounter.ticket_type_id == ticket_type_id)
            .values(held_count=InventoryCounter.held_count - quantity)
        )


async def expire_due_holds(
    session: AsyncSession, *, limit: int = 200, tournament_id: uuid.UUID | None = None
) -> int:
    """Close holds whose time is up. Returns how many. Safe to run from many workers at once."""
    due = (
        select(Hold.id)
        .where(Hold.status == "active", Hold.expires_at < func.now())
        .order_by(Hold.expires_at)
        .limit(limit)
        # Skip holds another worker has, or that are being checked out right
        # now. Never wait: this is tidying, and waiting is how deadlocks start.
        .with_for_update(skip_locked=True)
    )
    if tournament_id is not None:
        due = due.where(Hold.tournament_id == tournament_id)
    hold_ids = list(await session.scalars(due))
    if hold_ids:
        await _close_holds(session, hold_ids, "expired")
    return len(hold_ids)


async def _reclaim_if_short(
    session: AsyncSession,
    tournament_id: uuid.UUID,
    wanted: list[tuple[catalog.TicketTypeInfo, int]],
) -> None:
    """Counters, unlike seats, cannot notice a lapsed hold by themselves.

    So when a counter looks too full for this request, close this
    tournament's lapsed holds on the spot. Buyers then never depend on the
    sweeper having run recently.
    """
    free = dict(
        (
            await session.execute(
                select(
                    InventoryCounter.ticket_type_id,
                    InventoryCounter.capacity
                    - InventoryCounter.held_count
                    - InventoryCounter.sold_count,
                ).where(InventoryCounter.tournament_id == tournament_id)
            )
        ).all()
    )
    if all(free.get(ticket_type.id, 0) >= quantity for ticket_type, quantity in wanted):
        return
    await _lock_counters(session, tournament_id)
    await expire_due_holds(session, tournament_id=tournament_id)


async def _lock_counters(session: AsyncSession, tournament_id: uuid.UUID) -> None:
    """Take every counter of the tournament, in order, before touching any.

    Called before closing other holds inside a claim. Whatever counters that
    turns out to need, and whatever the claim needs afterwards, this
    transaction then already has, in the agreed order.
    """
    await session.execute(
        select(InventoryCounter.ticket_type_id)
        .where(InventoryCounter.tournament_id == tournament_id)
        .order_by(InventoryCounter.ticket_type_id)
        .with_for_update()
    )


async def free_lapsed_seats(session: AsyncSession, *, limit: int = 1000) -> int:
    """Reset seat rows whose hold ran out. Cosmetic: they were already claimable."""
    key = tuple_(TournamentSeat.tournament_id, TournamentSeat.seat_id)
    lapsed = (
        select(TournamentSeat.tournament_id, TournamentSeat.seat_id)
        .where(TournamentSeat.status == "held", TournamentSeat.held_until < func.now())
        .limit(limit)
        .with_for_update(skip_locked=True)
    )
    freed = await session.execute(
        update(TournamentSeat)
        .where(key.in_(lapsed), TournamentSeat.status == "held")
        .values(status="available", hold_id=None, held_until=None)
        .returning(TournamentSeat.seat_id)
    )
    return len(freed.all())


# --- interface for the orders module ------------------------------------------


@dataclass(frozen=True)
class PurchasedItem:
    ticket_type_id: uuid.UUID
    seat_id: uuid.UUID | None
    quantity: int
    unit_price_cents: int


@dataclass(frozen=True)
class HoldSnapshot:
    hold_id: uuid.UUID
    tournament_id: uuid.UUID
    currency: str
    items: list[PurchasedItem]


async def _items(session: AsyncSession, hold_id: uuid.UUID) -> list[PurchasedItem]:
    rows = await session.execute(
        select(
            HoldItem.ticket_type_id, HoldItem.seat_id, HoldItem.quantity, HoldItem.unit_price_cents
        )
        .where(HoldItem.hold_id == hold_id)
        .order_by(HoldItem.id)
    )
    return [PurchasedItem(*row) for row in rows]


def _our_seats(hold: Hold) -> ColumnElement[bool]:
    return and_(
        TournamentSeat.tournament_id == hold.tournament_id,
        TournamentSeat.hold_id == hold.id,
        TournamentSeat.status == "held",
    )


async def begin_checkout(
    session: AsyncSession, user_id: uuid.UUID, hold_id: uuid.UUID, *, window_seconds: int
) -> HoldSnapshot:
    """Check a hold is good to pay for, and give it time for the payment to finish.

    Without the extension, a buyer who starts paying in the last seconds of
    their hold could lose the seats while the payment is in flight.
    """
    hold = await _lock_hold(session, hold_id, user_id)
    if hold is None:
        raise _hold_not_found()
    if hold.status == "converted":
        raise ProblemError(409, "hold_already_used", "This hold has already been checked out.")
    now = await _now(session)
    if hold.status != "active" or hold.expires_at <= now:
        raise _hold_expired()

    deadline = max(hold.expires_at, now + timedelta(seconds=window_seconds))
    if deadline > hold.expires_at:
        hold.expires_at = deadline
        ours = _our_seats(hold)
        await session.execute(
            select(TournamentSeat.seat_id)
            .where(ours)
            .order_by(TournamentSeat.seat_id)
            .with_for_update()
        )
        await session.execute(update(TournamentSeat).where(ours).values(held_until=deadline))
        await session.flush()
    return HoldSnapshot(hold.id, hold.tournament_id, hold.currency, await _items(session, hold.id))


async def convert_hold(session: AsyncSession, hold_id: uuid.UUID) -> None:
    """Turn a hold into a sale. All of it, or `hold_expired`.

    Called when payment has succeeded, on the system's behalf.
    """
    hold = await _lock_hold(session, hold_id, None)
    if hold is None:
        raise _hold_not_found()
    if hold.status == "converted":
        raise ProblemError(409, "hold_already_used", "This hold has already been checked out.")
    if hold.status != "active":
        raise _hold_expired()

    items = await _items(session, hold.id)
    counted: dict[uuid.UUID, int] = {}
    for item in items:
        if item.seat_id is None:
            counted[item.ticket_type_id] = counted.get(item.ticket_type_id, 0) + item.quantity
    for ticket_type_id in sorted(counted):  # lock order
        await session.execute(
            update(InventoryCounter)
            .where(InventoryCounter.ticket_type_id == ticket_type_id)
            .values(
                held_count=InventoryCounter.held_count - counted[ticket_type_id],
                sold_count=InventoryCounter.sold_count + counted[ticket_type_id],
            )
        )

    seat_count = sum(1 for item in items if item.seat_id is not None)
    if seat_count:
        ours = _our_seats(hold)
        await session.execute(
            select(TournamentSeat.seat_id)
            .where(ours)
            .order_by(TournamentSeat.seat_id)
            .with_for_update()
        )
        sold = await session.execute(
            update(TournamentSeat)
            .where(ours)
            .values(status="sold", hold_id=None, held_until=None)
            .returning(TournamentSeat.seat_id)
        )
        # The seat rows still pointing at this hold are the proof we still own
        # them. No clock is consulted: a payment that lands a moment past the
        # deadline keeps its seats if nobody else took them. If someone did,
        # sell nothing.
        if len(sold.all()) != seat_count:
            raise _hold_expired()

    hold.status = "converted"
    await session.flush()


async def closed_holds(session: AsyncSession, hold_ids: list[uuid.UUID]) -> set[uuid.UUID]:
    """Which of these holds have lapsed or been released."""
    rows = await session.scalars(
        select(Hold.id).where(Hold.id.in_(hold_ids), Hold.status.in_(("expired", "released")))
    )
    return set(rows)


def _hold_expired() -> ProblemError:
    return ProblemError(
        409, "hold_expired", "This hold has expired and its inventory went back on sale."
    )
