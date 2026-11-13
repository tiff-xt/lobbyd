# oncely

Idempotency keys and a transactional outbox for async SQLAlchemy and Postgres.

Two small tools for the same problem: a network that retries.

- **Idempotency keys.** A client that times out sends the request again. The
  work runs once, and every retry gets the first answer, byte for byte.
- **Transactional outbox.** "Save the order, then publish an event" can do one
  without the other. Here the event is a row written in the same transaction
  as the change, and a relay publishes it afterwards.

About 400 lines, typed, no framework required. It was extracted from
[lobbyd](../../README.md), a ticketing backend, where it sits under every
purchase and every payment.

## Install

```bash
pip install "oncely[redis]"
```

The `redis` extra adds a Redis Streams publisher and consumer. Without it the
package needs only SQLAlchemy 2 and a Postgres driver such as asyncpg.

## Idempotency keys

Attach the table to your metadata, so your own migrations create it:

```python
from oncely import IdempotencyKeys, Refused, fingerprint

keys = IdempotencyKeys(Base.metadata)
```

Then wrap the work. This is a FastAPI route, but nothing here depends on it:

```python
@router.post("/orders", status_code=201)
async def create_order(
    body: NewOrder,
    request: Request,
    user: CurrentUser,
    session: Session,
    key: str = Header(alias="Idempotency-Key"),
):
    async def work() -> tuple[int, str]:
        seat = await claim_seat(session, body.seat_id)
        if seat is None:
            raise Refused(409, '{"code": "seat_taken"}')
        order = await place_order(session, user, seat)
        return 201, order.model_dump_json()

    outcome = await keys.run(
        session,
        scope=str(user.id),
        key=key,
        fingerprint=fingerprint(request.method, request.url.path, body.model_dump_json()),
        work=work,
    )
    return Response(outcome.body, outcome.status, media_type="application/json")
```

What you get:

| Situation | Result |
| --- | --- |
| First request | `work` runs. Its answer is stored in the same transaction as its writes. |
| Retry after success | The stored answer. `work` does not run. `outcome.replayed` is true. |
| Retry while the first is still running | Waits for the first to finish, then gets its answer. |
| `work` raises `Refused` | Its writes are undone, the refusal is stored, and retries get it again. |
| `work` raises anything else | Nothing is stored. The retry starts from scratch. |
| Same key, different request | `KeyReused` is raised. That is a client bug, not a retry. |

The caller owns the transaction. Commit, and the answer is permanent. Roll
back, and the key is free again. There is no state in which the work happened
but the key was not recorded.

`scope` keeps callers apart: two users who pick the same key never see each
other's responses. Run `keys.prune(session, older_than=timedelta(hours=24))`
on a timer to drop keys past your retry window.

## Transactional outbox

```python
from oncely import Outbox

outbox = Outbox(Base.metadata)

async with sessionmaker.begin() as session:
    order.status = "paid"
    await outbox.emit(session, "order.paid", {"order_id": str(order.id)})
# Committed together, or not at all.
```

A relay moves events out. It takes any async function as the publisher:

```python
from oncely.streams import stream_publisher

publish = stream_publisher(redis, "shop:events")

while True:
    if not await outbox.relay_once(sessionmaker, publish):
        await asyncio.sleep(0.2)
```

- Events leave oldest first.
- Run as many relays as you like. Rows are taken with `FOR UPDATE SKIP LOCKED`,
  so two relays never publish the same batch.
- If the publisher raises, nothing is marked and the batch is offered again.
- If the process dies between publishing and committing, the batch is published
  twice. Delivery is **at least once**, so handlers must be safe to repeat.
- `outbox.lag_seconds(session)` is the age of the oldest unpublished event. It
  is the one number worth an alert.

`headers` on `emit` carries context that is not part of the event's meaning,
such as a trace id, so a handler can continue the trace of the request that
caused it.

## Consuming from Redis Streams

```python
from oncely.streams import StreamConsumer

worker = StreamConsumer(redis, stream="shop:events", max_attempts=5)


async def send_receipt(event):
    await mail.send(event.payload["order_id"])


worker.subscribe("order.paid", send_receipt)

while True:
    await worker.run_once(block_ms=1000)
```

Each event goes to one worker in the group. If a handler raises, the event is
retried after `retry_after_ms`. If its worker dies, another takes it over.
After `max_attempts` it moves to `shop:events:dead`, so one bad message cannot
block the rest. `around` wraps each event's handling (a span, log context) and
`on_result` reports how each attempt ended (metrics).

## What it does not do

- **Exactly-once delivery.** Nothing does, across a network. The name refers to
  the work behind a key, which does run once.
- **Other databases.** It uses Postgres features on purpose: `ON CONFLICT`,
  `SKIP LOCKED`, partial indexes.
- **Migrations.** The tables join your metadata, and Alembic's autogenerate
  picks them up like any other.

## Tests

The suite runs against a real Postgres and Redis, including the races: twelve
retries of one key at once, and two relays competing for the same events.

```bash
ONCELY_TEST_DATABASE_URL=postgresql+asyncpg://user:pass@localhost/db \
ONCELY_TEST_REDIS_URL=redis://localhost:6379/14 \
pytest
```

It works inside its own Postgres schema (`oncely_test`) and drops it when done.

## License

MIT
