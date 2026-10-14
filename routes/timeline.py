"""Routes for interactive timeline / Gantt chart and cascade rescheduling."""
from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from controllers import timeline_controller as controller
from db.session import get_db
from dependencies.workspace import WorkspaceAccess, as_member
from routes.workspaces import NOT_A_MEMBER
from schemas.errors import COMMON_ERRORS
from schemas.response import ErrorResponse, SuccessResponse
from schemas.timeline import RescheduleRequest, RescheduleResponse, TimelineResponse

router = APIRouter(prefix="/workspaces/{workspace_id}", tags=["Workspaces"])
issue_router = APIRouter(prefix="/workspaces/{workspace_id}/issues", tags=["Issues"])


@router.get(
    "/spaces/{space_id}/timeline",
    response_model=SuccessResponse[TimelineResponse],
    responses={**COMMON_ERRORS, **NOT_A_MEMBER, 404: {"model": ErrorResponse, "description": "Space not found"}},
    summary="Get timeline and Gantt view data for a space",
    operation_id="get_space_timeline",
)
def get_space_timeline(
    space_id: str,
    start_date: date | None = Query(None, description="Filter issues starting on or after this date"),
    end_date: date | None = Query(None, description="Filter issues ending on or before this date"),
    zoom: str = Query("week", description="Zoom resolution: day, week, month, quarter"),
    group_by: str = Query("none", description="Grouping: none, epic, list, assignee"),
    access: WorkspaceAccess = Depends(as_member),
    db: Session = Depends(get_db),
):
    """Returns timeline items, dependencies, critical path, and swimlanes for a space."""
    return controller.get_timeline(
        access=access,
        space_id=space_id,
        db=db,
        start_date=start_date,
        end_date=end_date,
        zoom=zoom,
        group_by=group_by,
    )


@issue_router.patch(
    "/{issue_id}/reschedule",
    response_model=SuccessResponse[RescheduleResponse],
    responses={
        **COMMON_ERRORS,
        **NOT_A_MEMBER,
        400: {"model": ErrorResponse, "description": "Invalid date range"},
        404: {"model": ErrorResponse, "description": "Issue or Space not found"},
    },
    summary="Reschedule an issue with optional cascade shifting",
    operation_id="reschedule_issue",
)
def reschedule_issue(
    issue_id: str,
    body: RescheduleRequest,
    access: WorkspaceAccess = Depends(as_member),
    db: Session = Depends(get_db),
):
    """Updates issue start and due dates with optional forward cascading of downstream blockers."""
    return controller.reschedule_issue(
        access=access,
        issue_id=issue_id,
        payload=body,
        db=db,
    )
