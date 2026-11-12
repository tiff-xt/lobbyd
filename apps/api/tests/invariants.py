"""The statements that must be true of the booking tables whenever nothing is in flight.

Each query returns the rows that BREAK a rule, so an empty result is a pass.
These are the invariants listed in docs/data-model.md, plus cross-checks
between tables that no single constraint can express.
"""

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

CHECKS = {
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
    "an order total differs from its items": """
        SELECT o.id FROM orders o
        WHERE o.total_cents <> (
            SELECT coalesce(sum(quantity * unit_price_cents), 0)
            FROM order_items WHERE order_id = o.id)
    """,
    "a checked-out hold does not have exactly one order": """
        SELECT h.id FROM holds h
        WHERE h.status = 'converted'
          AND (SELECT count(*) FROM orders o WHERE o.hold_id = h.id) <> 1
    """,
    "an order exists for a hold that was not checked out": """
        SELECT o.id FROM orders o JOIN holds h ON h.id = o.hold_id WHERE h.status <> 'converted'
    """,
    "a user has more than one active hold for a tournament": """
        SELECT user_id, tournament_id FROM holds WHERE status = 'active'
        GROUP BY user_id, tournament_id HAVING count(*) > 1
    """,
}


async def violations(sessionmaker: async_sessionmaker[AsyncSession]) -> dict[str, list[object]]:
    broken: dict[str, list[object]] = {}
    async with sessionmaker() as session:
        for rule, query in CHECKS.items():
            rows = (await session.execute(text(query))).all()
            if rows:
                broken[rule] = [tuple(row) for row in rows[:5]]
    return broken


async def assert_invariants(sessionmaker: async_sessionmaker[AsyncSession]) -> None:
    broken = await violations(sessionmaker)
    assert not broken, broken
