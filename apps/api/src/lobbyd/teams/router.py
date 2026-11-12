"""HTTP endpoints for teams and the waitlist."""

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Header, Request, Response, status

from lobbyd.identity.deps import CurrentUser, limit_per_user
from lobbyd.platform.cache import RedisDep
from lobbyd.platform.db import SessionDep
from lobbyd.platform.deps import SettingsDep
from lobbyd.platform.idempotency import IdempotencyKeyHeader, run_idempotent
from lobbyd.platform.ratelimit import BOOKING, READS, by_ip
from lobbyd.teams import service
from lobbyd.teams.schemas import Roster, TeamCreate, TeamOut, TeamRegistration, WaitlistOut

router = APIRouter()

_reads = [Depends(by_ip(READS))]
_booking = [Depends(limit_per_user(BOOKING))]


@router.post(
    "/tournaments/{slug}/teams",
    status_code=status.HTTP_201_CREATED,
    response_model=TeamRegistration,
    dependencies=_booking,
    tags=["teams"],
    summary="Register a team",
    description=(
        "Claims a team slot and records the roster, together or not at all. The team is "
        "`pending` until the returned hold is checked out. Retry safely with the same "
        "Idempotency-Key."
    ),
)
async def register_team(
    slug: str,
    body: TeamCreate,
    request: Request,
    session: SessionDep,
    redis: RedisDep,
    settings: SettingsDep,
    user: CurrentUser,
    key: IdempotencyKeyHeader,
    admission: Annotated[str | None, Header(alias="X-Admission-Token")] = None,
) -> Response:
    return await run_idempotent(
        session,
        request,
        user_id=user.id,
        key=key,
        body=body,
        success_status=status.HTTP_201_CREATED,
        work=lambda: service.register(
            session, settings, user.id, slug, body, admission_token=admission, redis=redis
        ),
    )


@router.get(
    "/tournaments/{slug}/teams",
    dependencies=_reads,
    tags=["teams"],
    summary="Teams confirmed for a tournament",
)
async def list_teams(slug: str, session: SessionDep) -> list[TeamOut]:
    return await service.list_confirmed(session, slug)


@router.get("/users/me/teams", tags=["teams"], summary="Teams I captain")
async def list_my_teams(session: SessionDep, user: CurrentUser) -> list[TeamOut]:
    return await service.list_mine(session, user.id)


@router.delete(
    "/teams/{team_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    tags=["teams"],
    summary="Withdraw a team",
    description=(
        "Frees the slot for the waitlist. If the entry fee was paid it is refunded; "
        "anything else bought in the same order stands."
    ),
)
async def withdraw_team(team_id: uuid.UUID, session: SessionDep, user: CurrentUser) -> None:
    await service.withdraw(session, user.id, team_id)


@router.post(
    "/tournaments/{slug}/waitlist",
    status_code=status.HTTP_201_CREATED,
    dependencies=_booking,
    tags=["teams"],
    summary="Join the waitlist when team slots are full",
)
async def join_waitlist(
    slug: str, body: Roster, session: SessionDep, user: CurrentUser
) -> WaitlistOut:
    return await service.join_waitlist(session, user.id, slug, body)


@router.get(
    "/tournaments/{slug}/waitlist/me",
    tags=["teams"],
    summary="My place on the waitlist",
    description="When a slot is offered, `offered_hold_id` is the hold to check out.",
)
async def my_waitlist_entry(slug: str, session: SessionDep, user: CurrentUser) -> WaitlistOut:
    return await service.my_waitlist_entry(session, user.id, slug)


@router.delete(
    "/tournaments/{slug}/waitlist/me",
    status_code=status.HTTP_204_NO_CONTENT,
    tags=["teams"],
    summary="Leave the waitlist",
)
async def leave_waitlist(slug: str, session: SessionDep, user: CurrentUser) -> None:
    await service.leave_waitlist(session, user.id, slug)
