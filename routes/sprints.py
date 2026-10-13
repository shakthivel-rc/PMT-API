"""Sprint routes, and the sprint and project report shortcuts.

Sprints belong to a project (a space): listed and created under `/spaces/{space_id}`, read
and changed under `/sprints/{sprint_id}`. The shortcuts at the end — a sprint's report and
burndown, a project's velocity, cumulative flow and cycle time — are the general report
route (`GET /reports/{type}`) with the scope filled in, for the links the SPA's sprint and
project pages make.
"""
from datetime import date

from fastapi import APIRouter, Depends, Query, Request
from fastapi.exceptions import RequestValidationError
from pydantic import ValidationError
from sqlalchemy.orm import Session

from controllers import report_controller as report_ctl
from controllers import sprint_controller as ctl
from db.session import get_db
from dependencies.workspace import WorkspaceAccess, as_guest, as_member
from routes.reports import BAD_RANGE, FORBIDDEN, TOO_LARGE
from routes.workspaces import NOT_A_MEMBER
from schemas.errors import COMMON_ERRORS, VALIDATION_ERROR
from schemas.reports import DatePreset, ReportParams, Unit
from schemas.response import ErrorResponse, SuccessResponse
from schemas.responses.issues import IssueCard
from schemas.responses.reports import ReportResult
from schemas.responses.sprints import SprintCloseResult, SprintIssueChange, SprintSummary
from schemas.sprints import SprintClose, SprintCreate, SprintIssues, SprintStart, SprintUpdate

router = APIRouter(prefix="/workspaces/{workspace_id}", tags=["Sprints"])

NO_SPRINT = {404: {"model": ErrorResponse,
                   "description": "No such workspace, project, sprint or issue — or one in "
                                  "another workspace or project."}}
CONFLICT = {409: {"model": ErrorResponse,
                  "description": "Not in this state: only a planned sprint starts or is "
                                 "deleted, only the active one closes, a project has one "
                                 "active sprint, and a closed sprint does not change."}}
NO_MANAGE = {403: {"model": ErrorResponse,
                   "description": "Your role is below the workspace's `sprint_manage_role`."}}
#: docs/design/09-restructuring.md §3.4, decided by `services/write_guard`. Reads are never
#: refused, so only the routes that write declare it.
LOCKED = {423: {"model": ErrorResponse,
                "description": "Locked: the sprint's project (space) is archived, or frozen "
                               "while a migration wave runs. When issues move in or out — "
                               "including unfinished ones carried into another sprint at the "
                               "close — an issue's own list or folder being archived refuses "
                               "it too, naming the issue in `data.issue_id`. The message says "
                               "which container and what lifts it; reads still work."}}
REPORT_ERRORS = {**COMMON_ERRORS, **VALIDATION_ERROR, **NOT_A_MEMBER, **NO_SPRINT, **FORBIDDEN,
                 **BAD_RANGE, **TOO_LARGE}


@router.get(
    "/spaces/{space_id}/sprints",
    response_model=SuccessResponse[list[SprintSummary]],
    responses={**COMMON_ERRORS, **NOT_A_MEMBER, **NO_SPRINT},
    summary="List a project's sprints",
    operation_id="list_sprints",
)
def list_sprints(space_id: str,
                 state: str | None = Query(None, max_length=40,
                                           description="Comma-separated: planned, active, "
                                                       "closed. Default: all."),
                 access: WorkspaceAccess = Depends(as_guest), db: Session = Depends(get_db)):
    """The active sprint, then planned ones in creation order, then closed, newest first."""
    return ctl.list_sprints(access, space_id, state, db)


@router.post(
    "/spaces/{space_id}/sprints",
    response_model=SuccessResponse[SprintSummary],
    status_code=201,
    responses={**COMMON_ERRORS, **VALIDATION_ERROR, **NOT_A_MEMBER, **NO_SPRINT, **LOCKED},
    summary="Create a sprint",
    operation_id="create_sprint",
)
def create_sprint(space_id: str, body: SprintCreate,
                  access: WorkspaceAccess = Depends(as_member), db: Session = Depends(get_db)):
    """A planned sprint. Members plan; starting it is `POST /sprints/{id}/start`."""
    return ctl.create_sprint(access, space_id, body, db)


