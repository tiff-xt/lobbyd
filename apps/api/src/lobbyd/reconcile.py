"""Cross-checks between tables that no single constraint can express.

Each query returns the rows that BREAK a rule, so an empty result is a pass.
The test suite runs these after every concurrency test; the worker runs them
on a schedule and logs loudly if anything turns up. Read-only, and the one
place allowed to look across every module's tables.

STRICT rules hold at every instant between transactions. SETTLED rules hold
once background work has caught up (events relayed, refunds completed), so
the worker only reports them for rows that have been wrong for a while.
"""

import structlog
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

log = structlog.get_logger()

STRICT = {
    "a counter holds or has sold more than its capacity": """
        SELECT ticket_type_id FROM inventory_counters
        WHERE held_count + sold_count > capacity OR held_count < 0 OR sold_count < 0
    """,
    "held_count differs from what active holds add up to": """
        SELECT c.ticket_type_id, c.held_count, coalesce(h.held, 0)
        FROM inventory_counters c
        LEFT JOIN (
            SELECT i.ticket_type_id, sum(i.quantity) AS held
            FROM hold_items i JOIN holds h ON h.id = i.hold_id
            WHERE h.status = 'active' AND i.seat_id IS NULL
            GROUP BY i.ticket_type_id
        ) h USING (ticket_type_id)
        WHERE c.held_count <> coalesce(h.held, 0)
    """,
    "sold_count differs from the number of valid tickets": """
        SELECT c.ticket_type_id, c.sold_count, coalesce(t.sold, 0)
        FROM inventory_counters c
        LEFT JOIN (
            SELECT ticket_type_id, count(*) AS sold FROM tickets
            WHERE status = 'valid' AND seat_id IS NULL GROUP BY ticket_type_id
        ) t USING (ticket_type_id)
        WHERE c.sold_count <> coalesce(t.sold, 0)
    """,
    "a seat has more than one valid ticket": """
        SELECT tournament_id, seat_id FROM tickets
        WHERE status = 'valid' AND seat_id IS NOT NULL
        GROUP BY tournament_id, seat_id HAVING count(*) > 1
    """,
    "a sold seat has no valid ticket": """
        SELECT s.tournament_id, s.seat_id FROM tournament_seats s
        WHERE s.status = 'sold' AND NOT EXISTS (
            SELECT 1 FROM tickets t
            WHERE t.tournament_id = s.tournament_id AND t.seat_id = s.seat_id
              AND t.status = 'valid')
    """,
    "a valid ticket is for a seat that is not sold": """
        SELECT t.id FROM tickets t
        JOIN tournament_seats s ON s.tournament_id = t.tournament_id AND s.seat_id = t.seat_id
        WHERE t.status = 'valid' AND s.status <> 'sold'
    """,
    "a held seat belongs to a hold that was checked out or released": """
        SELECT s.seat_id, h.status FROM tournament_seats s JOIN holds h ON h.id = s.hold_id
        WHERE s.status = 'held' AND h.status IN ('converted', 'released')
    """,
    "an active hold in time does not own all of its seats": """
        SELECT h.id FROM holds h JOIN hold_items i ON i.hold_id = h.id
        LEFT JOIN tournament_seats s
               ON s.tournament_id = h.tournament_id AND s.seat_id = i.seat_id AND s.hold_id = h.id
        WHERE h.status = 'active' AND h.expires_at > now()
          AND i.seat_id IS NOT NULL AND s.seat_id IS NULL
    """,
    "a paid order's tickets do not match what was bought": """
        SELECT o.id FROM orders o
        WHERE o.status = 'paid'
          AND (SELECT coalesce(sum(quantity), 0) FROM order_items WHERE order_id = o.id)
           <> (SELECT count(*) FROM tickets WHERE order_id = o.id AND status = 'valid')
    """,
    "an order that is not paid has tickets": """
        SELECT o.id FROM orders o
        WHERE o.status <> 'paid'
          AND EXISTS (SELECT 1 FROM tickets t WHERE t.order_id = o.id AND t.status = 'valid')
    """,
    "an order total differs from its items": """
        SELECT o.id FROM orders o
        WHERE o.total_cents <> (
            SELECT coalesce(sum(quantity * unit_price_cents), 0)
            FROM order_items WHERE order_id = o.id)
    """,
    "a checked-out hold does not belong to exactly one paid order": """
        SELECT h.id FROM holds h
        WHERE h.status = 'converted'
          AND (SELECT count(*) FROM orders o WHERE o.hold_id = h.id AND o.status = 'paid') <> 1
    """,
    "a paid order's hold was not checked out": """
        SELECT o.id FROM orders o JOIN holds h ON h.id = o.hold_id
        WHERE o.status = 'paid' AND h.status <> 'converted'
    """,
    "a user has more than one active hold for a tournament": """
        SELECT user_id, tournament_id FROM holds WHERE status = 'active'
        GROUP BY user_id, tournament_id HAVING count(*) > 1
    """,
    "a paid order that cost money has no succeeded payment": """
        SELECT o.id FROM orders o
        WHERE o.status = 'paid' AND o.total_cents > 0 AND NOT EXISTS (
            SELECT 1 FROM payments p WHERE p.order_id = o.id AND p.status = 'succeeded')
    """,
    "a payment's amount differs from its order's total": """
        SELECT p.id FROM payments p JOIN orders o ON o.id = p.order_id
        WHERE p.amount_cents <> o.total_cents OR p.currency <> o.currency
    """,
}

