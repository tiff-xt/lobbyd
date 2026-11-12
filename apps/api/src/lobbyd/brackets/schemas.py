"""Request and response shapes for brackets."""

import uuid

from pydantic import BaseModel, Field


class TeamRef(BaseModel):
    id: uuid.UUID
    name: str


class MatchOut(BaseModel):
    id: uuid.UUID
    round: int
    position: int
    team_a: TeamRef | None = Field(description="Null until the feeding match is decided")
    team_b: TeamRef | None
    score_a: int | None
    score_b: int | None
    winner_id: uuid.UUID | None
    status: str = Field(description="scheduled, live, or completed")
    version: int = Field(description="Send this back when changing the match")
    next_match_id: uuid.UUID | None


class RoundOut(BaseModel):
    round: int
    name: str
    matches: list[MatchOut]


class BracketOut(BaseModel):
    tournament_id: uuid.UUID
    rounds: list[RoundOut]
    champion: TeamRef | None


class ResultIn(BaseModel):
    score_a: int = Field(ge=0, le=999)
    score_b: int = Field(ge=0, le=999)
    version: int = Field(description="The match's version when you looked at it")


class VersionIn(BaseModel):
    version: int
