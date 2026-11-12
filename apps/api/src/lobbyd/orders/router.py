"""HTTP endpoints for checkout and order history."""

import uuid

from fastapi import APIRouter, Request, Response, status

from lobbyd.identity.deps import CurrentUser
from lobbyd.orders import payment_flow, service
from lobbyd.orders.schemas import OrderCreate, OrderOut
from lobbyd.payments.provider import SIGNATURE_HEADER
from lobbyd.platform.db import SessionDep
from lobbyd.platform.deps import SettingsDep
from lobbyd.platform.idempotency import IdempotencyKeyHeader, run_idempotent

router = APIRouter()


@router.post(
    "/orders",
    status_code=status.HTTP_201_CREATED,
    response_model=OrderOut,
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


@router.get("/orders/{order_id}", tags=["booking"], summary="An order and its tickets")
async def get_order(order_id: uuid.UUID, session: SessionDep, user: CurrentUser) -> OrderOut:
    return await service.get_order(session, user.id, order_id)


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
    request: Request, session: SessionDep, settings: SettingsDep
) -> dict[str, bool]:
    await payment_flow.handle_webhook(
        session,
        settings,
        signature=request.headers.get(SIGNATURE_HEADER),
        body=await request.body(),
    )
    return {"received": True}
