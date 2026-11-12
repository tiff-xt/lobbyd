"""Single-elimination brackets. The only code that touches the matches table.

Contrast with seats. There, contention is the normal case, so claims are
decided by row locks. A match result is entered rarely and by a couple of
people, so a version number is enough: an edit says which version it was
made against, and is refused if the match has changed since.

Every change is announced through the outbox, so the announcement commits
with the change. A worker publishes it to Redis, and every API instance
relays it to the browsers watching (see platform/live.py).
"""

import uuid
from typing import Any

import structlog
from sqlalchemy import insert, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from lobbyd.brackets.models import Match
from lobbyd.brackets.schemas import BracketOut, MatchOut, ResultIn, RoundOut, TeamRef
from lobbyd.catalog import service as catalog
from lobbyd.identity.models import User
from lobbyd.platform import live, outbox
from lobbyd.platform.db import violated_constraint
from lobbyd.platform.errors import ProblemError
from lobbyd.platform.events import EventContext
from lobbyd.platform.ids import uuid7
from lobbyd.teams import service as teams

log = structlog.get_logger()

BRACKET_CHANGED = "bracket.changed"
MAX_TEAMS = 256


def seed_order(size: int) -> list[int]:
    """Seeds in bracket order for `size` places (a power of two), 1-based.

    Built so the top seeds are as far apart as possible: 1 and 2 can only
    meet in the final, and when there are fewer teams than places the byes
    fall to the top seeds. For 8: [1, 8, 4, 5, 2, 7, 3, 6].
    """
    order = [1]
    while len(order) < size:
        pair_sum = 2 * len(order) + 1
        order = [seed for high in order for seed in (high, pair_sum - high)]
    return order


def round_name(round_: int, rounds: int) -> str:
    remaining = rounds - round_
    if remaining == 0:
        return "Final"
    if remaining == 1:
        return "Semifinals"
    if remaining == 2:
        return "Quarterfinals"
    return f"Round of {2 ** (remaining + 1)}"


# --- reading ---------------------------------------------------------------------


async def _render(session: AsyncSession, matches: list[Match]) -> list[MatchOut]:
    ids = {team for m in matches for team in (m.team_a_id, m.team_b_id) if team is not None}
    names = await teams.names(session, list(ids))

    def ref(team_id: uuid.UUID | None) -> TeamRef | None:
        return TeamRef(id=team_id, name=names[team_id]) if team_id is not None else None

    return [
        MatchOut(
            id=m.id,
            round=m.round,
            position=m.position,
            team_a=ref(m.team_a_id),
            team_b=ref(m.team_b_id),
            score_a=m.score_a,
            score_b=m.score_b,
            winner_id=m.winner_id,
            status=m.status,
            version=m.version,
            next_match_id=m.next_match_id,
        )
        for m in matches
    ]


async def bracket_of(session: AsyncSession, tournament_id: uuid.UUID) -> BracketOut | None:
    matches = list(
        await session.scalars(
            select(Match)
            .where(Match.tournament_id == tournament_id)
            .order_by(Match.round, Match.position)
        )
    )
    if not matches:
        return None
    rendered = await _render(session, matches)
    rounds = max(m.round for m in rendered)
    final = rendered[-1]
    champion = None
    if final.winner_id is not None:
        champion = (
            final.team_a if final.team_a and final.team_a.id == final.winner_id else final.team_b
        )
    return BracketOut(
        tournament_id=tournament_id,
        rounds=[
            RoundOut(
                round=number,
                name=round_name(number, rounds),
                matches=[m for m in rendered if m.round == number],
            )
            for number in range(1, rounds + 1)
        ],
        champion=champion,
    )


async def get_bracket(session: AsyncSession, slug: str) -> BracketOut:
    info = await catalog.get_sale_info(session, slug)
    bracket = await bracket_of(session, info.tournament_id)
    if bracket is None:
        raise ProblemError(404, "bracket_not_found", "This tournament has no bracket yet.")
    return bracket


# --- changing ---------------------------------------------------------------------


async def _announce(
    session: AsyncSession, tournament_id: uuid.UUID, matches: list[Match] | None
) -> None:
    """Tell everyone watching. `matches` None means "the whole bracket changed, fetch it"."""
    message: dict[str, Any] = {"type": "reload"}
    if matches is not None:
        rendered = await _render(session, matches)
        message = {"type": "matches", "matches": [m.model_dump(mode="json") for m in rendered]}
    await outbox.emit(
        session, BRACKET_CHANGED, {"tournament_id": str(tournament_id), "message": message}
    )


async def announce(ctx: EventContext, payload: dict[str, Any]) -> None:
    """Event handler: pass a committed change on to every API instance."""
    await live.publish(ctx.redis, payload["tournament_id"], payload["message"])