# {grace} is replaced by how long a row may be in this state before it counts.
SETTLED = {
    "money was taken for an order that is not paid, and no refund is under way": """
        SELECT p.id FROM payments p JOIN orders o ON o.id = p.order_id
        WHERE p.status = 'succeeded' AND o.status <> 'paid'
          AND p.updated_at < now() - interval '{grace}'
    """,
    "an order is still pending although its hold is closed": """
        SELECT o.id FROM orders o JOIN holds h ON h.id = o.hold_id
        WHERE o.status = 'pending' AND h.status IN ('expired', 'released')
          AND o.created_at < now() - interval '{grace}'
    """,
    "a payment or refund has been waiting on the provider": """
        SELECT id FROM payments
        WHERE status IN ('created', 'processing', 'refund_pending')
          AND updated_at < now() - interval '{grace}'
    """,
    "an event has been sitting unpublished in the outbox": """
        SELECT id FROM outbox
        WHERE published_at IS NULL AND created_at < now() - interval '{grace}'
    """,
    "a paid order has no confirmation": """
        SELECT o.id FROM orders o
        WHERE o.status = 'paid' AND o.paid_at < now() - interval '{grace}' AND NOT EXISTS (
            SELECT 1 FROM notifications n
            WHERE n.subject_id = o.id AND n.kind = 'order_confirmation')
    """,
}


async def violations(
    session: AsyncSession, *, settled_grace: str | None = "15 minutes"
) -> dict[str, list[tuple[object, ...]]]:
    """Rules that are broken, each with a few offending rows.

    `settled_grace=None` skips the SETTLED rules; "0 seconds" demands they
    hold right now (tests, after draining all background work).
    """
    checks = dict(STRICT)
    if settled_grace is not None:
        checks.update({rule: sql.format(grace=settled_grace) for rule, sql in SETTLED.items()})
    broken: dict[str, list[tuple[object, ...]]] = {}
    for rule, sql in checks.items():
        rows = (await session.execute(text(sql + " LIMIT 5"))).all()
        if rows:
            broken[rule] = [tuple(row) for row in rows]
    return broken


async def run(session: AsyncSession) -> int:
    broken = await violations(session)
    for rule, rows in broken.items():
        # This is the alert. If it ever fires, something is badly wrong.
        log.error("invariant_violated", rule=rule, examples=[str(row) for row in rows])
    return len(broken)
