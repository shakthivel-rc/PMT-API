"""Payloads of the sprint routes (`routes/sprints.py`)."""
from datetime import date, datetime

from pydantic import BaseModel, Field


class SprintSummary(BaseModel):
    """A sprint and the live counts of what is in it."""

    id: str = Field(description="Opaque id.")
    space_id: str = Field(description="The project (space) it belongs to.")
    name: str = Field(description="`Sprint 12`.")
    goal: str | None = Field(description="What the sprint is for. Plain text.")
    state: str = Field(description="planned, active or closed.")
    starts_on: date | None = Field(description="First day, in the workspace's zone.")
    ends_on: date | None = Field(description="Last day, inclusive.")
    committed_points: int | None = Field(description="Points in it when it was started; "
                                                     "null until then.")
    completed_points: int | None = Field(description="Points done when it was closed; null "
                                                     "until then.")
    started_at: datetime | None = Field(description="UTC.")
    closed_at: datetime | None = Field(description="UTC.")
    created_at: datetime = Field(description="UTC.")
    issue_count: int = Field(description="Live issues in it now.")
    points: int = Field(description="Their summed story points.")
    done_issue_count: int = Field(description="Of those, how many are in a stage of the "
                                              "`done` category — delivered.")
    done_points: int = Field(description="Their summed story points.")
    # A default, so a payload built before the category existed still validates; every
    # payload `services/sprint.payloads` builds carries it.
    canceled_issue_count: int = Field(
        default=0, description="Of those, how many are in a `canceled` stage: closed but not "
                               "delivered, so in neither `done_issue_count` nor the points "
                               "completed (docs/design/09-restructuring.md §9).")


class SprintCloseResult(BaseModel):
    """What closing did."""

    sprint: SprintSummary = Field(description="The sprint, now closed.")
    completed_issues: int = Field(description="Issues done at close; they stay in it.")
    canceled_issues: int = Field(default=0,
                                 description="Issues canceled at close. They stay in it too, "
                                             "are not carried over and are not in "
                                             "`completed_points`.")
    carried_over: int = Field(description="Unfinished issues moved out.")
    completed_points: int = Field(description="Points done at close — `done` stages only.")


class SprintIssueChange(BaseModel):
    """What a membership change did."""

    sprint: SprintSummary = Field(description="The sprint after the change.")
    changed: list[str] = Field(description="Issue ids whose membership changed.")
    unchanged: list[str] = Field(description="Issue ids already in (or already out of) it.")
