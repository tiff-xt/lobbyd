"""Teams, rosters, and the waitlist.

Revision ID: 0006
Revises: 0005
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "teams",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tournament_id", sa.Uuid(), nullable=False),
        sa.Column("captain_id", sa.Uuid(), nullable=False),
        sa.Column("name", postgresql.CITEXT(), nullable=False),
        sa.Column("status", sa.Text(), server_default="pending", nullable=False),
        sa.Column("ticket_type_id", sa.Uuid(), nullable=False),
        sa.Column("hold_id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at",
            postgresql.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'confirmed', 'withdrawn')", name=op.f("teams_status_check")
        ),
        sa.ForeignKeyConstraint(["captain_id"], ["users.id"], name=op.f("teams_captain_id_fkey")),
        sa.ForeignKeyConstraint(["hold_id"], ["holds.id"], name=op.f("teams_hold_id_fkey")),
        sa.ForeignKeyConstraint(
            ["ticket_type_id"], ["ticket_types.id"], name=op.f("teams_ticket_type_id_fkey")
        ),
        sa.ForeignKeyConstraint(
            ["tournament_id"], ["tournaments.id"], name=op.f("teams_tournament_id_fkey")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("teams_pkey")),
        sa.UniqueConstraint("id", "tournament_id", name=op.f("teams_id_tournament_id_key")),
    )
    op.create_index(
        "teams_captain_idx",
        "teams",
        ["tournament_id", "captain_id"],
        unique=True,
        postgresql_where=sa.text("status <> 'withdrawn'"),
    )
    op.create_index("teams_hold_id_idx", "teams", ["hold_id"], unique=True)
    op.create_index(
        "teams_name_idx",
        "teams",
        ["tournament_id", "name"],
        unique=True,
        postgresql_where=sa.text("status <> 'withdrawn'"),
    )
    op.create_table(
        "team_members",
        sa.Column("team_id", sa.Uuid(), nullable=False),
        sa.Column("tournament_id", sa.Uuid(), nullable=False),
        sa.Column("handle", postgresql.CITEXT(), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(
            ["team_id", "tournament_id"],
            ["teams.id", "teams.tournament_id"],
            name=op.f("team_members_team_id_tournament_id_fkey"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("team_id", "handle", name=op.f("team_members_pkey")),
        sa.UniqueConstraint(
            "tournament_id", "handle", name=op.f("team_members_tournament_id_handle_key")
        ),
    )
    op.create_table(
        "waitlist_entries",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tournament_id", sa.Uuid(), nullable=False),
        sa.Column("captain_id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("roster", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("status", sa.Text(), server_default="waiting", nullable=False),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("team_id", sa.Uuid(), nullable=True),
        sa.Column(
            "created_at",
            postgresql.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "status IN ('waiting', 'offered', 'accepted', 'lapsed', 'left')",
            name=op.f("waitlist_entries_status_check"),
        ),
        sa.ForeignKeyConstraint(
            ["captain_id"], ["users.id"], name=op.f("waitlist_entries_captain_id_fkey")
        ),
        sa.ForeignKeyConstraint(
            ["team_id"], ["teams.id"], name=op.f("waitlist_entries_team_id_fkey")
        ),
        sa.ForeignKeyConstraint(
            ["tournament_id"], ["tournaments.id"], name=op.f("waitlist_entries_tournament_id_fkey")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("waitlist_entries_pkey")),
    )
    op.create_index(
        "waitlist_next_idx",
        "waitlist_entries",
        ["tournament_id", "id"],
        unique=False,
        postgresql_where=sa.text("status = 'waiting'"),
    )
    op.create_index(
        "waitlist_one_live_idx",
        "waitlist_entries",
        ["tournament_id", "captain_id"],
        unique=True,
        postgresql_where=sa.text("status IN ('waiting', 'offered')"),
    )


def downgrade() -> None:
    op.drop_index(
        "waitlist_one_live_idx",
        table_name="waitlist_entries",
        postgresql_where=sa.text("status IN ('waiting', 'offered')"),
    )
    op.drop_index(
        "waitlist_next_idx",
        table_name="waitlist_entries",
        postgresql_where=sa.text("status = 'waiting'"),
    )
    op.drop_table("waitlist_entries")
    op.drop_table("team_members")
    op.drop_index(
        "teams_name_idx", table_name="teams", postgresql_where=sa.text("status <> 'withdrawn'")
    )
    op.drop_index("teams_hold_id_idx", table_name="teams")
    op.drop_index(
        "teams_captain_idx", table_name="teams", postgresql_where=sa.text("status <> 'withdrawn'")
    )
    op.drop_table("teams")
