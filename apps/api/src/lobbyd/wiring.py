"""Connects modules that cannot import each other directly."""

from lobbyd.catalog import hooks
from lobbyd.inventory import service as inventory


def wire_modules() -> None:
    # Publishing a tournament puts its inventory on sale, in the same transaction.
    hooks.on_publish(inventory.open_inventory)
