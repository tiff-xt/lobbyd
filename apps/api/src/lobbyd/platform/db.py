"""Database engine, session handling, and the declarative base."""

from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import Depends, Request
from sqlalchemy import MetaData, event
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase

from lobbyd.config import Settings
from lobbyd.platform import metrics, tracing

# Explicit constraint names, so migrations can refer to them reliably.
NAMING_CONVENTION = {
    "ix": "%(table_name)s_%(column_0_N_name)s_idx",
    "uq": "%(table_name)s_%(column_0_N_name)s_key",
    "ck": "%(table_name)s_%(constraint_name)s_check",
    "fk": "%(table_name)s_%(column_0_N_name)s_fkey",
    "pk": "%(table_name)s_pkey",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)


def create_engine(settings: Settings) -> AsyncEngine:
    engine = create_async_engine(
        settings.database_url,
        pool_size=settings.db_pool_size,
        max_overflow=settings.db_max_overflow,
        pool_timeout=settings.db_pool_timeout_seconds,
        pool_pre_ping=True,
    )
    # Count connections as they are lent out and handed back. When "in use"
    # sits at "capacity", requests are queueing for the database.
    metrics.DB_POOL.labels("capacity").inc(settings.db_pool_size + settings.db_max_overflow)
    in_use = metrics.DB_POOL.labels("in_use")
    event.listen(engine.sync_engine, "checkout", lambda *_: in_use.inc())
    event.listen(engine.sync_engine, "checkin", lambda *_: in_use.dec())
    tracing.instrument_engine(engine)
    return engine


def create_sessionmaker(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    # expire_on_commit=False: objects stay readable after commit, which matters
    # because the response is built after the transaction ends.
    return async_sessionmaker(engine, expire_on_commit=False)


async def get_session(request: Request) -> AsyncIterator[AsyncSession]:
    """One transaction per request: commit if the handler returns, roll back if it raises."""
    sessionmaker: async_sessionmaker[AsyncSession] = request.app.state.sessionmaker
    async with sessionmaker() as session:
        try:
            yield session
            await session.commit()
        except BaseException:
            await session.rollback()
            raise


def violated_constraint(exc: IntegrityError) -> str | None:
    """Name of the constraint an IntegrityError tripped, to map it to a precise error."""
    cause = getattr(exc.orig, "__cause__", None)
    name = getattr(cause, "constraint_name", None)
    return name if isinstance(name, str) else None


# scope="function": the commit happens before the response is sent, so a client
# never sees a success for work that then fails to commit.
SessionDep = Annotated[AsyncSession, Depends(get_session, scope="function")]
