"""Team registration and the waitlist. The only code that touches the teams tables.

Registering a team claims a competitor slot, records the team, and records
its roster in one transaction: all of it, or none. The slot is claimed
through the same hold mechanism as a seat, so it is paid for through the
same checkout, lapses the same way, and cannot be oversold for the same
reasons.

A team's life:

    pending --entry fee paid--> confirmed --captain withdraws--> withdrawn
       \\--hold lapsed, or payment failed--> withdrawn

Rules that span rows are constraints, so they hold when requests race: one
team per name per tournament, one team per captain, and one team per player.
"""

import uuid
from datetime import UTC, datetime
from typing import Any

import structlog
from redis.asyncio import Redis
from sqlalchemy import delete, func, insert, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from lobbyd.catalog import service as catalog
from lobbyd.catalog.service import SaleInfo, TicketTypeInfo
from lobbyd.config import Settings
from lobbyd.inventory import service as inventory
from lobbyd.inventory.schemas import CountedItemIn, HoldCreate
from lobbyd.platform import outbox
from lobbyd.platform.db import violated_constraint
from lobbyd.platform.errors import ProblemError
from lobbyd.platform.events import EventContext
from lobbyd.platform.ids import uuid7
from lobbyd.teams.models import Team, TeamMember, WaitlistEntry
from lobbyd.teams.schemas import Roster, TeamCreate, TeamOut, TeamRegistration, WaitlistOut

log = structlog.get_logger()

TEAM_WITHDRAWN = "team.withdrawn"
WAITLIST_OFFERED = "waitlist.offered"

# constraint -> (error code, message)
_CONFLICTS = {
    "teams_name_idx": ("team_name_taken", "Another team in this tournament has that name."),
    "teams_captain_idx": (
        "already_registered",
        "You have already entered a team in this tournament.",
    ),
    "team_members_tournament_id_handle_key": (
        "player_already_registered",
        "One of these players is already on another team in this tournament.",
    ),
}


def _slot_type(info: SaleInfo) -> TicketTypeInfo:
    for ticket_type in info.ticket_types.values():
        if ticket_type.kind == "competitor":
            return ticket_type
    raise ProblemError(422, "no_team_slots", "This tournament does not take team entries.")


def _check_open(info: SaleInfo) -> None:
    """Spectators can buy until the last match. Teams enter before the first."""
    if info.status not in ("published", "live") or datetime.now(UTC) >= info.starts_at:
        raise ProblemError(
            409, "registration_closed", "Team entry closed when the tournament started."
        )


def _check_roster(info: SaleInfo, data: Roster) -> None:
    if len(data.roster) != info.team_size:
        raise ProblemError(
            422,
            "wrong_roster_size",
            f"This tournament is {info.team_size}v{info.team_size}: "
            f"list exactly {info.team_size} player{'s' if info.team_size != 1 else ''}.",
            team_size=info.team_size,
        )


# --- registering ----------------------------------------------------------------


async def register(
    session: AsyncSession,
    settings: Settings,
    user_id: uuid.UUID,
    slug: str,
    data: TeamCreate,
    *,
    admission_token: str | None = None,
    redis: Redis | None = None,
    from_waitlist: bool = False,
) -> TeamRegistration:
    """Claim a slot and record the team and its roster. All of it, or a refusal.

    The caller runs this inside a savepoint (the idempotency wrapper, or the
    waitlist promoter), so a refusal at any step undoes the slot claim too.
    """
    info = await catalog.get_sale_info(session, slug)
    slot = _slot_type(info)
    _check_open(info)
    _check_roster(info, data)

    # The slot first. This is the contended step: of fifty captains racing
    # for the last one, forty-nine are refused here, before anything else.
    hold = await inventory.create_hold(
        session,
        settings,
        user_id,
        HoldCreate(
            tournament=slug,
            seat_ids=data.seat_ids,
            items=[*data.items, CountedItemIn(ticket_type_id=slot.id, quantity=1)],
        ),
        admission_token,
        redis,
        team_slot=True,
        on_behalf_of_system=from_waitlist,
        ttl_seconds=settings.waitlist_offer_seconds if from_waitlist else None,
    )

    team_id = uuid7()
    try:
        async with session.begin_nested():
            await session.execute(
                insert(Team).values(
                    id=team_id,
                    tournament_id=info.tournament_id,
                    captain_id=user_id,
                    name=data.name,
                    ticket_type_id=slot.id,
                    hold_id=hold.id,
                )
            )
            await session.execute(
                insert(TeamMember),
                [
                    {
                        "team_id": team_id,
                        "tournament_id": info.tournament_id,
                        "handle": handle,
                        "position": position,
                    }
                    for position, handle in enumerate(data.roster)
                ],
            )
    except IntegrityError as exc:
        conflict = _CONFLICTS.get(violated_constraint(exc) or "")
        if conflict is None:
            raise
        raise ProblemError(409, *conflict) from exc

    log.info("team_registered", team_id=str(team_id), slug=slug)
    team = (await _teams(session, Team.id == team_id, private=True))[0]
    return TeamRegistration(team=team, hold=hold)


