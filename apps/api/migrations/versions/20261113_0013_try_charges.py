"""A public demonstration of idempotency keys: charges anyone may try to duplicate.

Revision ID: 0013
Revises: 0012
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0013"
down_revision: str | None = "0012"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "try_charges",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("key", sa.Text(), nullable=False),
        sa.Column("amount_cents", sa.Integer(), nullable=False),
        sa.Column(
            "created_at",
            postgresql.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("try_charges_pkey")),
    )
    op.create_index(op.f("try_charges_key_idx"), "try_charges", ["key"], unique=False)


def downgrade() -> None:
    op.drop_index(op.f("try_charges_key_idx"), table_name="try_charges")
    op.drop_table("try_charges")
