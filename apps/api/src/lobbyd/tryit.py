"""Try to charge twice: a public endpoint for seeing idempotency keys work.

No account and no inventory, so anyone can hammer it: send the same
Idempotency-Key as many times and as fast as you like, then ask how many
charges exist for it. The answer is one, because this goes through the same
`run_idempotent` that guards every real hold and order.

The charges table deliberately has no unique constraint on the key. If the
mechanism ever let a second request through, there would be a second row,
and the count below would say so.
"""

import asyncio
import uuid
from datetime import datetime
from typing import Literal

from fastapi import APIRouter, Request, Response, status
from pydantic import BaseModel, Field
from sqlalchemy import Integer, Text, delete, func, insert, select, text
from sqlalchemy.dialects.postgresql import TIMESTAMP
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, mapped_column

from lobbyd.platform.db import Base, SessionDep
from lobbyd.platform.idempotency import IdempotencyKeyHeader, run_idempotent
from lobbyd.platform.ids import uuid7
from lobbyd.platform.ratelimit import Limit, take

router = APIRouter(tags=["try it"])

# Every visitor's keys share one scope: there is no account to scope them by.
_SCOPE = uuid.UUID("00000000-0000-7000-8000-00000000d340")
# Per key, so one visitor's burst never spends another's allowance. Generous:
# the point is to be hammered.
_PER_KEY = Limit("try", capacity=80, per_second=4)
_PRICE_CENTS = 2500
# Long enough that a burst of clicks lands while the first is still running,
# which is the hard case: a retry racing the original, not following it.
_WORK_SECONDS = 0.25


class TryCharge(Base):
    __tablename__ = "try_charges"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid7)
    key: Mapped[str] = mapped_column(Text, index=True)
    amount_cents: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=func.now()
    )


class ChargeIn(BaseModel):
    item: Literal["ticket"] = "ticket"


class ChargeOut(BaseModel):
    charge_id: uuid.UUID
    amount_cents: int


class Tally(BaseModel):
    requests_received: int = Field(description="Every request the server saw for this key")
    charges_made: int = Field(description="Rows in the charges table for this key")


def _counter(key: str) -> str:
    return f"try:requests:{key}"


@router.post(
    "/try/charges",
    response_model=ChargeOut,
    status_code=status.HTTP_201_CREATED,
    summary="Charge for a ticket. Send it as many times as you like",
    description=(
        "A demonstration with no account and no real money. Every request with the same "
        "Idempotency-Key returns the first response; repeats carry `Idempotent-Replay: true`."
    ),
)
async def charge(
    body: ChargeIn, request: Request, session: SessionDep, key: IdempotencyKeyHeader
) -> Response:
    await take(request, _PER_KEY, key)
    redis = request.app.state.redis
    pipeline = redis.pipeline(transaction=False)
    pipeline.incr(_counter(key))
    pipeline.expire(_counter(key), 3600)
    await pipeline.execute()

    async def work() -> ChargeOut:
        await asyncio.sleep(_WORK_SECONDS)
        charge_id = uuid7()
        await session.execute(
            insert(TryCharge).values(id=charge_id, key=key, amount_cents=_PRICE_CENTS)
        )
        return ChargeOut(charge_id=charge_id, amount_cents=_PRICE_CENTS)

    return await run_idempotent(
        session,
        request,
        user_id=_SCOPE,
        key=key,
        body=body,
        success_status=status.HTTP_201_CREATED,
        work=work,
    )


@router.get(
    "/try/charges/{key}",
    response_model=Tally,
    summary="How many requests arrived for a key, and how many charges exist",
)
async def tally(key: str, request: Request, session: SessionDep) -> Tally:
    received = await request.app.state.redis.get(_counter(key))
    made = await session.scalar(
        select(func.count()).select_from(TryCharge).where(TryCharge.key == key)
    )
    return Tally(requests_received=int(received or 0), charges_made=made or 0)


async def prune(session: AsyncSession, *, older_than_hours: int = 24) -> None:
    await session.execute(
        delete(TryCharge).where(
            TryCharge.created_at < func.now() - text(f"interval '{int(older_than_hours)} hours'")
        )
    )
