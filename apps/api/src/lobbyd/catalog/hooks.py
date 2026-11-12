"""Lets modules above the catalog react when a tournament is published.

The catalog cannot import them (dependencies point downward only), so they
register here at startup. Hooks run inside the publishing transaction: if one
fails, the tournament is not published.
"""

from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING

from sqlalchemy.ext.asyncio import AsyncSession

if TYPE_CHECKING:
    from lobbyd.catalog.service import SaleInfo

PublishHook = Callable[[AsyncSession, "SaleInfo"], Awaitable[None]]

_publish_hooks: list[PublishHook] = []


def on_publish(hook: PublishHook) -> None:
    if hook not in _publish_hooks:
        _publish_hooks.append(hook)


async def run_publish_hooks(session: AsyncSession, info: "SaleInfo") -> None:
    for hook in _publish_hooks:
        await hook(session, info)