@router.get(
    "/sprints/{sprint_id}",
    response_model=SuccessResponse[SprintSummary],
    responses={**COMMON_ERRORS, **NOT_A_MEMBER, **NO_SPRINT},
    summary="Get a sprint",
    operation_id="get_sprint",
)
def get_sprint(sprint_id: str, access: WorkspaceAccess = Depends(as_guest),
               db: Session = Depends(get_db)):
    """With live counts of what is in it and what is done."""
    return ctl.get_sprint(access, sprint_id, db)


@router.patch(
    "/sprints/{sprint_id}",
    response_model=SuccessResponse[SprintSummary],
    responses={**COMMON_ERRORS, **VALIDATION_ERROR, **NOT_A_MEMBER, **NO_SPRINT, **CONFLICT,
               **LOCKED},
    summary="Rename or re-date a sprint",
    operation_id="update_sprint",
)
def update_sprint(sprint_id: str, body: SprintUpdate,
                  access: WorkspaceAccess = Depends(as_member), db: Session = Depends(get_db)):
    """Only the fields present change. A closed sprint does not."""
    return ctl.update_sprint(access, sprint_id, body, db)


@router.delete(
    "/sprints/{sprint_id}",
    response_model=SuccessResponse[dict],
    responses={**COMMON_ERRORS, **NOT_A_MEMBER, **NO_SPRINT, **NO_MANAGE, **CONFLICT,
               **LOCKED},
    summary="Delete a sprint that has not started",
    operation_id="delete_sprint",
)
def delete_sprint(sprint_id: str, request: Request,
                  access: WorkspaceAccess = Depends(as_member), db: Session = Depends(get_db)):
    """Its issues go back to the backlog. Audited."""
    return ctl.delete_sprint(access, sprint_id, db, request)


@router.post(
    "/sprints/{sprint_id}/start",
    response_model=SuccessResponse[SprintSummary],
    responses={**COMMON_ERRORS, **VALIDATION_ERROR, **NOT_A_MEMBER, **NO_SPRINT, **NO_MANAGE,
               **CONFLICT, **LOCKED},
    summary="Start a sprint",
    operation_id="start_sprint",
)
def start_sprint(sprint_id: str, body: SprintStart, request: Request,
                 access: WorkspaceAccess = Depends(as_member), db: Session = Depends(get_db)):
    """Freezes the commitment (the points in it now) for the velocity chart. One active
    sprint per project. Audited."""
    return ctl.start_sprint(access, sprint_id, body, db, request)


@router.post(
    "/sprints/{sprint_id}/close",
    response_model=SuccessResponse[SprintCloseResult],
    responses={**COMMON_ERRORS, **VALIDATION_ERROR, **NOT_A_MEMBER, **NO_SPRINT, **NO_MANAGE,
               **CONFLICT, **LOCKED},
    summary="Close a sprint",
    operation_id="close_sprint",
)
def close_sprint(sprint_id: str, body: SprintClose, request: Request,
                 access: WorkspaceAccess = Depends(as_member), db: Session = Depends(get_db)):
    """Freezes the points completed — issues in `done` stages only. Unfinished issues go
    to the backlog or a planned sprint; done ones stay, so the sprint keeps what it
    delivered, and so do canceled ones, which are neither carried over nor counted as
    completed (`canceled_issues`). Audited."""
    return ctl.close_sprint(access, sprint_id, body, db, request)


@router.get(
    "/sprints/{sprint_id}/issues",
    response_model=SuccessResponse[list[IssueCard]],
    responses={**COMMON_ERRORS, **NOT_A_MEMBER, **NO_SPRINT},
    summary="List the issues in a sprint",
    operation_id="list_sprint_issues",
)
def sprint_issues(sprint_id: str, access: WorkspaceAccess = Depends(as_guest),
                  db: Session = Depends(get_db)):
    """The live issues in it now, as board cards."""
    return ctl.sprint_issues(access, sprint_id, db)


@router.post(
    "/sprints/{sprint_id}/issues",
    response_model=SuccessResponse[SprintIssueChange],
    responses={**COMMON_ERRORS, **VALIDATION_ERROR, **NOT_A_MEMBER, **NO_SPRINT, **CONFLICT,
               **LOCKED},
    summary="Add issues to a sprint",
    operation_id="add_sprint_issues",
)
def add_issues(sprint_id: str, body: SprintIssues,
               access: WorkspaceAccess = Depends(as_member), db: Session = Depends(get_db)):
    """Issues of the sprint's project. One in another open sprint moves. Added to an active
    sprint, it is "added after start" in the sprint report."""
    return ctl.change_issues(access, sprint_id, body, True, db)


