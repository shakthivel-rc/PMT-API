"""Phase 3 Autonomous AI Agents & Workflows routes."""
from datetime import date
from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from controllers import ai_agents_controller as ctl
from db.session import get_db
from dependencies.workspace import WorkspaceAccess, as_guest, as_member
from routes.workspaces import NOT_A_MEMBER
from schemas.ai_agents import (
    BacklogHygieneResponse,
    DailyStandupResponse,
    SprintRetroResponse,
    TriageResult,
)
from schemas.errors import COMMON_ERRORS, VALIDATION_ERROR
from schemas.response import SuccessResponse

router = APIRouter()

AI_ERRORS = {**COMMON_ERRORS, **NOT_A_MEMBER, **VALIDATION_ERROR}


@router.post(
    "/workspaces/{workspace_id}/issues/{issue_id}/triage",
    response_model=SuccessResponse[TriageResult],
    status_code=200,
    responses=AI_ERRORS,
    tags=["Issues"],
    summary="Run autonomous bug triage and duplicate detection on an issue",
    operation_id="triage_issue",
)
def triage_issue(
    issue_id: str,
    access: WorkspaceAccess = Depends(as_member),
    db: Session = Depends(get_db),
):
    return ctl.triage_issue(access, issue_id, db)


@router.get(
    "/workspaces/{workspace_id}/ai/standup",
    response_model=SuccessResponse[DailyStandupResponse],
    status_code=200,
    responses=AI_ERRORS,
    tags=["Workspaces"],
    summary="Get automated daily standup briefing and velocity trajectory",
    operation_id="get_daily_standup",
)
def get_daily_standup(
    for_date: date | None = Query(None, description="Target standup calendar date"),
    access: WorkspaceAccess = Depends(as_guest),
    db: Session = Depends(get_db),
):
    return ctl.get_daily_standup(access, db, for_date)


@router.post(
    "/workspaces/{workspace_id}/ai/standup/generate",
    response_model=SuccessResponse[DailyStandupResponse],
    status_code=200,
    responses=AI_ERRORS,
    tags=["Workspaces"],
    summary="Generate fresh daily standup synthesis",
    operation_id="generate_daily_standup",
)
def generate_daily_standup(
    for_date: date | None = Query(None, description="Target standup calendar date"),
    access: WorkspaceAccess = Depends(as_member),
    db: Session = Depends(get_db),
):
    return ctl.generate_daily_standup(access, db, for_date)


@router.get(
    "/workspaces/{workspace_id}/sprints/{sprint_id}/retrospective",
    response_model=SuccessResponse[SprintRetroResponse],
    status_code=200,
    responses=AI_ERRORS,
    tags=["Sprints"],
    summary="Get sprint retrospective post-mortem",
    operation_id="get_sprint_retro",
)
def get_sprint_retro(
    sprint_id: str,
    access: WorkspaceAccess = Depends(as_guest),
    db: Session = Depends(get_db),
):
    return ctl.get_sprint_retro(access, sprint_id, db)


@router.post(
    "/workspaces/{workspace_id}/sprints/{sprint_id}/retrospective/generate",
    response_model=SuccessResponse[SprintRetroResponse],
    status_code=200,
    responses=AI_ERRORS,
    tags=["Sprints"],
    summary="Generate fresh sprint retrospective",
    operation_id="generate_sprint_retro",
)
def generate_sprint_retro(
    sprint_id: str,
    access: WorkspaceAccess = Depends(as_member),
    db: Session = Depends(get_db),
):
    return ctl.generate_sprint_retro(access, sprint_id, db)


@router.get(
    "/workspaces/{workspace_id}/spaces/{space_id}/backlog/hygiene",
    response_model=SuccessResponse[BacklogHygieneResponse],
    status_code=200,
    responses=AI_ERRORS,
    tags=["Workspaces"],
    summary="Scan space backlog for stale issues",
    operation_id="get_backlog_hygiene",
)
def get_backlog_hygiene(
    space_id: str,
    threshold_days: int = Query(90, ge=7, le=365, description="Days of inactivity threshold"),
    access: WorkspaceAccess = Depends(as_guest),
    db: Session = Depends(get_db),
):
    return ctl.get_backlog_hygiene(access, space_id, threshold_days, db)