async def _teams(session: AsyncSession, *where: Any, private: bool = False) -> list[TeamOut]:
    teams = list(await session.scalars(select(Team).where(*where).order_by(Team.id)))
    if not teams:
        return []
    rosters: dict[uuid.UUID, list[str]] = {team.id: [] for team in teams}
    members = await session.execute(
        select(TeamMember.team_id, TeamMember.handle)
        .where(TeamMember.team_id.in_(rosters))
        .order_by(TeamMember.team_id, TeamMember.position)
    )
    for team_id, handle in members:
        rosters[team_id].append(handle)
    return [
        TeamOut(
            id=team.id,
            tournament_id=team.tournament_id,
            name=team.name,
            status=team.status,
            captain_id=team.captain_id,
            roster=rosters[team.id],
            hold_id=team.hold_id if private else None,
            created_at=team.created_at,
        )
        for team in teams
    ]


async def list_confirmed(session: AsyncSession, slug: str) -> list[TeamOut]:
    info = await catalog.get_sale_info(session, slug)
    return await _teams(
        session, Team.tournament_id == info.tournament_id, Team.status == "confirmed"
    )


async def confirmed_entrants(
    session: AsyncSession, tournament_id: uuid.UUID
) -> list[tuple[uuid.UUID, str]]:
    """Confirmed teams as (id, name), in the order they registered."""
    rows = await session.execute(
        select(Team.id, Team.name)
        .where(Team.tournament_id == tournament_id, Team.status == "confirmed")
        .order_by(Team.id)
    )
    return [(row.id, row.name) for row in rows]


async def headcount(session: AsyncSession, tournament_id: uuid.UUID) -> dict[str, int]:
    """Teams by status, plus how many captains are waiting, for the organizer."""
    rows = await session.execute(
        select(Team.status, func.count())
        .where(Team.tournament_id == tournament_id)
        .group_by(Team.status)
    )
    counts = {status: count for status, count in rows}
    waiting = await session.scalar(
        select(func.count())
        .select_from(WaitlistEntry)
        .where(WaitlistEntry.tournament_id == tournament_id, WaitlistEntry.status == "waiting")
    )
    return {
        "confirmed": counts.get("confirmed", 0),
        "pending": counts.get("pending", 0),
        "waitlist": int(waiting or 0),
    }


async def names(session: AsyncSession, team_ids: list[uuid.UUID]) -> dict[uuid.UUID, str]:
    if not team_ids:
        return {}
    rows = await session.execute(select(Team.id, Team.name).where(Team.id.in_(team_ids)))
    return {row.id: row.name for row in rows}


async def list_mine(session: AsyncSession, user_id: uuid.UUID) -> list[TeamOut]:
    return await _teams(
        session, Team.captain_id == user_id, Team.status != "withdrawn", private=True
    )


# --- leaving ----------------------------------------------------------------------


async def _withdraw(session: AsyncSession, team: Team) -> None:
    """Take a team out. The caller holds the team's row lock."""
    team.status = "withdrawn"
    # Its players are free to join another team, and its name can be reused.
    await session.execute(delete(TeamMember).where(TeamMember.team_id == team.id))
    await session.execute(
        update(WaitlistEntry)
        .where(WaitlistEntry.team_id == team.id, WaitlistEntry.status == "offered")
        .values(status="lapsed", note="The offered slot was not taken up in time.")
    )
    await session.flush()


