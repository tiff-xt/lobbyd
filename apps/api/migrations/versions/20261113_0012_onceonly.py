"""Idempotency keys and the outbox move to the onceonly package's column names.

A key is scoped by any string, not only a user id, and an event carries
general headers, of which the trace is one.

Revision ID: 0012
Revises: 0011
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0012"
down_revision: str | None = "0011"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.alter_column("idempotency_keys", "user_id", new_column_name="scope")
    op.alter_column("idempotency_keys", "scope", type_=sa.Text(), postgresql_using="scope::text")
    op.alter_column("outbox", "trace", new_column_name="headers")


def downgrade() -> None:
    op.alter_column("outbox", "headers", new_column_name="trace")
    op.alter_column("idempotency_keys", "scope", type_=sa.Uuid(), postgresql_using="scope::uuid")
    op.alter_column("idempotency_keys", "scope", new_column_name="user_id")
