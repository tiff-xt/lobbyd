"""Connects modules that cannot import each other directly."""

from lobbyd.catalog import hooks
from lobbyd.inventory import service as inventory
from lobbyd.notifications import service as notifications
from lobbyd.orders import service as orders
from lobbyd.payments import service as payments
from lobbyd.platform import events
from lobbyd.teams import service as teams


def wire_modules() -> None:
    # Publishing a tournament puts its inventory on sale, in the same transaction.
    hooks.on_publish(inventory.open_inventory)

    # Work done by workers, from events in the outbox.
    events.subscribe(payments.PAYMENT_REQUESTED, payments.handle_payment_requested)
    events.subscribe(payments.REFUND_REQUESTED, payments.handle_refund_requested)
    for topic, handler in notifications.HANDLERS.items():
        events.subscribe(topic, handler)

    # A team is confirmed when its entry is paid for, and out if payment fails.
    events.subscribe("order.paid", teams.on_order_paid)
    for topic in ("order.cancelled", "order.expired"):
        events.subscribe(topic, teams.on_order_closed)
    # A confirmed team that withdraws gives its ticket and its slot back.
    events.subscribe(teams.TEAM_WITHDRAWN, orders.handle_team_withdrawn)
