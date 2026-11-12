"""HTTP endpoints for availability and holds."""

import uuid

from fastapi import APIRouter, Request, Response, status

from lobbyd.identity.deps import CurrentUser
from lobbyd.inventory import service
from lobbyd.inventory.schemas import Availability, HoldCreate, HoldOut
from lobbyd.platform.db import SessionDep
from lobbyd.platform.deps import SettingsDep
from lobbyd.platform.idempotency import IdempotencyKeyHeader, run_idempotent

router = APIRouter()


@router.get(
    "/tournaments/{slug}/availability",
    tags=["booking"],
    summary="What is left: counts per ticket type and the seats already taken",
)
async def get_availability(slug: str, session: SessionDep) -> Availability:
    return await service.availability(session, slug)


@router.post(
    "/holds",
    status_code=status.HTTP_201_CREATED,
    response_model=HoldOut,
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
    settings: SettingsDep,
    user: CurrentUser,
    key: IdempotencyKeyHeader,
) -> Response:
    return await run_idempotent(
        session,
        request,
        user_id=user.id,
        key=key,
        body=body,
        success_status=status.HTTP_201_CREATED,
        work=lambda: service.create_hold(session, settings, user.id, body),
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
    await service.release_hold(session, user.id, hold_id)
