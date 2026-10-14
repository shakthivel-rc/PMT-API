"""Pydantic schemas for Sprint Planning Poker."""
from typing import Literal
from pydantic import BaseModel, Field, model_validator


VALID_FIBONACCI_POINTS: set[int] = {0, 1, 2, 3, 5, 8, 13, 21}
VALID_SPECIAL_VOTES: set[str] = {"?", "coffee"}


class PokerStartRequest(BaseModel):
    issue_id: str = Field(..., description="Issue ID to estimate in this round")


class PokerVoteRequest(BaseModel):
    point_vote: int | None = Field(default=None, description="Fibonacci story point vote (0, 1, 2, 3, 5, 8, 13, 21)")
    special_vote: Literal["?", "coffee"] | None = Field(default=None, description="Special card ('?' or 'coffee')")

    @model_validator(mode="after")
    def validate_vote(self):
        if self.point_vote is None and self.special_vote is None:
            raise ValueError("Must provide either point_vote or special_vote")
        if self.point_vote is not None and self.point_vote not in VALID_FIBONACCI_POINTS:
            raise ValueError(f"point_vote must be one of {sorted(VALID_FIBONACCI_POINTS)}")
        return self


class PokerAcceptRequest(BaseModel):
    story_points: int = Field(..., ge=0, description="Consensus story points to assign to the active issue")


class PokerVoterState(BaseModel):
    user_id: str
    name: str | None = None
    email: str | None = None
    avatar_url: str | None = None
    has_voted: bool
    point_vote: int | None = None
    special_vote: str | None = None


class PokerSessionState(BaseModel):
    id: str
    workspace_id: str
    sprint_id: str
    active_issue_id: str | None = None
    active_issue_title: str | None = None
    active_issue_key: str | None = None
    status: str  # "voting", "revealed", "completed"
    created_by: str
    voters: list[PokerVoterState] = []
    average: float | None = None
    median: float | None = None
    lowest_vote: int | None = None
    highest_vote: int | None = None
    total_voters: int = 0
    voted_count: int = 0
