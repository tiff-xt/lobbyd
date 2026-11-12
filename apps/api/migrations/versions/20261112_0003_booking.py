"""Booking: inventory, holds, orders, tickets, idempotency keys.

Revision ID: 0003
Revises: 0002
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "idempotency_keys",
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("key", sa.Text(), nullable=False),
        sa.Column("request_hash", sa.LargeBinary(), nullable=False),
        sa.Column("response_code", sa.Integer(), nullable=True),
        sa.Column("response_body", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            postgresql.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("user_id", "key", name=op.f("idempotency_keys_pkey")),
    )
    op.create_index(
        op.f("idempotency_keys_created_at_idx"), "idempotency_keys", ["created_at"], unique=False
    )
    op.create_table(
        "holds",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("tournament_id", sa.Uuid(), nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column("status", sa.Text(), server_default="active", nullable=False),
        sa.Column("expires_at", postgresql.TIMESTAMP(timezone=True), nullable=False),
        sa.Column(
            "created_at",
            postgresql.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "status IN ('active', 'converted', 'expired', 'released')",
            name=op.f("holds_status_check"),
        ),
        sa.ForeignKeyConstraint(
            ["tournament_id"], ["tournaments.id"], name=op.f("holds_tournament_id_fkey")
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], name=op.f("holds_user_id_fkey")),
        sa.PrimaryKeyConstraint("id", name=op.f("holds_pkey")),
    )
    op.create_index(
        "holds_expiry_idx",
        "holds",
        ["expires_at"],
        unique=False,
        postgresql_where=sa.text("status = 'active'"),
    )
    op.create_index(
        "holds_one_active_idx",
        "holds",
        ["user_id", "tournament_id"],
        unique=True,
        postgresql_where=sa.text("status = 'active'"),
    )
    op.create_table(
        "hold_items",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("hold_id", sa.Uuid(), nullable=False),
        sa.Column("ticket_type_id", sa.Uuid(), nullable=False),
        sa.Column("seat_id", sa.Uuid(), nullable=True),
        sa.Column("quantity", sa.Integer(), server_default="1", nullable=False),
        sa.Column("unit_price_cents", sa.Integer(), nullable=False),
        sa.CheckConstraint("quantity >= 1", name=op.f("hold_items_quantity_check")),
        sa.CheckConstraint(
            "seat_id IS NULL OR quantity = 1", name=op.f("hold_items_one_per_seat_check")
        ),
        sa.ForeignKeyConstraint(
            ["hold_id"], ["holds.id"], name=op.f("hold_items_hold_id_fkey"), ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["seat_id"], ["seats.id"], name=op.f("hold_items_seat_id_fkey")),
        sa.ForeignKeyConstraint(
            ["ticket_type_id"], ["ticket_types.id"], name=op.f("hold_items_ticket_type_id_fkey")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("hold_items_pkey")),
    )
    op.create_index("hold_items_hold_id_idx", "hold_items", ["hold_id"], unique=False)
    op.create_table(
        "inventory_counters",
        sa.Column("ticket_type_id", sa.Uuid(), nullable=False),
        sa.Column("tournament_id", sa.Uuid(), nullable=False),
        sa.Column("capacity", sa.Integer(), nullable=False),
        sa.Column("held_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("sold_count", sa.Integer(), server_default="0", nullable=False),
        sa.CheckConstraint("capacity >= 0", name=op.f("inventory_counters_capacity_check")),
        sa.CheckConstraint(
            "held_count + sold_count <= capacity", name=op.f("inventory_counters_no_oversell_check")
        ),
        sa.CheckConstraint("held_count >= 0", name=op.f("inventory_counters_held_check")),
        sa.CheckConstraint("sold_count >= 0", name=op.f("inventory_counters_sold_check")),
        sa.ForeignKeyConstraint(
            ["ticket_type_id"],
            ["ticket_types.id"],
            name=op.f("inventory_counters_ticket_type_id_fkey"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["tournament_id"],
            ["tournaments.id"],
            name=op.f("inventory_counters_tournament_id_fkey"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("ticket_type_id", name=op.f("inventory_counters_pkey")),
    )
    op.create_index(
        op.f("inventory_counters_tournament_id_idx"),
        "inventory_counters",
        ["tournament_id"],
        unique=False,
    )
    op.create_table(
        "orders",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("tournament_id", sa.Uuid(), nullable=False),
        sa.Column("hold_id", sa.Uuid(), nullable=False),
        sa.Column("status", sa.Text(), server_default="pending", nullable=False),
        sa.Column("total_cents", sa.Integer(), nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column(
            "created_at",
            postgresql.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("paid_at", postgresql.TIMESTAMP(timezone=True), nullable=True),
        sa.CheckConstraint(
            "status IN ('pending', 'paid', 'expired', 'cancelled', 'refunded')",
            name=op.f("orders_status_check"),
        ),
        sa.CheckConstraint("total_cents >= 0", name=op.f("orders_total_check")),
        sa.ForeignKeyConstraint(["hold_id"], ["holds.id"], name=op.f("orders_hold_id_fkey")),
        sa.ForeignKeyConstraint(
            ["tournament_id"], ["tournaments.id"], name=op.f("orders_tournament_id_fkey")
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], name=op.f("orders_user_id_fkey")),
        sa.PrimaryKeyConstraint("id", name=op.f("orders_pkey")),
        sa.UniqueConstraint("hold_id", name=op.f("orders_hold_id_key")),
    )
    op.create_index("orders_user_id_idx", "orders", ["user_id", "created_at"], unique=False)
    op.create_table(
        "tickets",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("order_id", sa.Uuid(), nullable=False),
        sa.Column("order_item_id", sa.Uuid(), nullable=False),
        sa.Column("tournament_id", sa.Uuid(), nullable=False),
        sa.Column("ticket_type_id", sa.Uuid(), nullable=False),
        sa.Column("seat_id", sa.Uuid(), nullable=True),
        sa.Column("holder_id", sa.Uuid(), nullable=False),
        sa.Column("status", sa.Text(), server_default="valid", nullable=False),
        sa.Column("checked_in_at", postgresql.TIMESTAMP(timezone=True), nullable=True),
        sa.Column(
            "issued_at",
            postgresql.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("status IN ('valid', 'void')", name=op.f("tickets_status_check")),
        sa.ForeignKeyConstraint(["holder_id"], ["users.id"], name=op.f("tickets_holder_id_fkey")),
        sa.ForeignKeyConstraint(["seat_id"], ["seats.id"], name=op.f("tickets_seat_id_fkey")),
        sa.ForeignKeyConstraint(
            ["ticket_type_id"], ["ticket_types.id"], name=op.f("tickets_ticket_type_id_fkey")
        ),
        sa.ForeignKeyConstraint(
            ["tournament_id"], ["tournaments.id"], name=op.f("tickets_tournament_id_fkey")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("tickets_pkey")),
    )
    op.create_index("tickets_holder_id_idx", "tickets", ["holder_id"], unique=False)
    op.create_index(
        "tickets_one_per_seat_idx",
        "tickets",
        ["tournament_id", "seat_id"],
        unique=True,
        postgresql_where=sa.text("status = 'valid' AND seat_id IS NOT NULL"),
    )
    op.create_index("tickets_order_id_idx", "tickets", ["order_id"], unique=False)
    op.create_table(
        "tournament_seats",
        sa.Column("tournament_id", sa.Uuid(), nullable=False),
        sa.Column("seat_id", sa.Uuid(), nullable=False),
        sa.Column("ticket_type_id", sa.Uuid(), nullable=False),
        sa.Column("status", sa.Text(), server_default="available", nullable=False),
        sa.Column("hold_id", sa.Uuid(), nullable=True),
        sa.Column("held_until", postgresql.TIMESTAMP(timezone=True), nullable=True),
        sa.CheckConstraint(
            "(status = 'held') = (hold_id IS NOT NULL) "
            "AND (hold_id IS NULL) = (held_until IS NULL)",
            name=op.f("tournament_seats_held_has_owner_check"),
        ),
        sa.CheckConstraint(
            "status IN ('available', 'held', 'sold', 'blocked')",
            name=op.f("tournament_seats_status_check"),
        ),
        sa.ForeignKeyConstraint(
            ["seat_id"], ["seats.id"], name=op.f("tournament_seats_seat_id_fkey")
        ),
        sa.ForeignKeyConstraint(
            ["ticket_type_id"],
            ["ticket_types.id"],
            name=op.f("tournament_seats_ticket_type_id_fkey"),
        ),
        sa.ForeignKeyConstraint(
            ["tournament_id"],
            ["tournaments.id"],
            name=op.f("tournament_seats_tournament_id_fkey"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("tournament_id", "seat_id", name=op.f("tournament_seats_pkey")),
    )
    op.create_index(
        "tournament_seats_held_until_idx",
        "tournament_seats",
        ["held_until"],
        unique=False,
        postgresql_where=sa.text("status = 'held'"),
    )
    op.create_index(
        "tournament_seats_hold_id_idx",
        "tournament_seats",
        ["hold_id"],
        unique=False,
        postgresql_where=sa.text("hold_id IS NOT NULL"),
    )
    op.create_table(
        "order_items",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("order_id", sa.Uuid(), nullable=False),
        sa.Column("ticket_type_id", sa.Uuid(), nullable=False),
        sa.Column("seat_id", sa.Uuid(), nullable=True),
        sa.Column("quantity", sa.Integer(), nullable=False),
        sa.Column("unit_price_cents", sa.Integer(), nullable=False),
        sa.CheckConstraint("quantity >= 1", name=op.f("order_items_quantity_check")),
        sa.ForeignKeyConstraint(
            ["order_id"], ["orders.id"], name=op.f("order_items_order_id_fkey"), ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["seat_id"], ["seats.id"], name=op.f("order_items_seat_id_fkey")),
        sa.ForeignKeyConstraint(
            ["ticket_type_id"], ["ticket_types.id"], name=op.f("order_items_ticket_type_id_fkey")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("order_items_pkey")),
    )
    op.create_index("order_items_order_id_idx", "order_items", ["order_id"], unique=False)


def downgrade() -> None:
    op.drop_index("order_items_order_id_idx", table_name="order_items")
    op.drop_table("order_items")
    op.drop_index(
        "tournament_seats_hold_id_idx",
        table_name="tournament_seats",
        postgresql_where=sa.text("hold_id IS NOT NULL"),
    )
    op.drop_index(
        "tournament_seats_held_until_idx",
        table_name="tournament_seats",
        postgresql_where=sa.text("status = 'held'"),
    )
    op.drop_table("tournament_seats")
    op.drop_index("tickets_order_id_idx", table_name="tickets")
    op.drop_index(
        "tickets_one_per_seat_idx",
        table_name="tickets",
        postgresql_where=sa.text("status = 'valid' AND seat_id IS NOT NULL"),
    )
    op.drop_index("tickets_holder_id_idx", table_name="tickets")
    op.drop_table("tickets")
    op.drop_index("orders_user_id_idx", table_name="orders")
    op.drop_table("orders")
    op.drop_index(op.f("inventory_counters_tournament_id_idx"), table_name="inventory_counters")
    op.drop_table("inventory_counters")
    op.drop_index("hold_items_hold_id_idx", table_name="hold_items")
    op.drop_table("hold_items")
    op.drop_index(
        "holds_one_active_idx", table_name="holds", postgresql_where=sa.text("status = 'active'")
    )
    op.drop_index(
        "holds_expiry_idx", table_name="holds", postgresql_where=sa.text("status = 'active'")
    )
    op.drop_table("holds")
    op.drop_index(op.f("idempotency_keys_created_at_idx"), table_name="idempotency_keys")
    op.drop_table("idempotency_keys")
