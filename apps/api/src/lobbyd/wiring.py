"""Connects modules that cannot import each other directly."""

from lobbyd.catalog import hooks
from lobbyd.inventory import service as inventory
from lobbyd.notifications import service as notifications
from lobbyd.payments import service as payments
from lobbyd.platform import events


def wire_modules() -> None:
    # Publishing a tournament puts its inventory on sale, in the same transaction.
    hooks.on_publish(inventory.open_inventory)

    # Work done by workers, from events in the outbox.
    events.subscribe(payments.PAYMENT_REQUESTED, payments.handle_payment_requested)
    events.subscribe(payments.REFUND_REQUESTED, payments.handle_refund_requested)
    for topic, handler in notifications.HANDLERS.items():
        events.subscribe(topic, handler)