async def generate(session: AsyncSession, user: User, slug: str) -> BracketOut:
    """Draw the bracket from the confirmed teams, seeded in the order they registered."""
    managed = await catalog.require_owner(session, user, slug=slug)
    entrants = await teams.confirmed_entrants(session, managed.tournament_id)
    if len(entrants) < 2:
        raise ProblemError(409, "not_enough_teams", "A bracket needs at least two confirmed teams.")
    if len(entrants) > MAX_TEAMS:
        raise ProblemError(409, "too_many_teams", f"Brackets hold at most {MAX_TEAMS} teams.")

    size = 2
    while size < len(entrants):
        size *= 2
    rounds = size.bit_length() - 1
    ids = {
        (round_, position): uuid7()
        for round_ in range(1, rounds + 1)
        for position in range(size >> round_)
    }
    rows: dict[tuple[int, int], dict[str, Any]] = {
        (round_, position): {
            "id": match_id,
            "tournament_id": managed.tournament_id,
            "round": round_,
            "position": position,
            "team_a_id": None,
            "team_b_id": None,
            "winner_id": None,
            "status": "scheduled",
            "next_match_id": ids.get((round_ + 1, position // 2)),
        }
        for (round_, position), match_id in ids.items()
    }

    def team(seed: int) -> uuid.UUID | None:
        return entrants[seed - 1][0] if seed <= len(entrants) else None

    order = seed_order(size)
    for position in range(size // 2):
        match = rows[(1, position)]
        match["team_a_id"], match["team_b_id"] = (
            team(order[2 * position]),
            team(order[2 * position + 1]),
        )
        if match["team_b_id"] is None:
            # A bye: nobody to play, so the team goes straight through.
            match["winner_id"], match["status"] = match["team_a_id"], "completed"
            if rounds > 1:
                slot = "team_a_id" if position % 2 == 0 else "team_b_id"
                rows[(2, position // 2)][slot] = match["winner_id"]

    try:
        async with session.begin_nested():
            # Later rounds first: a match must exist before another can point at it.
            await session.execute(
                insert(Match), sorted(rows.values(), key=lambda row: -row["round"])
            )
    except IntegrityError as exc:
        if violated_constraint(exc) != "matches_tournament_id_round_position_key":
            raise
        raise ProblemError(409, "bracket_exists", "This tournament already has a bracket.") from exc
    await _announce(session, managed.tournament_id, None)
    log.info("bracket_generated", slug=slug, teams=len(entrants), rounds=rounds)
    bracket = await bracket_of(session, managed.tournament_id)
    assert bracket is not None  # noqa: S101
    return bracket


def _stale(current: int) -> ProblemError:
    return ProblemError(
        409,
        "stale_version",
        "This match was changed by someone else. Reload it and try again.",
        current_version=current,
    )


async def _load(session: AsyncSession, user: User, match_id: uuid.UUID) -> Match:
    match = await session.get(Match, match_id)
    if match is None:
        raise ProblemError(404, "match_not_found", "No such match.")
    await catalog.require_owner(session, user, id=match.tournament_id)
    return match


async def start(session: AsyncSession, user: User, match_id: uuid.UUID, version: int) -> MatchOut:
    match = await _load(session, user, match_id)
    if match.team_a_id is None or match.team_b_id is None:
        raise ProblemError(409, "match_not_ready", "Both teams are not known yet.")
    if match.status == "completed":
        raise ProblemError(409, "match_completed", "This match already has a result.")
    changed = await session.execute(
        update(Match)
        .where(Match.id == match_id, Match.version == version)
        .values(status="live", version=Match.version + 1)
        .returning(Match.id)
    )
    if changed.first() is None:
        raise _stale(match.version)
    await session.refresh(match)
    await _announce(session, match.tournament_id, [match])
    return (await _render(session, [match]))[0]


async def report(
    session: AsyncSession, user: User, match_id: uuid.UUID, data: ResultIn
) -> MatchOut:
    """Record a result (or correct one) and move the winner on."""
    match = await _load(session, user, match_id)
    if match.team_a_id is None or match.team_b_id is None:
        raise ProblemError(409, "match_not_ready", "Both teams are not known yet.")
    if data.score_a == data.score_b:
        raise ProblemError(422, "draw_not_allowed", "An elimination match needs a winner.")
    winner = match.team_a_id if data.score_a > data.score_b else match.team_b_id
    previous_winner, seen_version = match.winner_id, match.version

    # The optimistic lock. If the row changed since the caller read it, the
    # version no longer matches, nothing is updated, and we say so.
    changed = await session.execute(
        update(Match)
        .where(Match.id == match_id, Match.version == data.version)
        .values(
            score_a=data.score_a,
            score_b=data.score_b,
            winner_id=winner,
            status="completed",
            version=Match.version + 1,
        )
        .returning(Match.id)
    )
    if changed.first() is None:
        raise _stale(seen_version)
    await session.refresh(match)
    touched = [match]

    if match.next_match_id is not None and winner != previous_winner:
        following = await session.scalar(
            select(Match).where(Match.id == match.next_match_id).with_for_update()
        )
        assert following is not None  # noqa: S101
        if following.status == "completed":
            # Correcting this result would change who played a match that
            # has already been decided. That needs a person, not a cascade.
            raise ProblemError(
                409,
                "result_locked",
                "The next match has already been played, so this winner cannot change.",
            )
        if match.position % 2 == 0:
            following.team_a_id = winner
        else:
            following.team_b_id = winner
        following.version += 1
        await session.flush()
        touched.append(following)

    await _announce(session, match.tournament_id, touched)
    log.info("result_reported", match_id=str(match.id), winner=str(winner))
    return (await _render(session, [match]))[0]
