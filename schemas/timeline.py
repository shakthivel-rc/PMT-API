"""Schemas for interactive timeline / Gantt chart and cascade rescheduling."""
from __future__ import annotations

from datetime import date
from typing import Literal

from pydantic import BaseModel, Field


class TimelineIssueItem(BaseModel):
    id: str
    key: str
    title: str
    type: str
    status_id: str
    status_name: str
    status_category: str
    priority: str
    start_date: date | None = None
    due_date: date | None = None
    duration_days: int = 1
    story_points: float | None = None
    assignee_id: str | None = None
    assignee_name: str | None = None
    epic_id: str | None = None
    epic_title: str | None = None
    list_id: str
    list_name: str
    is_blocked: bool = False
    is_critical_path: bool = False


class TimelineDependencyItem(BaseModel):
    id: str
    source_issue_id: str
    target_issue_id: str
    kind: str
    is_conflict: bool = False


class TimelineGroup(BaseModel):
    id: str
    title: str
    issue_ids: list[str] = Field(default_factory=list)


class TimelineResponse(BaseModel):
    space_id: str
    zoom: str = "week"
    start_date: date | None = None
    end_date: date | None = None
    items: list[TimelineIssueItem] = Field(default_factory=list)
    dependencies: list[TimelineDependencyItem] = Field(default_factory=list)
    critical_path_issue_ids: list[str] = Field(default_factory=list)
    groups: list[TimelineGroup] = Field(default_factory=list)


class RescheduleRequest(BaseModel):
    start_date: date
    due_date: date
    cascade: bool = False
    cascade_mode: Literal["push_all_dependents", "push_conflicts_only"] = "push_all_dependents"


class RescheduleResponse(BaseModel):
    issue: TimelineIssueItem
    cascaded_issues: list[TimelineIssueItem] = Field(default_factory=list)
    delta_days: int = 0
