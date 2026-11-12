"""Request and response shapes for the catalog module."""

import uuid
from typing import Annotated, Literal, Self
from zoneinfo import available_timezones

from pydantic import (
    AfterValidator,
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    model_validator,
)

from lobbyd.catalog.models import MAX_DURATION

MAX_SEATS_PER_VENUE = 100_000

Name = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=80)]
Currency = Annotated[str, StringConstraints(pattern=r"^[A-Z]{3}$")]


def _valid_timezone(value: str) -> str:
    if value not in available_timezones():
        raise ValueError("must be an IANA timezone name such as 'Europe/Berlin'")
    return value


Timezone = Annotated[str, AfterValidator(_valid_timezone)]


# --- venues ---------------------------------------------------------------


class RowIn(BaseModel):
    label: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=10)]
    seats: int = Field(ge=1, le=500, description="Seats in this row, numbered from 1")


class SectionIn(BaseModel):
    name: Name
    rows: list[RowIn] = Field(min_length=1, max_length=200)

    @model_validator(mode="after")
    def _unique_rows(self) -> Self:
        labels = [row.label for row in self.rows]
        if len(labels) != len(set(labels)):
            raise ValueError("row labels must be unique within a section")
        return self


class VenueCreate(BaseModel):
    """A venue and its whole seat map in one request."""

    name: Name
    city: Name
    timezone: Timezone
    sections: list[SectionIn] = Field(min_length=1, max_length=500)

    @model_validator(mode="after")
    def _check_seat_map(self) -> Self:
        names = [section.name for section in self.sections]
        if len(names) != len(set(names)):
            raise ValueError("section names must be unique")
        total = sum(row.seats for section in self.sections for row in section.rows)
        if total > MAX_SEATS_PER_VENUE:
            raise ValueError(f"a venue can have at most {MAX_SEATS_PER_VENUE} seats")
        return self


class SectionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    seat_count: int


class VenueSummary(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    city: str
    timezone: str


class VenueOut(VenueSummary):
    capacity: int
    sections: list[SectionOut]


class SeatOut(BaseModel):
    id: uuid.UUID
    number: int


class RowOut(BaseModel):
    label: str
    seats: list[SeatOut]


class SectionLayout(BaseModel):
    id: uuid.UUID
    name: str
    rows: list[RowOut]


class SeatMap(BaseModel):
    venue_id: uuid.UUID
    sections: list[SectionLayout]


# --- tournaments ------------------------------------------------------------

Title = Annotated[str, StringConstraints(strip_whitespace=True, min_length=3, max_length=120)]
Game = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=60)]
Description = Annotated[str, StringConstraints(max_length=5000)]


class TournamentCreate(BaseModel):
    title: Title
    game: Game
    description: Description = ""
    venue_id: uuid.UUID | None = Field(default=None, description="Omit for online-only events")
    team_size: int = Field(ge=1, le=20)
    currency: Currency
    starts_at: AwareDatetime
    ends_at: AwareDatetime
    sales_open_at: AwareDatetime
    waiting_room: bool = Field(
        default=False, description="Make buyers queue for a turn. For oversubscribed sales."
    )

    @model_validator(mode="after")
    def _check_dates(self) -> Self:
        if self.ends_at <= self.starts_at:
            raise ValueError("ends_at must be after starts_at")
        if self.ends_at - self.starts_at > MAX_DURATION:
            raise ValueError(f"a tournament can last at most {MAX_DURATION.days} days")
        if self.sales_open_at >= self.starts_at:
            raise ValueError("sales_open_at must be before starts_at")
        return self


class TournamentUpdate(BaseModel):
    """Only the fields sent are changed."""

    title: Title | None = None
    game: Game | None = None
    description: Description | None = None
    team_size: int | None = Field(default=None, ge=1, le=20)
    starts_at: AwareDatetime | None = None
    ends_at: AwareDatetime | None = None
    sales_open_at: AwareDatetime | None = None
    waiting_room: bool | None = None


class TicketTypeCreate(BaseModel):
    kind: Literal["competitor", "reserved", "general"]
    name: Name
    price_cents: int = Field(ge=0, le=10_000_000)
    capacity: int | None = Field(
        default=None,
        ge=1,
        le=1_000_000,
        description="Team slots or tickets. Not used for reserved seating.",
    )
    section_ids: list[uuid.UUID] | None = Field(
        default=None,
        min_length=1,
        description="Venue sections sold under this type. Reserved seating only.",
    )

    @model_validator(mode="after")
    def _check_shape(self) -> Self:
        if self.kind == "reserved":
            if not self.section_ids:
                raise ValueError("reserved seating needs section_ids")
            if self.capacity is not None:
                raise ValueError("capacity is derived from the sections for reserved seating")
            if len(self.section_ids) != len(set(self.section_ids)):
                raise ValueError("section_ids must be unique")
        else:
            if self.capacity is None:
                raise ValueError("capacity is required")
            if self.section_ids is not None:
                raise ValueError("section_ids only applies to reserved seating")
        return self


class TicketTypeOut(BaseModel):
    id: uuid.UUID
    kind: str
    name: str
    price_cents: int
    capacity: int
    section_ids: list[uuid.UUID]


class TournamentSummary(BaseModel):
    id: uuid.UUID
    slug: str
    title: str
    game: str
    status: str
    team_size: int
    currency: str
    starts_at: AwareDatetime
    ends_at: AwareDatetime
    sales_open_at: AwareDatetime
    waiting_room: bool
    venue: VenueSummary | None


class TournamentOut(TournamentSummary):
    description: str
    ticket_types: list[TicketTypeOut]


class GameCount(BaseModel):
    game: str
    tournaments: int


# --- shared with the booking modules ------------------------------------------


class SeatLabel(BaseModel):
    """Where a seat is, in words a person can find it by."""

    section: str
    row: str
    number: int


class TournamentRef(BaseModel):
    slug: str
    title: str
    starts_at: AwareDatetime
    timezone: str | None = Field(description="The venue's timezone. Null for online events.")
