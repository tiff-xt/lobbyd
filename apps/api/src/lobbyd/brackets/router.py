"""HTTP and WebSocket endpoints for brackets."""

import asyncio
import contextlib
import uuid

from fastapi import APIRouter, Depends, WebSocket, WebSocketDisconnect, status

from lobbyd.brackets import service
from lobbyd.brackets.schemas import BracketOut, MatchOut, ResultIn, VersionIn
from lobbyd.catalog import service as catalog
from lobbyd.identity.deps import CurrentUser
from lobbyd.platform.db import SessionDep
from lobbyd.platform.errors import ProblemError
from lobbyd.platform.live import Hub
from lobbyd.platform.ratelimit import READS, by_ip

router = APIRouter(tags=["brackets"])


@router.get(
    "/tournaments/{slug}/bracket",
    dependencies=[Depends(by_ip(READS))],
    summary="The bracket as it stands",
)
async def get_bracket(slug: str, session: SessionDep) -> BracketOut:
    return await service.get_bracket(session, slug)


@router.post(
    "/tournaments/{slug}/bracket",
    status_code=status.HTTP_201_CREATED,
    summary="Draw the bracket from the confirmed teams",
    description=(
        "Single elimination, seeded in registration order. With a number of teams that is "
        "not a power of two, the top seeds get a bye. A tournament has one bracket."
    ),
)
async def generate_bracket(slug: str, session: SessionDep, user: CurrentUser) -> BracketOut:
    return await service.generate(session, user, slug)


@router.post("/matches/{match_id}/start", summary="Mark a match as being played")
async def start_match(
    match_id: uuid.UUID, body: VersionIn, session: SessionDep, user: CurrentUser
) -> MatchOut:
    return await service.start(session, user, match_id, body.version)


@router.post(
    "/matches/{match_id}/result",
    summary="Record or correct a result",
    description=(
        "Send the `version` you last saw. If the match changed since, the answer is 409 "
        "`stale_version` and nothing is written. The winner moves on to the next match."
    ),
)
async def report_result(
    match_id: uuid.UUID, body: ResultIn, session: SessionDep, user: CurrentUser
) -> MatchOut:
    return await service.report(session, user, match_id, body)


@router.websocket("/tournaments/{slug}/live")
async def live_bracket(websocket: WebSocket, slug: str) -> None:
    """Bracket updates as they happen.

    First message: `{"type": "bracket", "bracket": {...} | null}`, the state
    right now. Then `{"type": "matches", "matches": [...]}` for each change,
    or `{"type": "reload"}` when the whole bracket should be fetched again.
    Every match carries a version: ignore one older than what you hold.

    The same connection carries seat news during a sale:
    `{"type": "availability", "taken": [seat ids], "freed": [seat ids]}`, a
    few times a second at most. It is a hint to redraw the map at once and
    to fetch availability again soon, not a replacement for fetching it.
    """
    hub: Hub = websocket.app.state.live
    await websocket.accept()
    try:
        async with websocket.app.state.sessionmaker() as session:
            info = await catalog.get_sale_info(session, slug)
            # Listen before reading. A change that lands between the two is
            # then delivered twice (harmless: versions) and never missed.
            subscription = hub.subscribe(str(info.tournament_id))
            try:
                bracket = await service.bracket_of(session, info.tournament_id)
            except BaseException:
                subscription.close()
                raise
    except ProblemError:
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION, reason="No such tournament")
        return

    async def forward() -> None:
        await websocket.send_json(
            {"type": "bracket", "bracket": bracket.model_dump(mode="json") if bracket else None}
        )
        async for message in subscription:
            await websocket.send_text(message)
        # The hub let go of us: we fell behind, or it lost its connection.
        await websocket.close(code=status.WS_1013_TRY_AGAIN_LATER, reason="Reconnect to resync")

    async def until_gone() -> None:
        with contextlib.suppress(WebSocketDisconnect):
            while True:
                await (
                    websocket.receive_text()
                )  # clients have nothing to say; this notices them leaving

    tasks = [asyncio.create_task(forward()), asyncio.create_task(until_gone())]
    try:
        await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    finally:
        subscription.close()
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
