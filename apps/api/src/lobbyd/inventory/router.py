"""HTTP endpoints for availability and holds."""

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Header, Request, Response, status

from lobbyd.identity.deps import CurrentUser, limit_per_user
from lobbyd.inventory import service
from lobbyd.inventory.schemas import Availability, HoldCreate, HoldOut
from lobbyd.platform.cache import RedisDep, cached
from lobbyd.platform.db import SessionDep
from lobbyd.platform.deps import SettingsDep
from lobbyd.platform.idempotency import IdempotencyKeyHeader, run_idempotent
from lobbyd.platform.ratelimit import BOOKING, READS, by_ip
from lobbyd.waitingroom.service import QueueStatus

router = APIRouter()


@router.get(
    "/tournaments/{slug}/availability",
    response_model=Availability,
    dependencies=[Depends(by_ip(READS))],
    tags=["booking"],
    summary="What is left: counts per ticket type and the seats already taken",
    description=(
        "Up to a few seconds old. That is safe: a claim is always checked against the "
        "database, so out-of-date availability can cause a refused hold, never a wrong sale."
    ),
)
async def get_availability(slug: str, session: SessionDep, redis: RedisDep) -> Response:
    async def load() -> str:
        return (await service.availability(session, slug)).model_dump_json()

    # During a sale every open seat map polls this. One second fresh, two
    # more while a single request refreshes it: the 12,000 seat scan runs
    # about once a second however many people are watching.
    body = await cached(redis, f"availability:{slug}", load, fresh_seconds=1, stale_seconds=2)
    return Response(body, media_type="application/json")


@router.post(
    "/tournaments/{slug}/queue",
    dependencies=[Depends(by_ip(READS))],
    tags=["booking"],
    summary="Join the waiting room, or check your place in it",
    description=(
        "Call again every second or two. `lobby` means sales have not opened; everyone "
        "there is placed in random order when they do. `queued` gives your position. "
        "`admitted` carries the token to send with your hold. Tournaments without a "
        "waiting room answer `not_required`."
    ),
)
async def join_queue(
    slug: str, session: SessionDep, redis: RedisDep, settings: SettingsDep, user: CurrentUser
) -> QueueStatus:
    return await service.queue_status(session, redis, settings, slug, user.id)


@router.post(
    "/holds",
    status_code=status.HTTP_201_CREATED,
    response_model=HoldOut,
    dependencies=[Depends(limit_per_user(BOOKING))],
    tags=["booking"],
    summary="Take seats or tickets off sale while you check out",
    description=(
        "All or nothing: if any seat is taken or any ticket type is short, nothing is held. "
        "One open hold per user per tournament. Retry safely with the same Idempotency-Key."
    ),
)
async def create_hold(
    body: HoldCreate,
    request: Request,
    session: SessionDep,
    redis: RedisDep,
    settings: SettingsDep,
    user: CurrentUser,
    key: IdempotencyKeyHeader,
    admission: Annotated[
        str | None,
        Header(
            alias="X-Admission-Token",
            description="Required for tournaments with a waiting room. From the queue endpoint.",
        ),
    ] = None,
) -> Response:
    return await run_idempotent(
        session,
        request,
        user_id=user.id,
        key=key,
        body=body,
        success_status=status.HTTP_201_CREATED,
        work=lambda: service.create_hold(session, settings, user.id, body, admission, redis),
    )


@router.get("/holds/{hold_id}", tags=["booking"], summary="A hold and what is in it")
async def get_hold(hold_id: uuid.UUID, session: SessionDep, user: CurrentUser) -> HoldOut:
    return await service.get_hold(session, user.id, hold_id)


@router.delete(
    "/holds/{hold_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    tags=["booking"],
    summary="Give a hold back early",
)
async def release_hold(hold_id: uuid.UUID, session: SessionDep, user: CurrentUser) -> None:
    await service.release_hold(session, hold_id, user_id=user.id)
