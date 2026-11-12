"""Teams, their rosters, and the waitlist."""

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    CheckConstraint,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    PrimaryKeyConstraint,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import CITEXT, JSONB, TIMESTAMP
from sqlalchemy.orm import Mapped, mapped_column

from lobbyd.platform.db import Base
from lobbyd.platform.ids import uuid7


class Team(Base):
    __tablename__ = "teams"
    __table_args__ = (
        CheckConstraint("status IN ('pending', 'confirmed', 'withdrawn')", name="status"),
        # Target for the composite foreign key on team_members.
        UniqueConstraint("id", "tournament_id"),
        # A name is unique within a tournament, ignoring case, among teams still in it.
        Index(
            "teams_name_idx",
            "tournament_id",
            "name",
            unique=True,
            postgresql_where=text("status <> 'withdrawn'"),
        ),
        # A captain enters one team per tournament.
        Index(
            "teams_captain_idx",
            "tournament_id",
            "captain_id",
            unique=True,
            postgresql_where=text("status <> 'withdrawn'"),
        ),
        Index("teams_hold_id_idx", "hold_id", unique=True),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid7)
    tournament_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tournaments.id"))
    captain_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"))
    name: Mapped[str] = mapped_column(CITEXT)
    # pending: slot held, not yet paid. confirmed: paid. withdrawn: gave the slot up.
    status: Mapped[str] = mapped_column(Text, server_default="pending")
    ticket_type_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("ticket_types.id"))
    # The hold that claimed the team's slot. It links the team to its order.
    hold_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("holds.id"))
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=func.now()
    )


class TeamMember(Base):
    __tablename__ = "team_members"
    __table_args__ = (
        PrimaryKeyConstraint("team_id", "handle"),
        # Guarantees the copied tournament_id always matches the team's.
        ForeignKeyConstraint(
            ["team_id", "tournament_id"], ["teams.id", "teams.tournament_id"], ondelete="CASCADE"
        ),
        # A player competes for one team per tournament. Rows are removed when
        # a team withdraws, which frees its players to join another.
        UniqueConstraint("tournament_id", "handle"),
    )

    team_id: Mapped[uuid.UUID]
    tournament_id: Mapped[uuid.UUID]  # copied from the team, for the constraint above
    handle: Mapped[str] = mapped_column(CITEXT)  # in-game name
    position: Mapped[int]  # order on the roster as entered


class WaitlistEntry(Base):
    __tablename__ = "waitlist_entries"
    __table_args__ = (
        CheckConstraint(
            "status IN ('waiting', 'offered', 'accepted', 'lapsed', 'left')", name="status"
        ),
        # One live entry per captain per tournament.
        Index(
            "waitlist_one_live_idx",
            "tournament_id",
            "captain_id",
            unique=True,
            postgresql_where=text("status IN ('waiting', 'offered')"),
        ),
        # "Who is next?" in arrival order (ids are time-ordered).
        Index(
            "waitlist_next_idx", "tournament_id", "id", postgresql_where=text("status = 'waiting'")
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid7)
    tournament_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tournaments.id"))
    captain_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"))
    name: Mapped[str] = mapped_column(Text)
    roster: Mapped[list[Any]] = mapped_column(JSONB)  # checked again when a slot is offered
    status: Mapped[str] = mapped_column(Text, server_default="waiting")
    # Why an entry lapsed, in words for the captain.
    note: Mapped[str | None] = mapped_column(Text)
    team_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("teams.id"))
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=func.now()
    )
