"""Phase 3 AI Agents Controller."""
from __future__ import annotations

from datetime import date
from typing import Any

from fastapi import HTTPException
from sqlalchemy.orm import Session

from dependencies.workspace import WorkspaceAccess
from schemas.ai_agents import (
    BacklogHygieneResponse,
    DailyStandupResponse,
    SprintRetroResponse,
    StandupMemberUpdate,
    StandupVelocityForecast,
    TriageResult,
)
from schemas.response import SuccessResponse
from services import ai_agents as svc


def _standup_payload(row: Any) -> DailyStandupResponse:
    team_updates = [
        StandupMemberUpdate(
            user_id=u.get("user_id", ""),
            user_name=u.get("user_name", ""),
            completed_yesterday=u.get("completed_yesterday", []),
            in_flight_today=u.get("in_flight_today", []),
            blockers=u.get("blockers", []),
            hours_logged=u.get("hours_logged", 0.0),
        )
        for u in (row.team_updates or [])
    ]
    forecast = (
        StandupVelocityForecast(
            active_sprint_name=row.velocity_forecast.get("active_sprint_name"),
            remaining_points=row.velocity_forecast.get("remaining_points", 0),
            working_days_left=row.velocity_forecast.get("working_days_left", 0),
            required_rate=row.velocity_forecast.get("required_rate", 0.0),
            current_burn_rate=row.velocity_forecast.get("current_burn_rate", 0.0),
            carryover_risk=row.velocity_forecast.get("carryover_risk", False),
            projected_carryover_pct=row.velocity_forecast.get("projected_carryover_pct", 0.0),
            health_status=row.velocity_forecast.get("health_status", "healthy"),
        )
        if row.velocity_forecast
        else None
    )

    return DailyStandupResponse(
        id=str(row.id),
        workspace_id=str(row.workspace_id),
        standup_date=row.standup_date.isoformat() if hasattr(row.standup_date, "isoformat") else str(row.standup_date),
        summary=row.summary,
        team_updates=team_updates,
        velocity_forecast=forecast,
        created_at=row.created_at.isoformat() if hasattr(row.created_at, "isoformat") else str(row.created_at),
    )


def _retro_payload(row: Any) -> SprintRetroResponse:
    return SprintRetroResponse(
        id=str(row.id),
        workspace_id=str(row.workspace_id),
        sprint_id=str(row.sprint_id),
        summary=row.summary,
        wins=row.wins or [],
        scope_creep=row.scope_creep or [],
        cycle_time_outliers=row.cycle_time_outliers or [],
        metrics=row.metrics or {},
        created_at=row.created_at.isoformat() if hasattr(row.created_at, "isoformat") else str(row.created_at),
    )


def triage_issue(
    access: WorkspaceAccess,
    issue_id: str,
    db: Session,
) -> SuccessResponse[TriageResult]:
    """Execute autonomous triage on an issue."""
    try:
        result = svc.triage_issue(
            db=db,
            workspace_id=access.workspace_id,
            issue_id=issue_id,
            actor_id=access.user_id,
        )
        return SuccessResponse(status_code=200, message="Issue triaged successfully", data=result)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


def get_daily_standup(
    access: WorkspaceAccess,
    db: Session,
    for_date: date | None = None,
) -> SuccessResponse[DailyStandupResponse]:
    """Retrieve the standup for today (or requested date)."""
    standup = svc.get_daily_standup(db=db, workspace_id=access.workspace_id, for_date=for_date)
    return SuccessResponse(status_code=200, message="Standup retrieved", data=_standup_payload(standup))


def generate_daily_standup(
    access: WorkspaceAccess,
    db: Session,
    for_date: date | None = None,
) -> SuccessResponse[DailyStandupResponse]:
    """Force fresh generation of daily standup synthesis."""
    standup = svc.generate_daily_standup(db=db, workspace_id=access.workspace_id, for_date=for_date)
    return SuccessResponse(status_code=200, message="Standup generated", data=_standup_payload(standup))


def get_sprint_retro(
    access: WorkspaceAccess,
    sprint_id: str,
    db: Session,
) -> SuccessResponse[SprintRetroResponse]:
    """Retrieve sprint retrospective."""
    try:
        retro = svc.get_sprint_retro(db=db, workspace_id=access.workspace_id, sprint_id=sprint_id)
        return SuccessResponse(status_code=200, message="Sprint retrospective retrieved", data=_retro_payload(retro))
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


def generate_sprint_retro(
    access: WorkspaceAccess,
    sprint_id: str,
    db: Session,
) -> SuccessResponse[SprintRetroResponse]:
    """Generate fresh sprint retrospective post-mortem."""
    try:
        retro = svc.generate_sprint_retro(db=db, workspace_id=access.workspace_id, sprint_id=sprint_id)
        return SuccessResponse(status_code=200, message="Sprint retrospective generated", data=_retro_payload(retro))
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


def get_backlog_hygiene(
    access: WorkspaceAccess,
    space_id: str,
    threshold_days: int,
    db: Session,
) -> SuccessResponse[BacklogHygieneResponse]:
    """Scan space backlog for stale issues."""
    try:
        result = svc.scan_backlog_hygiene(
            db=db,
            workspace_id=access.workspace_id,
            space_id=space_id,
            threshold_days=threshold_days,
        )
        return SuccessResponse(status_code=200, message="Backlog hygiene report generated", data=result)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
