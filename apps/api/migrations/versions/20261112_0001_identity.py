"""Identity: users and refresh tokens.

Revision ID: 0001
Revises:
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS citext")

    op.create_table(
        "users",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("email", postgresql.CITEXT(), nullable=False),
        sa.Column("display_name", sa.Text(), nullable=False),
        sa.Column("password_hash", sa.Text(), nullable=False),
        sa.Column("role", sa.Text(), server_default="user", nullable=False),
        sa.Column(
            "created_at",
            postgresql.TIMESTAMP(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name="users_pkey"),
        sa.UniqueConstraint("email", name="users_email_key"),
        sa.CheckConstraint(
            "role IN ('user', 'organizer', 'staff', 'admin')", name="users_role_check"
        ),
    )

    op.create_table(
        "refresh_tokens",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("family_id", sa.Uuid(), nullable=False),
        sa.Column("token_hash", sa.LargeBinary(), nullable=False),
        sa.Column("expires_at", postgresql.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("used_at", postgresql.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("revoked_at", postgresql.TIMESTAMP(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            postgresql.TIMESTAMP(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name="refresh_tokens_pkey"),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name="refresh_tokens_user_id_fkey", ondelete="CASCADE"
        ),
        sa.UniqueConstraint("token_hash", name="refresh_tokens_token_hash_key"),
    )
    op.create_index("refresh_tokens_family_id_idx", "refresh_tokens", ["family_id"])
    op.create_index("refresh_tokens_user_id_idx", "refresh_tokens", ["user_id"])


def downgrade() -> None:
    op.drop_table("refresh_tokens")
    op.drop_table("users")
    # The citext extension is left in place: other database objects may use it.
