"""Schemas for deployments and issue deployment markers."""
from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field


DeploymentEnvironment = Literal["development", "staging", "production", "canary"]
DeploymentStatus = Literal["in_progress", "success", "failed", "rolled_back"]


class DeploymentCreate(BaseModel):
    environment: DeploymentEnvironment
    status: DeploymentStatus
    commit_sha: str = Field(..., min_length=7, max_length=40)
    ref_name: str | None = None
    deployer_name: str | None = None
    deployment_url: str | None = None
    repository_id: str | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    issue_keys: list[str] = Field(default_factory=list)
    commit_messages: list[str] = Field(default_factory=list)


class DeploymentResponse(BaseModel):
    id: str
    workspace_id: str
    repository_id: str | None = None
    environment: str
    status: str
    commit_sha: str
    ref_name: str | None = None
    deployer_name: str | None = None
    deployment_url: str | None = None
    started_at: datetime
    finished_at: datetime | None = None
    created_at: datetime
    shipped_issue_count: int = 0


class IssueDeploymentSummary(BaseModel):
    deployment_id: str
    environment: str
    status: str
    commit_sha: str
    ref_name: str | None = None
    deployment_url: str | None = None
    deployed_at: datetime
