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
            bracket=m.bracket,
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
            loser_next_match_id=m.loser_next_match_id,
        )
        for m in matches
    ]


_BRACKET_ORDER = {"winners": 0, "losers": 1, "final": 2}


def _round_names(bracket: str, numbers: list[int], double: bool) -> dict[int, str]:
    """What to call each round, given the rounds that actually have matches."""
    if bracket == "winners":
        names = {number: round_name(number, numbers[-1]) for number in numbers}
        if double:
            names[numbers[-1]] = "Winners final"
        return names
    if bracket == "losers":
        names = {number: f"Losers round {index}" for index, number in enumerate(numbers, 1)}
        names[numbers[-1]] = "Losers final"
        return names
    return {1: "Grand final", 2: "Grand final reset"}


async def bracket_of(session: AsyncSession, tournament_id: uuid.UUID) -> BracketOut | None:
    matches = list(await session.scalars(select(Match).where(Match.tournament_id == tournament_id)))
    if not matches:
        return None
    matches.sort(key=lambda m: (_BRACKET_ORDER[m.bracket], m.round, m.position))
    double = any(m.bracket == "final" for m in matches)
    rendered = await _render(session, matches)

    def winner_of(match: MatchOut) -> TeamRef | None:
        if match.winner_id is None:
            return None
        return match.team_a if match.team_a and match.team_a.id == match.winner_id else match.team_b

    champion = None
    if double:
        first, reset = (m for m in rendered if m.bracket == "final")
        if reset.winner_id is not None:
            champion = winner_of(reset)
        elif first.winner_id is not None and first.team_a and first.winner_id == first.team_a.id:
            # The team from the winners bracket had not lost yet, and still has not.
            champion = first.team_a
        if reset.team_a is None:
            # The reset is only played if the losers' champion wins the first
            # final. Until then it is not part of the picture.
            rendered = [m for m in rendered if m.id != reset.id]
    else:
        champion = winner_of(rendered[-1])

    rounds = []
    for bracket in _BRACKET_ORDER:
        numbers = sorted({m.round for m in rendered if m.bracket == bracket})
        if not numbers:
            continue
        names = _round_names(bracket, numbers, double)
        rounds += [
            RoundOut(
                bracket=bracket,
                round=number,
                name=names[number],
                matches=[m for m in rendered if m.bracket == bracket and m.round == number],
            )
            for number in numbers
        ]
    return BracketOut(
        tournament_id=tournament_id,
        format="double" if double else "single",
        rounds=rounds,
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


Key = tuple[int, int]  # (round, position) within one bracket
Source = tuple[str, Key]  # ("loser", winners match) or ("winner", losers match)


def plan(
    tournament_id: uuid.UUID, entrants: list[uuid.UUID], *, double: bool
) -> list[dict[str, Any]]:
    """Every match of a bracket, as rows ready to insert. Pure: no database.

    Winners bracket: `size` places (the next power of two), seeded so the
    best teams meet last. Missing teams become byes in round one.

    Losers bracket (double elimination): rounds alternate. An odd round
    plays the survivors of the last round against each other; an even round
    plays them against the teams that just lost in the winners bracket. A
    bye in the winners bracket sends nobody down, so some losers matches
    would have one team or none: those are not created at all, and whoever
    would have played in them is wired straight through to the next match.
    """
    size = 2
    while size < len(entrants):
        size *= 2
    rounds = size.bit_length() - 1

    def row(bracket: str, round_: int, position: int) -> dict[str, Any]:
        return {
            "id": uuid7(),
            "tournament_id": tournament_id,
            "bracket": bracket,
            "round": round_,
            "position": position,
            "team_a_id": None,
            "team_b_id": None,
            "winner_id": None,
            "status": "scheduled",
            "next_match_id": None,
            "next_slot": None,
            "loser_next_match_id": None,
            "loser_next_slot": None,
        }

    winners: dict[Key, dict[str, Any]] = {
        (round_, position): row("winners", round_, position)
        for round_ in range(1, rounds + 1)
        for position in range(size >> round_)
    }
    for (round_, position), match in winners.items():
        following = winners.get((round_ + 1, position // 2))
        if following is not None:
            match["next_match_id"] = following["id"]
            match["next_slot"] = "a" if position % 2 == 0 else "b"

    def team(seed: int) -> uuid.UUID | None:
        return entrants[seed - 1] if seed <= len(entrants) else None

    order = seed_order(size)
    byes: set[Key] = set()
    for position in range(size // 2):
        match = winners[(1, position)]
        match["team_a_id"], match["team_b_id"] = (
            team(order[2 * position]),
            team(order[2 * position + 1]),
        )
        if match["team_b_id"] is None:
            # A bye: nobody to play, so the team goes straight through.
            byes.add((1, position))
            match["winner_id"], match["status"] = match["team_a_id"], "completed"
            if rounds > 1:
                winners[(2, position // 2)][f"team_{match['next_slot']}_id"] = match["winner_id"]
    if not double:
        return list(winners.values())

    # Who would play in each losers match, before byes are taken into account.
    feeds: dict[Key, tuple[Source, Source]] = {}
    for position in range(size // 4):
        feeds[(1, position)] = (("loser", (1, 2 * position)), ("loser", (1, 2 * position + 1)))
    for stage in range(1, rounds):
        count = size >> (stage + 1)
        if stage >= 2:
            for position in range(count):
                feeds[(2 * stage - 1, position)] = (
                    ("winner", (2 * stage - 2, 2 * position)),
                    ("winner", (2 * stage - 2, 2 * position + 1)),
                )
        for position in range(count):
            # Reversed, so two teams that met in the winners bracket are kept
            # apart in the losers bracket for as long as possible.
            feeds[(2 * stage, position)] = (
                ("winner", (2 * stage - 1, position)),
                ("loser", (stage + 1, count - 1 - position)),
            )
    last: Source = ("winner", (2 * (rounds - 1), 0)) if rounds > 1 else ("loser", (1, 0))

    # Where each planned match's winner really comes from: itself if it is
    # played, the one team passing through it if not, or nobody.
    outcome: dict[Key, Source | None] = {}
    losers: dict[Key, dict[str, Any]] = {}

    def resolve(source: Source) -> Source | None:
        kind, key = source
        if kind == "loser":
            return None if key in byes else source
        return outcome[key]

    def connect(source: Source, target: dict[str, Any], slot: str) -> None:
        kind, key = source
        if kind == "loser":
            winners[key]["loser_next_match_id"], winners[key]["loser_next_slot"] = (
                target["id"],
                slot,
            )
        else:
            losers[key]["next_match_id"], losers[key]["next_slot"] = target["id"], slot

    for key in sorted(feeds):  # round by round, so feeders are settled first
        live = [found for found in map(resolve, feeds[key]) if found is not None]
        if len(live) == 2:
            losers[key] = row("losers", *key)
            for source, slot in zip(live, "ab", strict=True):
                connect(source, losers[key], slot)
            outcome[key] = ("winner", key)
        else:
            outcome[key] = live[0] if live else None

    final, reset = row("final", 1, 0), row("final", 2, 0)
    winners[(rounds, 0)]["next_match_id"], winners[(rounds, 0)]["next_slot"] = final["id"], "a"
    challenger = resolve(last)
    assert challenger is not None  # noqa: S101  (two teams always produce one loser)
    connect(challenger, final, "b")
    return [*winners.values(), *losers.values(), final, reset]


async def generate(
    session: AsyncSession, user: User, slug: str, *, double: bool = False
) -> BracketOut:
    """Draw the bracket from the confirmed teams, seeded in the order they registered."""
    managed = await catalog.require_owner(session, user, slug=slug)
    entrants = await teams.confirmed_entrants(session, managed.tournament_id)
    if len(entrants) < 2:
        raise ProblemError(409, "not_enough_teams", "A bracket needs at least two confirmed teams.")
    if len(entrants) > MAX_TEAMS:
        raise ProblemError(409, "too_many_teams", f"Brackets hold at most {MAX_TEAMS} teams.")

    rows = plan(managed.tournament_id, [team_id for team_id, _ in entrants], double=double)
    try:
        async with session.begin_nested():
            # A match must exist before another can point at it: finals
            # first, then each bracket from its last round back to its first.
            rows.sort(key=lambda r: (-_BRACKET_ORDER[r["bracket"]], -r["round"]))
            await session.execute(insert(Match), rows)
    except IntegrityError as exc:
        if violated_constraint(exc) != "matches_tournament_id_bracket_round_position_key":
            raise
        raise ProblemError(409, "bracket_exists", "This tournament already has a bracket.") from exc
    await _announce(session, managed.tournament_id, None)
    log.info("bracket_generated", slug=slug, teams=len(entrants), double=double)
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
    """Record a result (or correct one), move the winner on, and in double
    elimination send the loser to the losers bracket."""
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
    loser = match.team_b_id if winner == match.team_a_id else match.team_a_id

    def locked() -> ProblemError:
        # Correcting this result would change who played a match that has
        # already been decided. That needs a person, not a cascade.
        return ProblemError(
            409,
            "result_locked",
            "A match that follows from this one has already been played, "
            "so this winner cannot change.",
        )

    if winner != previous_winner and match.bracket == "final" and match.round == 1:
        # The grand final. The team from the winners bracket (side A) has not
        # lost yet, so beating them once only evens things up: the reset
        # match is then played, with the same two teams.
        reset = await session.scalar(
            select(Match)
            .where(
                Match.tournament_id == match.tournament_id,
                Match.bracket == "final",
                Match.round == 2,
            )
            .with_for_update()
        )
        assert reset is not None  # noqa: S101
        if reset.status == "completed":
            raise locked()
        needed = winner == match.team_b_id
        reset.team_a_id = match.team_a_id if needed else None
        reset.team_b_id = match.team_b_id if needed else None
        reset.status = "scheduled"
        reset.version += 1
        await session.flush()
        touched.append(reset)
    elif winner != previous_winner:
        moves = [
            (match.next_match_id, match.next_slot, winner),
            (match.loser_next_match_id, match.loser_next_slot, loser),
        ]
        # Locked in id order, like everything else that takes two locks.
        for target_id, slot, team_id in sorted((m for m in moves if m[0]), key=lambda m: str(m[0])):
            following = await session.scalar(
                select(Match).where(Match.id == target_id).with_for_update()
            )
            assert following is not None  # noqa: S101
            if following.status == "completed":
                raise locked()
            setattr(following, f"team_{slot}_id", team_id)
            following.version += 1
            touched.append(following)
        await session.flush()

    await _announce(session, match.tournament_id, touched)
    log.info("result_reported", match_id=str(match.id), winner=str(winner))
    return (await _render(session, [match]))[0]
