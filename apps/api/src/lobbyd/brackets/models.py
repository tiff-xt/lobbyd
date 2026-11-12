"""A tournament's bracket: one row per match."""

import uuid

from sqlalchemy import CheckConstraint, ForeignKey, Integer, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from lobbyd.platform.db import Base
from lobbyd.platform.ids import uuid7


class Match(Base):
    __tablename__ = "matches"
    __table_args__ = (
        CheckConstraint("status IN ('scheduled', 'live', 'completed')", name="status"),
        CheckConstraint(
            "winner_id IS NULL OR winner_id = team_a_id OR winner_id = team_b_id",
            name="winner_played",
        ),
        CheckConstraint("(status = 'completed') = (winner_id IS NOT NULL)", name="completed"),
        # One match per place in the bracket. Also what stops two organizers
        # generating the same bracket twice at the same moment.
        UniqueConstraint("tournament_id", "round", "position"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid7)
    tournament_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tournaments.id"))
    round: Mapped[int] = mapped_column(Integer)  # 1 is the first round
    position: Mapped[int] = mapped_column(Integer)  # 0-based, top to bottom within the round
    team_a_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("teams.id"))
    team_b_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("teams.id"))
    score_a: Mapped[int | None] = mapped_column(Integer)
    score_b: Mapped[int | None] = mapped_column(Integer)
    winner_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("teams.id"))
    # Where the winner goes. Position p feeds slot A of the next match if p is even, B if odd.
    next_match_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("matches.id"))
    status: Mapped[str] = mapped_column(Text, server_default="scheduled")
    # Goes up on every change. An edit must name the version it was made
    # against, and is refused if someone else changed the match first.
    version: Mapped[int] = mapped_column(Integer, server_default="0")
