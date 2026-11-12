"""What is left to sell, and who is holding what."""

import uuid
from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    ForeignKey,
    Index,
    Integer,
    PrimaryKeyConstraint,
    String,
    Text,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import TIMESTAMP
from sqlalchemy.orm import Mapped, mapped_column

from lobbyd.platform.db import Base
from lobbyd.platform.ids import uuid7


class InventoryCounter(Base):
    """Counted inventory: team slots and general admission."""

    __tablename__ = "inventory_counters"
    __table_args__ = (
        CheckConstraint("capacity >= 0", name="capacity"),
        CheckConstraint("held_count >= 0", name="held"),
        CheckConstraint("sold_count >= 0", name="sold"),
        # The database's own guarantee. Whatever the application does, a
        # transaction that would sell more than exists fails.
        CheckConstraint("held_count + sold_count <= capacity", name="no_oversell"),
    )

    ticket_type_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("ticket_types.id", ondelete="CASCADE"), primary_key=True
    )
    tournament_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tournaments.id", ondelete="CASCADE"), index=True
    )
    capacity: Mapped[int] = mapped_column(Integer)
    held_count: Mapped[int] = mapped_column(Integer, server_default="0")
    sold_count: Mapped[int] = mapped_column(Integer, server_default="0")


class TournamentSeat(Base):
    """One row per seat per tournament. Claiming a seat is an update to exactly this row."""

    __tablename__ = "tournament_seats"
    __table_args__ = (
        PrimaryKeyConstraint("tournament_id", "seat_id"),
        CheckConstraint("status IN ('available', 'held', 'sold', 'blocked')", name="status"),
        # Only a held seat has a holder and a deadline, and it always has both.
        CheckConstraint(
            "(status = 'held') = (hold_id IS NOT NULL) "
            "AND (hold_id IS NULL) = (held_until IS NULL)",
            name="held_has_owner",
        ),
        Index(
            "tournament_seats_hold_id_idx", "hold_id", postgresql_where=text("hold_id IS NOT NULL")
        ),
        # Lets the sweeper find lapsed seats without scanning every seat.
        Index(
            "tournament_seats_held_until_idx",
            "held_until",
            postgresql_where=text("status = 'held'"),
        ),
    )

    tournament_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tournaments.id", ondelete="CASCADE")
    )
    seat_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("seats.id"))
    ticket_type_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("ticket_types.id"))
    status: Mapped[str] = mapped_column(Text, server_default="available")
    hold_id: Mapped[uuid.UUID | None]
    # On the seat itself, so a claim can treat a lapsed hold as free in the
    # same statement, with no cleanup job involved.
    held_until: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))


class Hold(Base):
    __tablename__ = "holds"
    __table_args__ = (
        CheckConstraint("status IN ('active', 'converted', 'expired', 'released')", name="status"),
        # The sweeper's only query.
        Index("holds_expiry_idx", "expires_at", postgresql_where=text("status = 'active'")),
        # One open hold per user per tournament: no hoarding across many holds.
        Index(
            "holds_one_active_idx",
            "user_id",
            "tournament_id",
            unique=True,
            postgresql_where=text("status = 'active'"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid7)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"))
    tournament_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tournaments.id"))
    currency: Mapped[str] = mapped_column(String(3))
    status: Mapped[str] = mapped_column(Text, server_default="active")
    expires_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=func.now()
    )


class HoldItem(Base):
    __tablename__ = "hold_items"
    __table_args__ = (
        CheckConstraint("quantity >= 1", name="quantity"),
        CheckConstraint("seat_id IS NULL OR quantity = 1", name="one_per_seat"),
        Index("hold_items_hold_id_idx", "hold_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid7)
    hold_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("holds.id", ondelete="CASCADE"))
    ticket_type_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("ticket_types.id"))
    seat_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("seats.id"))
    quantity: Mapped[int] = mapped_column(Integer, server_default="1")
    # Price when held, so the total shown at hold time is the total charged.
    unit_price_cents: Mapped[int] = mapped_column(Integer)
