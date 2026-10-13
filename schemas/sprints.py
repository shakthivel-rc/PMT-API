"""Request bodies for sprints (`routes/sprints.py`, docs/design/08-reports.md §5.1)."""
from datetime import date
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

#: An `add`/`remove` request names at most this many issues. Planning drags a handful at a
#: time; a whole backlog in one request is a bulk import, which is a different tool.
MAX_SPRINT_BATCH = 200


class SprintCreate(BaseModel):
    model_config = ConfigDict(extra="forbid", json_schema_extra={
        "examples": [{"name": "Sprint 12", "goal": "Checkout works on mobile",
                      "starts_on": "2026-10-05", "ends_on": "2026-10-16"}],
    })

    name: str = Field(min_length=1, max_length=100)
    goal: str | None = Field(default=None, max_length=2000)
    starts_on: date | None = None
    ends_on: date | None = None

    @model_validator(mode="after")
    def _ordered(self) -> "SprintCreate":
        if self.starts_on and self.ends_on and self.ends_on < self.starts_on:
            raise ValueError("ends_on is before starts_on")
        return self


class SprintUpdate(BaseModel):
    """Only the fields present change. The dates of an active sprint may move — Jira allows
    it too — but a closed sprint is history and refuses every change."""

    model_config = ConfigDict(extra="forbid", json_schema_extra={
        "examples": [{"ends_on": "2026-10-19"}]})

    name: str | None = Field(default=None, min_length=1, max_length=100)
    goal: str | None = Field(default=None, max_length=2000)
    starts_on: date | None = None
    ends_on: date | None = None


class SprintStart(BaseModel):
    model_config = ConfigDict(extra="forbid", json_schema_extra={
        "examples": [{"starts_on": "2026-10-05", "ends_on": "2026-10-16"}]})

    starts_on: date | None = Field(default=None, description="Default: the planned date, "
                                                             "or today.")
    ends_on: date | None = Field(default=None, description="Default: the planned date, or "
                                                           "two weeks after the start.")


class SprintClose(BaseModel):
    model_config = ConfigDict(extra="forbid", json_schema_extra={
        "examples": [{"carry_over": "sprint", "target_sprint_id": "<sprint id>"}]})

    carry_over: Literal["backlog", "sprint"] = Field(
        default="backlog", description="Where the unfinished issues go.")
    target_sprint_id: str | None = Field(default=None,
                                         description="With `sprint`: a planned sprint of the "
                                                     "same project.")

    @model_validator(mode="after")
    def _target(self) -> "SprintClose":
        if self.carry_over == "sprint" and not self.target_sprint_id:
            raise ValueError("target_sprint_id is required to carry over to a sprint")
        return self


class SprintIssues(BaseModel):
    model_config = ConfigDict(extra="forbid", json_schema_extra={
        "examples": [{"issue_ids": ["<issue id>"]}]})

    issue_ids: list[str] = Field(min_length=1, max_length=MAX_SPRINT_BATCH)
