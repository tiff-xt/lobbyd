"""Partial refunds.

Revision ID: 0009
Revises: 0008
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0009"
down_revision: str | None = "0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "refunds",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("payment_id", sa.Uuid(), nullable=False),
        sa.Column("order_id", sa.Uuid(), nullable=False),
        sa.Column("amount_cents", sa.Integer(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), server_default="pending", nullable=False),
        sa.Column(
            "created_at",
            postgresql.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("settled_at", postgresql.TIMESTAMP(timezone=True), nullable=True),
        sa.CheckConstraint("status IN ('pending', 'succeeded')", name=op.f("refunds_status_check")),
        sa.CheckConstraint("amount_cents > 0", name=op.f("refunds_amount_check")),
        sa.ForeignKeyConstraint(
            ["payment_id"], ["payments.id"], name=op.f("refunds_payment_id_fkey")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("refunds_pkey")),
        sa.UniqueConstraint("order_id", "reason", name=op.f("refunds_order_id_reason_key")),
    )
    op.create_index(
        "refunds_pending_idx",
        "refunds",
        ["created_at"],
        unique=False,
        postgresql_where=sa.text("status = 'pending'"),
    )


def downgrade() -> None:
    op.drop_index(
        "refunds_pending_idx", table_name="refunds", postgresql_where=sa.text("status = 'pending'")
    )
    op.drop_table("refunds")
