"""Venues, seat maps, tournaments, and what is on sale."""

import uuid
from datetime import datetime, timedelta

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Computed,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    false,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import CITEXT, TIMESTAMP, TSVECTOR
from sqlalchemy.orm import Mapped, mapped_column, relationship

from lobbyd.platform.db import Base
from lobbyd.platform.ids import uuid7

# Statuses the public can see in listings.
MAX_DURATION = timedelta(days=14)
LISTED_SQL = "status IN ('published', 'live')"


class Venue(Base):
    __tablename__ = "venues"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid7)
    owner_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"), index=True)
    name: Mapped[str] = mapped_column(Text)
    city: Mapped[str] = mapped_column(Text)
    timezone: Mapped[str] = mapped_column(Text)  # IANA name, for display only
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=func.now()
    )

    sections: Mapped[list["Section"]] = relationship(
        back_populates="venue", order_by="Section.sort", lazy="raise"
    )


class Section(Base):
    __tablename__ = "sections"
    __table_args__ = (UniqueConstraint("venue_id", "name"),)

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid7)
    venue_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("venues.id", ondelete="CASCADE"))
    name: Mapped[str] = mapped_column(Text)
    sort: Mapped[int] = mapped_column(Integer)
    # Kept on the row so listing a venue never has to count its seats.
    seat_count: Mapped[int] = mapped_column(Integer)

    venue: Mapped[Venue] = relationship(back_populates="sections", lazy="raise")


class Seat(Base):
    __tablename__ = "seats"
    __table_args__ = (
        UniqueConstraint("section_id", "row_label", "number"),
        Index("seats_venue_id_idx", "venue_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid7)
    venue_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("venues.id", ondelete="CASCADE"))
    section_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("sections.id", ondelete="CASCADE"))
    row_label: Mapped[str] = mapped_column(Text)
    row_sort: Mapped[int] = mapped_column(Integer)  # rows keep the order they were given in
    number: Mapped[int] = mapped_column(Integer)


class Tournament(Base):
    __tablename__ = "tournaments"
    __table_args__ = (
        CheckConstraint(
            "status IN ('draft', 'published', 'live', 'completed', 'cancelled')", name="status"
        ),
        CheckConstraint("team_size >= 1", name="team_size"),
        CheckConstraint("ends_at > starts_at", name="dates"),
        # A bound on length lets "has not ended yet" be answered from the
        # starts_at index: see MAX_DURATION and docs/query-plans.md.
        CheckConstraint("ends_at <= starts_at + interval '14 days'", name="max_duration"),
        CheckConstraint("sales_open_at < starts_at", name="sales_open"),
        Index("tournaments_search_idx", "search", postgresql_using="gin"),
        # Browse: listed events in date order; (starts_at, id) is the pagination cursor.
        Index("tournaments_browse_idx", "starts_at", "id", postgresql_where=text(LISTED_SQL)),
        # Same, narrowed to one game.
        Index(
            "tournaments_game_browse_idx",
            "game",
            "starts_at",
            "id",
            postgresql_where=text(LISTED_SQL),
        ),
        Index("tournaments_organizer_id_idx", "organizer_id", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid7)
    organizer_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"))
    venue_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("venues.id"))
    slug: Mapped[str] = mapped_column(Text, unique=True)
    title: Mapped[str] = mapped_column(Text)
    game: Mapped[str] = mapped_column(CITEXT)
    description: Mapped[str] = mapped_column(Text, server_default="")
    team_size: Mapped[int] = mapped_column(Integer)
    # One currency per tournament, so an order can never mix currencies.
    currency: Mapped[str] = mapped_column(String(3))
    starts_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True))
    ends_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True))
    sales_open_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True))
    status: Mapped[str] = mapped_column(Text, server_default="draft")
    # Buyers must queue for a turn before they can book. For sales expected
    # to be heavily oversubscribed.
    waiting_room: Mapped[bool] = mapped_column(Boolean, server_default=false())
    # Maintained by Postgres. Title and game outrank the description.
    search: Mapped[str] = mapped_column(
        TSVECTOR,
        Computed(
            "setweight(to_tsvector('english', title), 'A') || "
            "setweight(to_tsvector('english', game::text), 'A') || "
            "setweight(to_tsvector('english', description), 'C')",
            persisted=True,
        ),
    )
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=func.now()
    )

    venue: Mapped[Venue | None] = relationship(lazy="raise")
    ticket_types: Mapped[list["TicketType"]] = relationship(
        back_populates="tournament", order_by="TicketType.created_at", lazy="raise"
    )


class TicketType(Base):
    __tablename__ = "ticket_types"
    __table_args__ = (
        CheckConstraint("kind IN ('competitor', 'reserved', 'general')", name="kind"),
        CheckConstraint("price_cents >= 0", name="price"),
        CheckConstraint("capacity >= 1", name="capacity"),
        UniqueConstraint("tournament_id", "name"),
        # Target for the composite foreign key on ticket_type_sections.
        UniqueConstraint("id", "tournament_id"),
        # A tournament has one set of team slots.
        Index(
            "ticket_types_one_competitor_idx",
            "tournament_id",
            unique=True,
            postgresql_where=text("kind = 'competitor'"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid7)
    tournament_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tournaments.id", ondelete="CASCADE")
    )
    kind: Mapped[str] = mapped_column(Text)
    name: Mapped[str] = mapped_column(Text)
    price_cents: Mapped[int] = mapped_column(Integer)
    # For 'reserved' this is the number of seats in the assigned sections.
    capacity: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=func.now()
    )

    tournament: Mapped[Tournament] = relationship(back_populates="ticket_types", lazy="raise")
    sections: Mapped[list["TicketTypeSection"]] = relationship(lazy="raise")


class TicketTypeSection(Base):
    """Which venue sections a reserved-seating ticket type sells."""

    __tablename__ = "ticket_type_sections"
    __table_args__ = (
        # Guarantees the copied tournament_id matches the ticket type's.
        ForeignKeyConstraint(
            ["ticket_type_id", "tournament_id"],
            ["ticket_types.id", "ticket_types.tournament_id"],
            ondelete="CASCADE",
        ),
        # A section is sold under one ticket type per tournament, so a seat has one price.
        UniqueConstraint("tournament_id", "section_id"),
    )

    ticket_type_id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    section_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("sections.id"), primary_key=True)
    tournament_id: Mapped[uuid.UUID]