@router.delete(
    "/sprints/{sprint_id}/issues",
    response_model=SuccessResponse[SprintIssueChange],
    responses={**COMMON_ERRORS, **VALIDATION_ERROR, **NOT_A_MEMBER, **NO_SPRINT, **CONFLICT,
               **LOCKED},
    summary="Remove issues from a sprint",
    operation_id="remove_sprint_issues",
)
def remove_issues(sprint_id: str, body: SprintIssues,
                  access: WorkspaceAccess = Depends(as_member), db: Session = Depends(get_db)):
    """Back to the backlog."""
    return ctl.change_issues(access, sprint_id, body, False, db)


# ---- report shortcuts --------------------------------------------------------------------


def _params(**values) -> ReportParams:
    """A path id that is not id-shaped is the same 422 as a bad query parameter."""
    try:
        return ReportParams(**values)
    except ValidationError as exc:
        raise RequestValidationError(exc.errors(include_url=False)) from exc


@router.get(
    "/sprints/{sprint_id}/report",
    response_model=SuccessResponse[ReportResult],
    responses=REPORT_ERRORS,
    summary="A sprint's report",
    operation_id="get_sprint_report",
)
def sprint_report(sprint_id: str, access: WorkspaceAccess = Depends(as_guest),
                  db: Session = Depends(get_db)):
    """`GET /reports/sprint_report?scope=sprint:{id}`."""
    return report_ctl.run_report(access, "sprint_report",
                                 _params(scope=f"sprint:{sprint_id}"), db)


@router.get(
    "/sprints/{sprint_id}/burndown",
    response_model=SuccessResponse[ReportResult],
    responses=REPORT_ERRORS,
    summary="A sprint's burndown",
    operation_id="get_sprint_burndown",
)
def sprint_burndown(sprint_id: str, unit: Unit = Query("points"),
                    access: WorkspaceAccess = Depends(as_guest),
                    db: Session = Depends(get_db)):
    """`GET /reports/burndown?scope=sprint:{id}&unit=…`."""
    return report_ctl.run_report(access, "burndown",
                                 _params(scope=f"sprint:{sprint_id}", unit=unit), db)


@router.get(
    "/spaces/{space_id}/velocity",
    response_model=SuccessResponse[ReportResult],
    responses=REPORT_ERRORS,
    summary="A project's velocity",
    operation_id="get_space_velocity",
)
def space_velocity(space_id: str, sprint_count: int = Query(7, ge=1, le=25),
                   access: WorkspaceAccess = Depends(as_guest),
                   db: Session = Depends(get_db)):
    """`GET /reports/velocity?scope=space:{id}`."""
    return report_ctl.run_report(access, "velocity", _params(
        scope=f"space:{space_id}", sprint_count=sprint_count), db)


@router.get(
    "/spaces/{space_id}/cfd",
    response_model=SuccessResponse[ReportResult],
    responses=REPORT_ERRORS,
    summary="A project's cumulative flow",
    operation_id="get_space_cfd",
)
def space_cfd(space_id: str, preset: DatePreset | None = Query(None),
              date_from: date | None = Query(None), date_to: date | None = Query(None),
              access: WorkspaceAccess = Depends(as_guest), db: Session = Depends(get_db)):
    """`GET /reports/cfd?scope=space:{id}`."""
    return report_ctl.run_report(access, "cfd", _params(
        scope=f"space:{space_id}", preset=preset, date_from=date_from, date_to=date_to), db)


@router.get(
    "/spaces/{space_id}/cycle-time",
    response_model=SuccessResponse[ReportResult],
    responses=REPORT_ERRORS,
    summary="A project's cycle time",
    operation_id="get_space_cycle_time",
)
def space_cycle_time(space_id: str, preset: DatePreset | None = Query(None),
                     date_from: date | None = Query(None), date_to: date | None = Query(None),
                     access: WorkspaceAccess = Depends(as_guest),
                     db: Session = Depends(get_db)):
    """`GET /reports/cycle_time?scope=space:{id}`."""
    return report_ctl.run_report(access, "cycle_time", _params(
        scope=f"space:{space_id}", preset=preset, date_from=date_from, date_to=date_to), db)
