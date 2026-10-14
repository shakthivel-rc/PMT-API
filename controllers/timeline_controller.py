"""Controller for interactive timeline / Gantt chart and cascade rescheduling."""
from __future__ import annotations

from datetime import date

from sqlalchemy.orm import Session

from dependencies.workspace import WorkspaceAccess
from schemas.response import SuccessResponse, error_json
from schemas.timeline import RescheduleRequest
from services import timeline as svc


def get_timeline(
    access: WorkspaceAccess,
    space_id: str,
    db: Session,
    start_date: date | None = None,
    end_date: date | None = None,
    zoom: str = "week",
    group_by: str = "none",
):
    try:
        data = svc.get_timeline(
            db,
            workspace_id=access.workspace_id,
            space_id=space_id,
            start_date=start_date,
            end_date=end_date,
            zoom=zoom,
            group_by=group_by,
        )
        return SuccessResponse(status_code=200, message="Timeline retrieved", data=data)
    except ValueError as e:
        return error_json(404, str(e))


def reschedule_issue(
    access: WorkspaceAccess,
    issue_id: str,
    payload: RescheduleRequest,
    db: Session,
):
    try:
        data = svc.reschedule_issue(
            db,
            workspace_id=access.workspace_id,
            issue_id=issue_id,
            payload=payload,
            actor_id=access.user_id,
        )
        return SuccessResponse(status_code=200, message="Issue rescheduled", data=data)
    except ValueError as e:
        msg = str(e)
        if "not found" in msg.lower():
            return error_json(404, msg)
        return error_json(400, msg)
