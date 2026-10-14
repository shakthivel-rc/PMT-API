"""Schemas for cross-space portfolio initiatives (Epics 2.0)."""
from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, Field


InitiativeStatus = Literal["planned", "active", "completed", "cancelled"]
InitiativeHealth = Literal["on_track", "at_risk", "off_track", "planned"]


class InitiativeCreate(BaseModel):
    key: str = Field(..., max_length=20, description="Short unique key e.g. INIT-1")
    title: str = Field(..., max_length=255)
    description: str | None = None
    status: InitiativeStatus = "planned"
    owner_id: str | None = None
    target_quarter: str | None = Field(None, max_length=10, description="e.g. 2026-Q4")
    start_date: date | None = None
    target_date: date | None = None
    budget_amount: Decimal | None = None
    space_ids: list[str] = Field(default_factory=list)


class InitiativeUpdate(BaseModel):
    title: str | None = None
    description: str | None = None
    status: InitiativeStatus | None = None
    owner_id: str | None = None
    target_quarter: str | None = None
    start_date: date | None = None
    target_date: date | None = None
    budget_amount: Decimal | None = None


class InitiativeSpaceSummary(BaseModel):
    id: str
    key: str
    name: str


class InitiativeIssueSummary(BaseModel):
    id: str
    key: str
    title: str
    type: str
    status_name: str
    status_category: str
    story_points: float | None = None
    space_id: str
    space_name: str


class InitiativeRollup(BaseModel):
    total_issues: int = 0
    completed_issues: int = 0
    total_points: float = 0.0
    completed_points: float = 0.0
    progress_percentage: float = 0.0
    health: InitiativeHealth = "planned"
    actual_spent_amount: float = 0.0
    budget_amount: float | None = None
    budget_burn_percentage: float | None = None


class InitiativeResponse(BaseModel):
    id: str
    workspace_id: str
    key: str
    title: str
    description: str | None = None
    status: InitiativeStatus
    owner_id: str | None = None
    owner_name: str | None = None
    target_quarter: str | None = None
    start_date: date | None = None
    target_date: date | None = None
    budget_amount: float | None = None
    created_by: str
    created_at: datetime
    updated_at: datetime
    rollup: InitiativeRollup


class InitiativeDetailResponse(InitiativeResponse):
    contributing_spaces: list[InitiativeSpaceSummary] = Field(default_factory=list)
    linked_issues: list[InitiativeIssueSummary] = Field(default_factory=list)


class LinkSpacesRequest(BaseModel):
    space_ids: list[str] = Field(..., min_length=1)


class LinkIssuesRequest(BaseModel):
    issue_ids: list[str] = Field(..., min_length=1)
