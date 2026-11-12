"""Double elimination: a losers bracket, and explicit slots for where teams go next.

Revision ID: 0010
Revises: 0009
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0010"
down_revision: str | None = "0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "matches", sa.Column("bracket", sa.Text(), server_default="winners", nullable=False)
    )
    op.add_column("matches", sa.Column("next_slot", sa.Text(), nullable=True))
    op.add_column("matches", sa.Column("loser_next_match_id", sa.Uuid(), nullable=True))
    op.add_column("matches", sa.Column("loser_next_slot", sa.Text(), nullable=True))
    # Until now the side a winner went to was implied by its position. Write
    # it down for the brackets that already exist.
    op.execute(
        "UPDATE matches SET next_slot = CASE WHEN position % 2 = 0 THEN 'a' ELSE 'b' END "
        "WHERE next_match_id IS NOT NULL"
    )
    op.create_check_constraint(
        op.f("matches_bracket_check"), "matches", "bracket IN ('winners', 'losers', 'final')"
    )
    op.drop_constraint(op.f("matches_tournament_id_round_position_key"), "matches", type_="unique")
    op.create_unique_constraint(
        op.f("matches_tournament_id_bracket_round_position_key"),
        "matches",
        ["tournament_id", "bracket", "round", "position"],
    )
    op.create_foreign_key(
        op.f("matches_loser_next_match_id_fkey"),
        "matches",
        "matches",
        ["loser_next_match_id"],
        ["id"],
    )


def downgrade() -> None:
    op.drop_constraint(op.f("matches_loser_next_match_id_fkey"), "matches", type_="foreignkey")
    op.drop_constraint(op.f("matches_bracket_check"), "matches", type_="check")
    op.execute("DELETE FROM matches WHERE bracket <> 'winners'")
    op.drop_constraint(
        op.f("matches_tournament_id_bracket_round_position_key"), "matches", type_="unique"
    )
    op.create_unique_constraint(
        op.f("matches_tournament_id_round_position_key"),
        "matches",
        ["tournament_id", "round", "position"],
        postgresql_nulls_not_distinct=False,
    )
    op.drop_column("matches", "loser_next_slot")
    op.drop_column("matches", "loser_next_match_id")
    op.drop_column("matches", "next_slot")
    op.drop_column("matches", "bracket")
