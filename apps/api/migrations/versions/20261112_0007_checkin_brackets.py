"""Door scans and bracket matches.

Revision ID: 0007
Revises: 0006
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "checkin_scans",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("ticket_id", sa.Uuid(), nullable=False),
        sa.Column("tournament_id", sa.Uuid(), nullable=False),
        sa.Column("device_id", sa.Text(), nullable=False),
        sa.Column("scanned_at", postgresql.TIMESTAMP(timezone=True), nullable=False),
        sa.Column(
            "synced_at",
            postgresql.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("result", sa.Text(), nullable=False),
        sa.Column("offline", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("clock_suspect", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.CheckConstraint(
            "result IN ('admitted', 'duplicate', 'void')", name=op.f("checkin_scans_result_check")
        ),
        sa.ForeignKeyConstraint(
            ["ticket_id"], ["tickets.id"], name=op.f("checkin_scans_ticket_id_fkey")
        ),
        sa.ForeignKeyConstraint(
            ["tournament_id"], ["tournaments.id"], name=op.f("checkin_scans_tournament_id_fkey")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("checkin_scans_pkey")),
    )
    op.create_index(
        "checkin_scans_flagged_idx",
        "checkin_scans",
        ["tournament_id", "scanned_at"],
        unique=False,
        postgresql_where=sa.text("result = 'duplicate' OR clock_suspect"),
    )
    op.create_index(
        "checkin_scans_one_admission_idx",
        "checkin_scans",
        ["ticket_id"],
        unique=True,
        postgresql_where=sa.text("result = 'admitted'"),
    )
    op.create_index("checkin_scans_ticket_id_idx", "checkin_scans", ["ticket_id"], unique=False)
    op.create_table(
        "matches",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tournament_id", sa.Uuid(), nullable=False),
        sa.Column("round", sa.Integer(), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("team_a_id", sa.Uuid(), nullable=True),
        sa.Column("team_b_id", sa.Uuid(), nullable=True),
        sa.Column("score_a", sa.Integer(), nullable=True),
        sa.Column("score_b", sa.Integer(), nullable=True),
        sa.Column("winner_id", sa.Uuid(), nullable=True),
        sa.Column("next_match_id", sa.Uuid(), nullable=True),
        sa.Column("status", sa.Text(), server_default="scheduled", nullable=False),
        sa.Column("version", sa.Integer(), server_default="0", nullable=False),
        sa.CheckConstraint(
            "(status = 'completed') = (winner_id IS NOT NULL)", name=op.f("matches_completed_check")
        ),
        sa.CheckConstraint(
            "status IN ('scheduled', 'live', 'completed')", name=op.f("matches_status_check")
        ),
        sa.CheckConstraint(
            "winner_id IS NULL OR winner_id = team_a_id OR winner_id = team_b_id",
            name=op.f("matches_winner_played_check"),
        ),
        sa.ForeignKeyConstraint(
            ["next_match_id"], ["matches.id"], name=op.f("matches_next_match_id_fkey")
        ),
        sa.ForeignKeyConstraint(["team_a_id"], ["teams.id"], name=op.f("matches_team_a_id_fkey")),
        sa.ForeignKeyConstraint(["team_b_id"], ["teams.id"], name=op.f("matches_team_b_id_fkey")),
        sa.ForeignKeyConstraint(
            ["tournament_id"], ["tournaments.id"], name=op.f("matches_tournament_id_fkey")
        ),
        sa.ForeignKeyConstraint(["winner_id"], ["teams.id"], name=op.f("matches_winner_id_fkey")),
        sa.PrimaryKeyConstraint("id", name=op.f("matches_pkey")),
        sa.UniqueConstraint(
            "tournament_id",
            "round",
            "position",
            name=op.f("matches_tournament_id_round_position_key"),
        ),
    )


def downgrade() -> None:
    op.drop_table("matches")
    op.drop_index("checkin_scans_ticket_id_idx", table_name="checkin_scans")
    op.drop_index(
        "checkin_scans_one_admission_idx",
        table_name="checkin_scans",
        postgresql_where=sa.text("result = 'admitted'"),
    )
    op.drop_index(
        "checkin_scans_flagged_idx",
        table_name="checkin_scans",
        postgresql_where=sa.text("result = 'duplicate' OR clock_suspect"),
    )
    op.drop_table("checkin_scans")
