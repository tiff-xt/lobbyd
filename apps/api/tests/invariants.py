"""Assertions over lobbyd.reconcile, the cross-table rules."""

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from lobbyd import reconcile


async def assert_invariants(sessionmaker: async_sessionmaker[AsyncSession]) -> None:
    """The rules that must hold at every instant between transactions."""
    async with sessionmaker() as session:
        broken = await reconcile.violations(session, settled_grace=None)
    assert not broken, broken


async def assert_settled(sessionmaker: async_sessionmaker[AsyncSession]) -> None:
    """Those, plus the rules that hold once all background work has finished:
    nobody charged without tickets or a refund, nothing stuck, every paid
    order confirmed."""
    async with sessionmaker() as session:
        broken = await reconcile.violations(session, settled_grace="0 seconds")
    assert not broken, broken
