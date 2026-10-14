"""Pydantic schemas for Phase 3 Autonomous AI Agents & Workflows."""
from typing import Any, Literal
from pydantic import BaseModel, Field


class TriageResult(BaseModel):
    issue_id: str
    potential_duplicate: bool
    duplicate_issue_id: str | None = None
    duplicate_issue_key: str | None = None
    duplicate_issue_title: str | None = None
    similarity_score: float | None = None
    suggested_assignee_id: str | None = None
    suggested_assignee_name: str | None = None
    suggested_priority: str | None = None
    structured_reproduction_steps: str | None = None
    triage_notes: str


class StandupMemberUpdate(BaseModel):
    user_id: str
    user_name: str
    completed_yesterday: list[str] = Field(default_factory=list)
    in_flight_today: list[str] = Field(default_factory=list)
    blockers: list[str] = Field(default_factory=list)
    hours_logged: float = 0.0


class StandupVelocityForecast(BaseModel):
    active_sprint_name: str | None = None
    remaining_points: int = 0
    working_days_left: int = 0
    required_rate: float = 0.0
    current_burn_rate: float = 0.0
    carryover_risk: bool = False
    projected_carryover_pct: float = 0.0
    health_status: Literal["healthy", "at_risk", "critical"] = "healthy"


class DailyStandupResponse(BaseModel):
    id: str
    workspace_id: str
    standup_date: str
    summary: str
    team_updates: list[StandupMemberUpdate] = Field(default_factory=list)
    velocity_forecast: StandupVelocityForecast | None = None
    created_at: str


class SprintRetroResponse(BaseModel):
    id: str
    workspace_id: str
    sprint_id: str
    summary: str
    wins: list[str] = Field(default_factory=list)
    scope_creep: list[str] = Field(default_factory=list)
    cycle_time_outliers: list[str] = Field(default_factory=list)
    metrics: dict[str, Any] = Field(default_factory=dict)
    created_at: str


class BacklogHygieneItem(BaseModel):
    issue_id: str
    key: str
    title: str
    days_inactive: int
    story_points: int | None = None
    priority: str
    recommendation: str


class BacklogHygieneResponse(BaseModel):
    space_id: str
    stale_count: int
    threshold_days: int
    items: list[BacklogHygieneItem] = Field(default_factory=list)