async def withdraw(session: AsyncSession, user_id: uuid.UUID, team_id: uuid.UUID) -> None:
    """A captain gives up their team's place. The slot goes to the waitlist, or back on sale."""
    team = await session.scalar(
        select(Team).where(Team.id == team_id, Team.captain_id == user_id).with_for_update()
    )
    if team is None or team.status == "withdrawn":
        raise ProblemError(404, "team_not_found", "No such team.")
    slug = (await catalog.tournament_refs(session, [team.tournament_id]))[team.tournament_id].slug
    info = await catalog.get_sale_info(session, slug)
    if datetime.now(UTC) >= info.starts_at:
        raise ProblemError(409, "tournament_started", "The tournament has already started.")

    was_confirmed = team.status == "confirmed"
    await _withdraw(session, team)
    if was_confirmed:
        # The orders module cancels the entry ticket and returns the slot.
        await outbox.emit(
            session,
            TEAM_WITHDRAWN,
            {"hold_id": str(team.hold_id), "ticket_type_id": str(team.ticket_type_id)},
        )
    else:
        # Not paid yet: the slot is still only held. Let go of the hold now.
        await inventory.release_hold(session, team.hold_id)
    log.info("team_withdrawn", team_id=str(team.id))


async def release_abandoned(session: AsyncSession, *, limit: int = 200) -> int:
    """Withdraw pending teams whose slot hold lapsed without being paid for."""
    pending = list(
        await session.scalars(
            select(Team)
            .where(Team.status == "pending")
            .order_by(Team.id)
            .limit(limit)
            .with_for_update(skip_locked=True)
        )
    )
    closed = await inventory.closed_holds(session, [team.hold_id for team in pending])
    abandoned = [team for team in pending if team.hold_id in closed]
    for team in abandoned:
        await _withdraw(session, team)
    return len(abandoned)


# --- reacting to payment (event handlers) ------------------------------------------


async def on_order_paid(ctx: EventContext, payload: dict[str, Any]) -> None:
    """The entry fee was paid: the team is in. Safe to run twice."""
    hold_id = uuid.UUID(payload["hold_id"])
    async with ctx.sessionmaker.begin() as session:
        team_id = await session.scalar(
            update(Team)
            .where(Team.hold_id == hold_id, Team.status == "pending")
            .values(status="confirmed")
            .returning(Team.id)
        )
        if team_id is not None:
            await session.execute(
                update(WaitlistEntry)
                .where(WaitlistEntry.team_id == team_id, WaitlistEntry.status == "offered")
                .values(status="accepted")
            )
            log.info("team_confirmed", team_id=str(team_id))


async def on_order_closed(ctx: EventContext, payload: dict[str, Any]) -> None:
    """Payment failed, or the order expired: the pending team is out."""
    hold_id = uuid.UUID(payload["hold_id"])
    async with ctx.sessionmaker.begin() as session:
        team = await session.scalar(
            select(Team).where(Team.hold_id == hold_id, Team.status == "pending").with_for_update()
        )
        if team is not None:
            await _withdraw(session, team)


# --- the waitlist ---------------------------------------------------------------------


async def _entry_out(session: AsyncSession, entry: WaitlistEntry) -> WaitlistOut:
    position = None
    if entry.status == "waiting":
        ahead = await session.scalar(
            select(func.count())
            .select_from(WaitlistEntry)
            .where(
                WaitlistEntry.tournament_id == entry.tournament_id,
                WaitlistEntry.status == "waiting",
                WaitlistEntry.id < entry.id,
            )
        )
        position = int(ahead or 0) + 1
    offered_hold_id = None
    if entry.status == "offered" and entry.team_id is not None:
        offered_hold_id = await session.scalar(select(Team.hold_id).where(Team.id == entry.team_id))
    return WaitlistOut(
        id=entry.id,
        tournament_id=entry.tournament_id,
        name=entry.name,
        roster=list(entry.roster),
        status=entry.status,
        position=position,
        note=entry.note,
        offered_hold_id=offered_hold_id,
        created_at=entry.created_at,
    )


async def join_waitlist(
    session: AsyncSession, user_id: uuid.UUID, slug: str, data: Roster
) -> WaitlistOut:
    info = await catalog.get_sale_info(session, slug)
    slot = _slot_type(info)
    _check_open(info)
    _check_roster(info, data)
    if await inventory.counted_free(session, slot.id) > 0:
        raise ProblemError(
            409, "slots_available", "There are still slots left. Register your team directly."
        )

    entry = WaitlistEntry(
        id=uuid7(),
        tournament_id=info.tournament_id,
        captain_id=user_id,
        name=data.name,
        roster=list(data.roster),
        status="waiting",
    )
    try:
        async with session.begin_nested():
            session.add(entry)
    except IntegrityError as exc:
        if violated_constraint(exc) != "waitlist_one_live_idx":
            raise
        raise ProblemError(
            409, "already_waiting", "You are already on the waitlist for this tournament."
        ) from exc
    await session.refresh(entry)
    return await _entry_out(session, entry)


