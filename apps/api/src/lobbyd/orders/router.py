"""HTTP endpoints for checkout and order history."""

import uuid

from fastapi import APIRouter, Depends, Request, Response, status

from lobbyd.identity.deps import CurrentUser, limit_per_user
from lobbyd.orders import payment_flow, service
from lobbyd.orders.schemas import OrderCreate, OrderOut
from lobbyd.payments.provider import SIGNATURE_HEADER
from lobbyd.platform.cache import RedisDep, cached, invalidate
from lobbyd.platform.db import SessionDep
from lobbyd.platform.deps import SettingsDep
from lobbyd.platform.idempotency import IdempotencyKeyHeader, run_idempotent
from lobbyd.platform.ratelimit import BOOKING

router = APIRouter()


@router.post(
    "/orders",
    status_code=status.HTTP_201_CREATED,
    response_model=OrderOut,
    dependencies=[Depends(limit_per_user(BOOKING))],
    tags=["booking"],
    summary="Check out a hold",
    description=(
        "Returns at once with the order `pending`. Payment completes in the background: "
        "fetch the order again until its status is `paid` (tickets attached), `cancelled` "
        "(payment refused), or `expired`. Retry safely with the same Idempotency-Key: you "
        "are never charged twice."
    ),
)
async def place_order(
    body: OrderCreate,
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
        work=lambda: service.place_order(
            session, settings, user.id, body.hold_id, body.payment_method
        ),
    )


@router.get(
    "/orders/{order_id}",
    response_model=OrderOut,
    tags=["booking"],
    summary="An order and its tickets",
)
async def get_order(
    order_id: uuid.UUID, session: SessionDep, redis: RedisDep, user: CurrentUser
) -> Response:
    async def load() -> str:
        return (await service.get_order(session, user.id, order_id)).model_dump_json()

    # After paying, a client asks for its order again and again until the
    # payment settles: during a sale this is the most frequent signed-in
    # request there is. The rendered order is cached, so most of those polls
    # cost one Redis read, not five queries. The key includes the user, so
    # nobody can be served someone else's order.
    #
    # A settled order is kept for thirty seconds. A pending one for barely
    # more than a second, however it got there. Every path that changes an
    # order drops the entry after committing, but that alone is not enough: a
    # read that began before the commit can finish after the drop and put
    # "pending" back. With a short life for pending, that stale copy lasts a
    # moment. (A load test caught this: orders paid in the database showed
    # as pending to their buyers for the full thirty seconds.)
    body = await cached(
        redis,
        service.cache_key(user.id, order_id),
        load,
        fresh_seconds=30,
        fresh_for=lambda order: 1.2 if '"status":"pending"' in order else 30,
    )
    return Response(body, media_type="application/json")


@router.get("/users/me/orders", tags=["booking"], summary="My orders, newest first")
async def list_my_orders(session: SessionDep, user: CurrentUser) -> list[OrderOut]:
    return await service.list_orders(session, user.id)


@router.post(
    "/webhooks/payments",
    tags=["webhooks"],
    summary="Payment provider callbacks",
    description="Signed by the provider. Safe to deliver more than once and in any order.",
)
async def payment_webhook(
    request: Request, redis: RedisDep, settings: SettingsDep
) -> dict[str, bool]:
    # Its own transaction, not the per-request one: the order's cached copy
    # must be dropped only after the change is committed. Dropping it before
    # would let a concurrent read put the old order straight back.
    async with request.app.state.sessionmaker.begin() as session:
        changed = await payment_flow.handle_webhook(
            session,
            settings,
            signature=request.headers.get(SIGNATURE_HEADER),
            body=await request.body(),
        )
    if changed:
        await invalidate(redis, changed)
    return {"received": True}
