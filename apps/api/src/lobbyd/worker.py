"""Background housekeeping: `lobbyd-worker`.

Nothing here is needed for correctness. A lapsed hold's seats are claimable
the moment its time is up, and counters are reclaimed on demand. This loop
keeps the stored state tidy and returns counted inventory promptly. Phase 4
replaces it with a proper job queue.
"""

import asyncio
import contextlib
import signal

import structlog
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from lobbyd.config import get_settings
from lobbyd.inventory import service as inventory
from lobbyd.platform import idempotency
from lobbyd.platform.db import create_engine, create_sessionmaker
from lobbyd.platform.logging import configure_logging

log = structlog.get_logger()

SWEEP_EVERY_SECONDS = 5
PRUNE_EVERY_SECONDS = 3600


async def sweep(sessionmaker: async_sessionmaker[AsyncSession]) -> tuple[int, int]:
    """One pass. Small batches in their own transactions, so locks are short."""
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


async def run() -> None:
    settings = get_settings()
    configure_logging(json=settings.log_json)
    engine = create_engine(settings)
    sessionmaker = create_sessionmaker(engine)

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)

    log.info("worker_started")
    since_prune = PRUNE_EVERY_SECONDS  # prune on the first pass
    try:
        while not stop.is_set():
            try:
                expired, freed = await sweep(sessionmaker)
                if expired or freed:
                    log.info("sweep", holds_expired=expired, seats_freed=freed)
                if since_prune >= PRUNE_EVERY_SECONDS:
                    async with sessionmaker.begin() as session:
                        pruned = await idempotency.prune(session)
                    log.info("idempotency_keys_pruned", count=pruned)
                    since_prune = 0
            except Exception:
                # Keep going: the next pass will pick up whatever this one missed.
                log.exception("sweep_failed")
            since_prune += SWEEP_EVERY_SECONDS
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=SWEEP_EVERY_SECONDS)
    finally:
        await engine.dispose()
        log.info("worker_stopped")


def main() -> None:
    asyncio.run(run())


if __name__ == "__main__":
    main()
