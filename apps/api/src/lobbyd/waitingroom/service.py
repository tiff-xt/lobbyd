"""The virtual waiting room. See docs/adr/0005-waiting-room-and-rate-limits.md.

When a sale is expected to be oversubscribed, buyers do not hit the booking
endpoint directly. They join a queue, and are let through at a steady rate
the database can serve. Being let through means receiving a short-lived
signed token, which `POST /holds` then requires.

Everything here lives in Redis, on purpose: it is fast, and nothing in it is
needed for a sale to be correct. If Redis loses it, people lose their place in
a queue. They cannot lose a seat they hold, or gain one they should not have.

Fairness: everyone who arrives before sales open waits in a lobby, and at
opening time the lobby is drawn into the queue in random order. Arriving early
or having a faster connection earns nothing. Later arrivals join the back.
"""

import time
import uuid
from datetime import UTC, datetime, timedelta
from typing import Literal

import jwt
from pydantic import BaseModel, Field
from redis.asyncio import Redis

from lobbyd.catalog.service import SaleInfo
from lobbyd.config import Settings
from lobbyd.platform.errors import ProblemError

ACTIVE = "wr:active"  # tournaments with a waiting room in use
_AUDIENCE = "lobbyd-admission"
_ALGORITHM = "HS256"

# Join the queue. If the lobby has not been drawn yet, draw it first: SPOP
# removes a random member, so popping until empty is a shuffle. One script,
# so the draw and every arrival racing with it happen in a definite order
# and nobody who waited in the lobby can end up behind a late arrival.
# Returns {rank, admitted so far, queue length}.
_ENTER = """
local lobby, queue, seq, admitted = KEYS[1], KEYS[2], KEYS[3], KEYS[4]
while true do
  local member = redis.call('SPOP', lobby)
  if not member then break end
  redis.call('ZADD', queue, 'NX', redis.call('INCR', seq), member)
end
if redis.call('ZSCORE', queue, ARGV[1]) == false then
  redis.call('ZADD', queue, redis.call('INCR', seq), ARGV[1])
end
return {
  redis.call('ZRANK', queue, ARGV[1]),
  tonumber(redis.call('GET', admitted) or '0'),
  redis.call('ZCARD', queue)
}
"""

# Let more people through, by the clock. Time-based, not "N per call", so any
# number of workers can run it without admitting anyone twice as fast.
# The cursor may run a little ahead of the queue (ARGV[3]) so that when there
# is no congestion a new arrival is admitted at once, but not far ahead, or a
# later burst would all be waved through together.
_ADMIT = """
local queue, admitted, clock = KEYS[1], KEYS[2], KEYS[3]
local now, rate, headroom = tonumber(ARGV[1]), tonumber(ARGV[2]), tonumber(ARGV[3])
local last = tonumber(redis.call('GET', clock) or now)
local due = math.floor((now - last) * rate)
if redis.call('EXISTS', clock) == 0 then
  redis.call('SET', clock, now)
elseif due > 0 then
  redis.call('SET', clock, last + due / rate)
end
local cursor = tonumber(redis.call('GET', admitted) or '0')
local limit = redis.call('ZCARD', queue) + headroom
local moved = math.max(0, math.min(cursor + due, limit) - cursor)
if moved > 0 then redis.call('SET', admitted, cursor + moved) end
return moved
"""


class QueueStatus(BaseModel):
    status: Literal["not_required", "lobby", "queued", "admitted"]
    position: int | None = Field(default=None, description="1 means you are next")
    queue_length: int | None = None
    estimated_wait_seconds: int | None = None
    opens_at: datetime | None = Field(default=None, description="When the lobby is drawn")
    admission_token: str | None = Field(
        default=None, description="Send as X-Admission-Token when holding"
    )
    token_expires_at: datetime | None = None


def _keys(tournament_id: uuid.UUID) -> tuple[str, str, str, str, str]:
    base = f"wr:{tournament_id}"
    return f"{base}:lobby", f"{base}:queue", f"{base}:seq", f"{base}:admitted", f"{base}:clock"


async def join(
    redis: Redis, settings: Settings, info: SaleInfo, user_id: uuid.UUID, now: datetime
) -> QueueStatus:
    """Enter the lobby or the queue, or report where the caller stands. Safe to poll."""
    if not info.waiting_room:
        return QueueStatus(status="not_required")
    lobby, queue, seq, admitted, _ = _keys(info.tournament_id)
    await redis.sadd(ACTIVE, str(info.tournament_id))

    if now < info.sales_open_at:
        await redis.sadd(lobby, str(user_id))
        waiting: int = await redis.scard(lobby)
        return QueueStatus(status="lobby", opens_at=info.sales_open_at, queue_length=waiting)

    rank, cursor, length = await redis.eval(_ENTER, 4, lobby, queue, seq, admitted, str(user_id))
    if rank < cursor:
        expires = now + timedelta(seconds=settings.admission_ttl_seconds)
        return QueueStatus(
            status="admitted",
            admission_token=_sign(settings, user_id, info.tournament_id, expires),
            token_expires_at=expires,
        )
    position = rank - cursor + 1
    return QueueStatus(
        status="queued",
        position=position,
        queue_length=length - cursor,
        estimated_wait_seconds=max(1, round(position / settings.waiting_room_admit_per_second)),
    )


async def admit(redis: Redis, settings: Settings, *, now: float | None = None) -> int:
    """Let through whoever is due, in every active waiting room. Run about once a second."""
    moved = 0
    for tournament_id in await redis.smembers(ACTIVE):
        _, queue, _, admitted, clock = _keys(uuid.UUID(str(tournament_id)))
        rate = settings.waiting_room_admit_per_second
        moved += await redis.eval(
            _ADMIT, 3, queue, admitted, clock, now or time.time(), rate, max(1, int(rate))
        )
    return moved


def _sign(
    settings: Settings, user_id: uuid.UUID, tournament_id: uuid.UUID, expires: datetime
) -> str:
    payload = {"sub": str(user_id), "tid": str(tournament_id), "aud": _AUDIENCE, "exp": expires}
    return jwt.encode(payload, settings.jwt_secret.get_secret_value(), algorithm=_ALGORITHM)


def require_admission(
    settings: Settings, info: SaleInfo, user_id: uuid.UUID, token: str | None
) -> None:
    """Raise unless `token` admits this user to this tournament right now.

    Checking the signature needs no Redis call, so the booking path gains
    almost nothing in cost, and keeps working if Redis is having a bad day.
    """
    if not info.waiting_room or datetime.now(UTC) < info.sales_open_at:
        return  # no waiting room, or sales are not open (refused elsewhere)
    refusal = ProblemError(
        403,
        "admission_required",
        "This sale has a waiting room. Join the queue to get your turn.",
    )
    if not token:
        raise refusal
    try:
        claims = jwt.decode(
            token,
            settings.jwt_secret.get_secret_value(),
            algorithms=[_ALGORITHM],
            audience=_AUDIENCE,
            options={"require": ["sub", "tid", "exp"]},
        )
    except jwt.PyJWTError as exc:
        raise refusal from exc
    # A token is for one person and one sale: it cannot be passed on or reused elsewhere.
    if claims["sub"] != str(user_id) or claims["tid"] != str(info.tournament_id):
        raise refusal
