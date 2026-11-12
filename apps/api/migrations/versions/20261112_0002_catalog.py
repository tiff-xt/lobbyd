"""Catalog: venues, seat maps, tournaments, ticket types.

Revision ID: 0002
Revises: 0001
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "venues",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("owner_id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("city", sa.Text(), nullable=False),
        sa.Column("timezone", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            postgresql.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["owner_id"], ["users.id"], name=op.f("venues_owner_id_fkey")),
        sa.PrimaryKeyConstraint("id", name=op.f("venues_pkey")),
    )
    op.create_index(op.f("venues_owner_id_idx"), "venues", ["owner_id"], unique=False)
    op.create_table(
        "sections",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("venue_id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("sort", sa.Integer(), nullable=False),
        sa.Column("seat_count", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(
            ["venue_id"], ["venues.id"], name=op.f("sections_venue_id_fkey"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("sections_pkey")),
        sa.UniqueConstraint("venue_id", "name", name=op.f("sections_venue_id_name_key")),
    )
    op.create_table(
        "tournaments",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("organizer_id", sa.Uuid(), nullable=False),
        sa.Column("venue_id", sa.Uuid(), nullable=True),
        sa.Column("slug", sa.Text(), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("game", postgresql.CITEXT(), nullable=False),
        sa.Column("description", sa.Text(), server_default="", nullable=False),
        sa.Column("team_size", sa.Integer(), nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column("starts_at", postgresql.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("ends_at", postgresql.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("sales_open_at", postgresql.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("status", sa.Text(), server_default="draft", nullable=False),
        sa.Column(
            "search",
            postgresql.TSVECTOR(),
            sa.Computed(
                "setweight(to_tsvector('english', title), 'A') || "
                "setweight(to_tsvector('english', game::text), 'A') || "
                "setweight(to_tsvector('english', description), 'C')",
                persisted=True,
            ),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            postgresql.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "status IN ('draft', 'published', 'live', 'completed', 'cancelled')",
            name=op.f("tournaments_status_check"),
        ),
        sa.CheckConstraint("ends_at > starts_at", name=op.f("tournaments_dates_check")),
        sa.CheckConstraint(
            "ends_at <= starts_at + interval '14 days'",
            name=op.f("tournaments_max_duration_check"),
        ),
        sa.CheckConstraint("sales_open_at < starts_at", name=op.f("tournaments_sales_open_check")),
        sa.CheckConstraint("team_size >= 1", name=op.f("tournaments_team_size_check")),
        sa.ForeignKeyConstraint(
            ["organizer_id"], ["users.id"], name=op.f("tournaments_organizer_id_fkey")
        ),
        sa.ForeignKeyConstraint(
            ["venue_id"], ["venues.id"], name=op.f("tournaments_venue_id_fkey")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("tournaments_pkey")),
        sa.UniqueConstraint("slug", name=op.f("tournaments_slug_key")),
    )
    op.create_index(
        "tournaments_browse_idx",
        "tournaments",
        ["starts_at", "id"],
        unique=False,
        postgresql_where=sa.text("status IN ('published', 'live')"),
    )
    op.create_index(
        "tournaments_game_browse_idx",
        "tournaments",
        ["game", "starts_at", "id"],
        unique=False,
        postgresql_where=sa.text("status IN ('published', 'live')"),
    )
    op.create_index(
        "tournaments_organizer_id_idx", "tournaments", ["organizer_id", "created_at"], unique=False
    )
    op.create_index(
        "tournaments_search_idx", "tournaments", ["search"], unique=False, postgresql_using="gin"
    )
    op.create_table(
        "seats",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("venue_id", sa.Uuid(), nullable=False),
        sa.Column("section_id", sa.Uuid(), nullable=False),
        sa.Column("row_label", sa.Text(), nullable=False),
        sa.Column("row_sort", sa.Integer(), nullable=False),
        sa.Column("number", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(
            ["section_id"], ["sections.id"], name=op.f("seats_section_id_fkey"), ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["venue_id"], ["venues.id"], name=op.f("seats_venue_id_fkey"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("seats_pkey")),
        sa.UniqueConstraint(
            "section_id", "row_label", "number", name=op.f("seats_section_id_row_label_number_key")
        ),
    )
    op.create_index("seats_venue_id_idx", "seats", ["venue_id"], unique=False)
    op.create_table(
        "ticket_types",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tournament_id", sa.Uuid(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("price_cents", sa.Integer(), nullable=False),
        sa.Column("capacity", sa.Integer(), nullable=False),
        sa.Column(
            "created_at",
            postgresql.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "kind IN ('competitor', 'reserved', 'general')", name=op.f("ticket_types_kind_check")
        ),
        sa.CheckConstraint("capacity >= 1", name=op.f("ticket_types_capacity_check")),
        sa.CheckConstraint("price_cents >= 0", name=op.f("ticket_types_price_check")),
        sa.ForeignKeyConstraint(
            ["tournament_id"],
            ["tournaments.id"],
            name=op.f("ticket_types_tournament_id_fkey"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("ticket_types_pkey")),
        sa.UniqueConstraint("id", "tournament_id", name=op.f("ticket_types_id_tournament_id_key")),
        sa.UniqueConstraint(
            "tournament_id", "name", name=op.f("ticket_types_tournament_id_name_key")
        ),
    )
    op.create_index(
        "ticket_types_one_competitor_idx",
        "ticket_types",
        ["tournament_id"],
        unique=True,
        postgresql_where=sa.text("kind = 'competitor'"),
    )
    op.create_table(
        "ticket_type_sections",
        sa.Column("ticket_type_id", sa.Uuid(), nullable=False),
        sa.Column("section_id", sa.Uuid(), nullable=False),
        sa.Column("tournament_id", sa.Uuid(), nullable=False),
        sa.ForeignKeyConstraint(
            ["section_id"], ["sections.id"], name=op.f("ticket_type_sections_section_id_fkey")
        ),
        sa.ForeignKeyConstraint(
            ["ticket_type_id", "tournament_id"],
            ["ticket_types.id", "ticket_types.tournament_id"],
            name=op.f("ticket_type_sections_ticket_type_id_tournament_id_fkey"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint(
            "ticket_type_id", "section_id", name=op.f("ticket_type_sections_pkey")
        ),
        sa.UniqueConstraint(
            "tournament_id",
            "section_id",
            name=op.f("ticket_type_sections_tournament_id_section_id_key"),
        ),
    )


def downgrade() -> None:
    op.drop_table("ticket_type_sections")
    op.drop_index(
        "ticket_types_one_competitor_idx",
        table_name="ticket_types",
        postgresql_where=sa.text("kind = 'competitor'"),
    )
    op.drop_table("ticket_types")
    op.drop_index("seats_venue_id_idx", table_name="seats")
    op.drop_table("seats")
    op.drop_index("tournaments_search_idx", table_name="tournaments", postgresql_using="gin")
    op.drop_index("tournaments_organizer_id_idx", table_name="tournaments")
    op.drop_index(
        "tournaments_game_browse_idx",
        table_name="tournaments",
        postgresql_where=sa.text("status IN ('published', 'live')"),
    )
    op.drop_index(
        "tournaments_browse_idx",
        table_name="tournaments",
        postgresql_where=sa.text("status IN ('published', 'live')"),
    )
    op.drop_table("tournaments")
    op.drop_table("sections")
    op.drop_index(op.f("venues_owner_id_idx"), table_name="venues")
    op.drop_table("venues")
