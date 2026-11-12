"""Request and response shapes for teams and the waitlist."""

import uuid
from datetime import datetime
from typing import Annotated, Self

from pydantic import BaseModel, Field, StringConstraints, model_validator

from lobbyd.inventory.schemas import MAX_SEATS_PER_HOLD, CountedItemIn, HoldOut

TeamName = Annotated[str, StringConstraints(strip_whitespace=True, min_length=2, max_length=40)]
Handle = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=2, max_length=24, pattern=r"^[\w .\-]+$"),
]


class Roster(BaseModel):
    name: TeamName
    roster: list[Handle] = Field(
        min_length=1, max_length=20, description="In-game names, one per player"
    )

    @model_validator(mode="after")
    def _unique_players(self) -> Self:
        handles = [handle.lower() for handle in self.roster]
        if len(handles) != len(set(handles)):
            raise ValueError("a player can only be listed once")
        return self


class TeamCreate(Roster):
    """Register a team: claims a slot now, confirmed when the entry fee is paid.

    Seats and standing tickets can be added to the same basket.
    """

    seat_ids: list[uuid.UUID] = Field(default_factory=list, max_length=MAX_SEATS_PER_HOLD)
    items: list[CountedItemIn] = Field(default_factory=list, max_length=3)


class TeamOut(BaseModel):
    id: uuid.UUID
    tournament_id: uuid.UUID
    name: str
    status: str
    captain_id: uuid.UUID
    roster: list[str]
    hold_id: uuid.UUID | None = Field(
        default=None, description="Only shown to the captain: the hold that pays for the entry"
    )
    created_at: datetime


class TeamRegistration(BaseModel):
    team: TeamOut
    hold: HoldOut = Field(description="Check this out to confirm the team")


class WaitlistOut(BaseModel):
    id: uuid.UUID
    tournament_id: uuid.UUID
    name: str
    roster: list[str]
    status: str
    position: int | None = Field(description="1 means next in line. Null once no longer waiting")
    note: str | None
    offered_hold_id: uuid.UUID | None = Field(
        description="When a slot has been offered: the hold to check out before it lapses"
    )
    created_at: datetime
