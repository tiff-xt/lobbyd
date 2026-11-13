"""Background work: `lobbyd-worker`.

Four loops in one process:

- relay:        outbox table -> Redis stream
- consume:      Redis stream -> handlers (provider calls, emails)
- admit:        lets queued buyers through waiting rooms at a steady rate
- housekeeping: close lapsed holds and stale orders, poll the provider for
                payments that have gone quiet, prune, and reconcile

Any number of workers can run side by side. Every query that picks up work
uses SKIP LOCKED, and stream consumers share a group.
"""

import asyncio
import contextlib
import signal
from collections.abc import Awaitable, Callable

import structlog
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from lobbyd import reconcile, tryit
from lobbyd.config import get_settings
from lobbyd.inventory import service as inventory
from lobbyd.orders import payment_flow
from lobbyd.orders import service as orders
from lobbyd.payments.provider import PaysimProvider
from lobbyd.platform import events, idempotency, live, metrics, outbox, tracing
from lobbyd.platform.cache import create_redis, invalidate
from lobbyd.platform.db import create_engine, create_sessionmaker
from lobbyd.platform.events import EventContext
from lobbyd.platform.logging import configure_logging
from lobbyd.teams import service as teams
from lobbyd.waitingroom import service as waitingroom
from lobbyd.wiring import wire_modules

log = structlog.get_logger()

RELAY_IDLE_SECONDS = 0.25
CONSUME_BLOCK_MS = 1000
ADMIT_EVERY_SECONDS = 0.5
SWEEP_EVERY_SECONDS = 5
SLOW_EVERY_SECONDS = 300


async def sweep(sessionmaker: async_sessionmaker[AsyncSession]) -> tuple[int, int]:
    """Close lapsed holds and tidy their seats. Small batches, short transactions."""
    expired = freed = 0
    while True:
        async with sessionmaker.begin() as session:
            batch = await inventory.expire_due_holds(session)
        expired += batch
        if batch == 0:
            break
    while True:
        async with sessionmaker.begin() as session:
            batch = await inventory.free_lapsed_seats(session)
        freed += batch
        if batch == 0:
            break
    return expired, freed


async def _relay(ctx: EventContext) -> None:
    published = await outbox.relay_once(ctx.sessionmaker, ctx.redis)
    if published == 0:
        await asyncio.sleep(RELAY_IDLE_SECONDS)


async def _admit(ctx: EventContext) -> None:
    await waitingroom.admit(ctx.redis, ctx.settings)
    await asyncio.sleep(ADMIT_EVERY_SECONDS)


async def _consume(ctx: EventContext) -> None:
    await events.consume_once(ctx, block_ms=CONSUME_BLOCK_MS)


class _Housekeeping:
    def __init__(self) -> None:
        self.since_slow = SLOW_EVERY_SECONDS  # run the slow jobs on the first pass

    async def __call__(self, ctx: EventContext) -> None:
        expired, freed = await sweep(ctx.sessionmaker)
        async with ctx.sessionmaker.begin() as session:
            closed = await orders.expire_stale_orders(session)
        await invalidate(ctx.redis, *closed)
        stale = len(closed)
        moved = await payment_flow.poll_unsettled(ctx.sessionmaker, ctx.provider, redis=ctx.redis)
        # Teams whose slot hold lapsed step aside, and whoever is next on the
        # waitlist is offered what that freed.
        async with ctx.sessionmaker.begin() as session:
            await teams.release_abandoned(session)
        async with ctx.sessionmaker.begin() as session:
            await teams.promote_waitlist(session, ctx.settings)
        for what, count in (
            ("holds_expired", expired),
            ("seats_freed", freed),
            ("orders_expired", stale),
            ("payments_polled", moved),
        ):
            metrics.HOUSEKEEPING.labels(what).inc(count)
        async with ctx.sessionmaker() as session:
            metrics.OUTBOX_LAG.set(await outbox.lag_seconds(session))
        if expired or freed or stale or moved:
            log.info(
                "housekeeping",
                holds_expired=expired,
                seats_freed=freed,
                orders_expired=stale,
                payments_polled=moved,
            )

        if self.since_slow >= SLOW_EVERY_SECONDS:
            self.since_slow = 0
            async with ctx.sessionmaker.begin() as session:
                await idempotency.prune(session)
                await outbox.prune(session)
                await tryit.prune(session)
            await events.trim(ctx.redis)
            async with ctx.sessionmaker() as session:
                await reconcile.run(session)
        self.since_slow += SWEEP_EVERY_SECONDS
        await asyncio.sleep(SWEEP_EVERY_SECONDS)


async def _forever(
    name: str, step: Callable[[EventContext], Awaitable[None]], ctx: EventContext
) -> None:
    while True:
        try:
            await step(ctx)
        except asyncio.CancelledError:
            raise
        except Exception:
            # One bad pass must not stop the loop. Whatever it missed is
            # still in the database or the stream for the next pass.
            metrics.WORKER_LOOP_FAILURES.labels(name).inc()
            log.exception("worker_loop_failed", loop=name)
            await asyncio.sleep(1)


def start_loops(ctx: EventContext) -> list[asyncio.Task[None]]:
    """Start the four loops as tasks on the running event loop. Cancel them to stop."""
    return [
        asyncio.create_task(_forever("relay", _relay, ctx)),
        asyncio.create_task(_forever("consume", _consume, ctx)),
        asyncio.create_task(_forever("admit", _admit, ctx)),
        asyncio.create_task(_forever("housekeeping", _Housekeeping(), ctx)),
    ]


async def run() -> None:
    settings = get_settings()
    configure_logging(json=settings.log_json)
    tracing.configure(settings, "lobbyd-worker")
    if settings.worker_metrics_port:
        metrics.serve(settings.worker_metrics_port)
    wire_modules()
    engine = create_engine(settings)
    ctx = EventContext(
        sessionmaker=create_sessionmaker(engine),
        redis=create_redis(settings),
        settings=settings,
        provider=PaysimProvider.connect(settings.paysim_url),
    )

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)

    log.info("worker_started", consumer=ctx.consumer)
    live.feed.start(ctx.redis)  # the sweeper frees seats; viewers should hear
    tasks = start_loops(ctx)
    try:
        await stop.wait()
    finally:
        for task in tasks:
            task.cancel()
        for task in tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await task
        await live.feed.stop()
        await ctx.provider.aclose()
        await ctx.redis.aclose()
        await engine.dispose()
        tracing.shutdown()
        log.info("worker_stopped")


def main() -> None:
    asyncio.run(run())


if __name__ == "__main__":
    main()
