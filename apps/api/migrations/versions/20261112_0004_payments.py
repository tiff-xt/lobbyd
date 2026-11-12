"""Payments, provider events, outbox, notifications.

Revision ID: 0004
Revises: 0003
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "outbox",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("topic", sa.Text(), nullable=False),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "created_at",
            postgresql.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("published_at", postgresql.TIMESTAMP(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id", name=op.f("outbox_pkey")),
    )
    op.create_index(
        "outbox_pending_idx",
        "outbox",
        ["id"],
        unique=False,
        postgresql_where=sa.text("published_at IS NULL"),
    )
    op.create_table(
        "payment_events",
        sa.Column("provider_event_id", sa.Text(), nullable=False),
        sa.Column("payment_id", sa.Uuid(), nullable=True),
        sa.Column("type", sa.Text(), nullable=False),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "received_at",
            postgresql.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("provider_event_id", name=op.f("payment_events_pkey")),
    )
    op.create_table(
        "payments",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("order_id", sa.Uuid(), nullable=False),
        sa.Column("provider_ref", sa.Text(), nullable=True),
        sa.Column("method", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), server_default="created", nullable=False),
        sa.Column("amount_cents", sa.Integer(), nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column(
            "created_at",
            postgresql.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            postgresql.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "status IN ('created', 'processing', 'succeeded', 'failed', "
            "'refund_pending', 'refunded')",
            name=op.f("payments_status_check"),
        ),
        sa.CheckConstraint("amount_cents > 0", name=op.f("payments_amount_check")),
        sa.PrimaryKeyConstraint("id", name=op.f("payments_pkey")),
        sa.UniqueConstraint("order_id", name=op.f("payments_order_id_key")),
        sa.UniqueConstraint("provider_ref", name=op.f("payments_provider_ref_key")),
    )
    op.create_index(
        "payments_unsettled_idx",
        "payments",
        ["updated_at"],
        unique=False,
        postgresql_where=sa.text("status IN ('created', 'processing', 'refund_pending')"),
    )
    op.create_table(
        "notifications",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("subject_id", sa.Uuid(), nullable=False),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "created_at",
            postgresql.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name=op.f("notifications_user_id_fkey"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("notifications_pkey")),
        sa.UniqueConstraint("kind", "subject_id", name=op.f("notifications_kind_subject_id_key")),
    )


def downgrade() -> None:
    op.drop_table("notifications")
    op.drop_index(
        "payments_unsettled_idx",
        table_name="payments",
        postgresql_where=sa.text("status IN ('created', 'processing', 'refund_pending')"),
    )
    op.drop_table("payments")
    op.drop_table("payment_events")
    op.drop_index(
        "outbox_pending_idx", table_name="outbox", postgresql_where=sa.text("published_at IS NULL")
    )
    op.drop_table("outbox")