async def _my_entry(
    session: AsyncSession, user_id: uuid.UUID, slug: str, *, lock: bool = False
) -> WaitlistEntry:
    info = await catalog.get_sale_info(session, slug)
    query = (
        select(WaitlistEntry)
        .where(
            WaitlistEntry.tournament_id == info.tournament_id, WaitlistEntry.captain_id == user_id
        )
        .order_by(WaitlistEntry.id.desc())
        .limit(1)
    )
    entry = await session.scalar(query.with_for_update() if lock else query)
    if entry is None:
        raise ProblemError(404, "not_on_waitlist", "You are not on this tournament's waitlist.")
    return entry


async def my_waitlist_entry(session: AsyncSession, user_id: uuid.UUID, slug: str) -> WaitlistOut:
    return await _entry_out(session, await _my_entry(session, user_id, slug))


async def leave_waitlist(session: AsyncSession, user_id: uuid.UUID, slug: str) -> None:
    entry = await _my_entry(session, user_id, slug, lock=True)
    if entry.status == "waiting":
        entry.status = "left"


async def promote_waitlist(session: AsyncSession, settings: Settings, *, limit: int = 20) -> int:
    """Offer free slots to whoever is next in line. Returns how many offers were made.

    An offer is a team registration made on the captain's behalf, with a
    longer hold: they check it out like any other. If their roster no longer
    works (a player joined another team meanwhile) the entry lapses with the
    reason, and the slot is offered to the next captain in the same pass.
    """
    tournaments = await session.scalars(
        select(WaitlistEntry.tournament_id).where(WaitlistEntry.status == "waiting").distinct()
    )
    offers = 0
    for tournament_id in list(tournaments):
        slug = (await catalog.tournament_refs(session, [tournament_id]))[tournament_id].slug
        # One worker at a time per tournament, held until this transaction
        # ends. Locking only the entry is not enough: a second worker would
        # skip the locked first-in-line, take the next captain, and could
        # win the free slot ahead of them.
        if not await session.scalar(
            select(
                func.pg_try_advisory_xact_lock(
                    func.hashtextextended(f"waitlist:{tournament_id}", 0)
                )
            )
        ):
            continue  # another worker is promoting this tournament right now
        skipped: list[uuid.UUID] = []
        while offers < limit:
            # Next in line, in the order they joined.
            entry = await session.scalar(
                select(WaitlistEntry)
                .where(
                    WaitlistEntry.tournament_id == tournament_id,
                    WaitlistEntry.status == "waiting",
                    WaitlistEntry.id.not_in(skipped),
                )
                .order_by(WaitlistEntry.id)
                .limit(1)
                .with_for_update()
            )
            if entry is None:
                break
            try:
                async with session.begin_nested():
                    registration = await register(
                        session,
                        settings,
                        entry.captain_id,
                        slug,
                        TeamCreate(name=entry.name, roster=entry.roster),
                        from_waitlist=True,
                    )
            except ProblemError as exc:
                if exc.code in (
                    "sold_out",
                    "sales_closed",
                    "sales_not_open",
                    "registration_closed",
                ):
                    break  # nothing to offer in this tournament right now
                if exc.code == "hold_exists":
                    # The captain is in the middle of buying something else
                    # here. Keep their place; the slot goes to the next in
                    # line, and they are first again when another frees up.
                    skipped.append(entry.id)
                    continue
                # This captain cannot be offered a slot as things stand. Say why, move on.
                entry.status, entry.note = "lapsed", exc.detail
                await session.flush()
                continue
            entry.status, entry.team_id = "offered", registration.team.id
            await session.flush()
            await outbox.emit(
                session,
                WAITLIST_OFFERED,
                {
                    "subject_id": str(entry.id),
                    "user_id": str(entry.captain_id),
                    "hold_id": str(registration.hold.id),
                    "expires_at": registration.hold.expires_at.isoformat(),
                },
            )
            offers += 1
            log.info("waitlist_offered", entry_id=str(entry.id), slug=slug)
    return offers
