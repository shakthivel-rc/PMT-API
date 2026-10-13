"""Time-tracking routes. Thin: path, contract, the tenancy gate, a controller call.

Two routers, because a timer belongs to a person and everything else to a workspace:

* `router`, under `/workspaces/{workspace_id}` — time logged on issues, the timesheet list
  and report, starting a timer on an issue, and the workspace's settings. Every route
  declares the workspace gate, so a non-member gets the same 404 as everywhere else.
* `timer_router`, under `/timer` — reading, stopping and discarding *your* running timer.
  You have at most one, in whichever workspace you started it, and the header that shows it
  is not inside any workspace; asking it to know which one first would make it a search.
  The controller re-checks membership of the timer's workspace before it shows or saves
  anything, so leaving a workspace takes its timer with it.
"""
from datetime import date

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy.orm import Session

from controllers import time_tracking_controller as ctl
from db.session import get_db
from dependencies.workspace import WorkspaceAccess, as_admin, as_guest, as_member, caller_id
from routes.workspaces import NOT_A_MEMBER
from schemas.errors import COMMON_ERRORS, VALIDATION_ERROR
from schemas.response import ErrorResponse, SuccessResponse
from schemas.responses.time_tracking import (
    ActiveTimerSummary,
    IssueTimeTracking,
    TimeEntryPage,
    TimeEntrySummary,
    TimeReport,
    TimerStartResult,
    TimerStopResult,
)
from schemas.time_tracking import (
    TimeEntryCreate,
    TimeEntryUpdate,
    TimerStart,
    TimerStop,
    TimeTrackingSettings,
    TimeTrackingSettingsUpdate,
)

router = APIRouter(prefix="/workspaces/{workspace_id}", tags=["Time tracking"])
timer_router = APIRouter(prefix="/timer", tags=["Time tracking"])

RULE_BROKEN = {422: {"model": ErrorResponse,
                     "description": "Well-formed but against a rule: tracking is switched off "
                                    "here, the day has not started yet, or the person you "
                                    "are logging for cannot log time."}}
NO_ENTRY = {404: {"model": ErrorResponse,
                  "description": "No such workspace or entry, or one you cannot see — under "
                                 "`own` visibility, other members' entries are not yours "
                                 "to see."}}
BAD_FILTER = {400: {"model": ErrorResponse,
                    "description": "The date range is backwards, or wider than a report "
                                   "covers."}}
#: docs/design/09-restructuring.md §3.4, decided by `services/write_guard`: time on an issue
#: is read-only while its list, folder or space is archived, or its space is frozen for a
#: migration wave. Reads are never refused, so only the writes declare it.
LOCKED = {423: {"model": ErrorResponse,
                "description": "Locked: the issue's list, folder or space is archived, or its "
                               "space is frozen while a migration wave runs. The message names "
                               "the container and what lifts it; reading the time still "
                               "works."}}
TIMER_LOCKED = {423: {"model": ErrorResponse,
                      "description": "Locked: the issue your timer is on is in an archived or "
                                     "frozen container, so its time cannot be logged now. "
                                     "The timer keeps running (`data.timer_running`) — stop "
                                     "it once the space is writable again, or discard it, "
                                     "which is never refused."}}


@router.get(
    "/issues/{issue_id}/time-tracking",
    response_model=SuccessResponse[IssueTimeTracking],
    responses={**COMMON_ERRORS, **NOT_A_MEMBER},
    summary="Get an issue's time: totals, roll-up, who logged it, and the work log",
    operation_id="get_issue_time_tracking",
)
def get_issue_time(issue_id: str,
                   access: WorkspaceAccess = Depends(as_guest),
                   db: Session = Depends(get_db)):
    """One read for the issue drawer's time panel. Totals are the issue's whatever the
    workspace's visibility setting; the per-person lines and the entries follow it."""
    return ctl.get_issue_time(access, issue_id, db)


@router.post(
    "/issues/{issue_id}/time-entries",
    response_model=SuccessResponse[TimeEntrySummary],
    status_code=201,
    responses={**COMMON_ERRORS, **VALIDATION_ERROR, **NOT_A_MEMBER, **RULE_BROKEN, **LOCKED},
    summary="Log time on an issue",
    operation_id="create_issue_time_entry",
)
def create_entry(issue_id: str, body: TimeEntryCreate, request: Request,
                 access: WorkspaceAccess = Depends(as_member),
                 db: Session = Depends(get_db)):
    """Your own time, or — as an admin, with `user_id` — someone else's. A retried request
    logs twice; on a timeout, re-read the issue's time before sending it again."""
    return ctl.create_entry(access, issue_id, body, db, request)


@router.patch(
    "/time-entries/{entry_id}",
    response_model=SuccessResponse[TimeEntrySummary],
    responses={**COMMON_ERRORS, **VALIDATION_ERROR, **NO_ENTRY, **RULE_BROKEN, **LOCKED},
    summary="Change a time entry",
    operation_id="update_time_entry",
)
def update_entry(entry_id: str, body: TimeEntryUpdate, request: Request,
                 access: WorkspaceAccess = Depends(as_member),
                 db: Session = Depends(get_db)):
    """Only the fields present change. Your own entries; anyone's if you are an admin."""
    return ctl.update_entry(access, entry_id, body, db, request)


@router.delete(
    "/time-entries/{entry_id}",
    response_model=SuccessResponse[dict],
    responses={**COMMON_ERRORS, **NO_ENTRY, **LOCKED},
    summary="Delete a time entry",
    operation_id="delete_time_entry",
)
def delete_entry(entry_id: str, request: Request,
                 access: WorkspaceAccess = Depends(as_member),
                 db: Session = Depends(get_db)):
    """A soft delete; the issue's history records how much time went with it."""
    return ctl.delete_entry(access, entry_id, db, request)


@router.get(
    "/time-entries",
    response_model=SuccessResponse[TimeEntryPage],
    responses={**COMMON_ERRORS, **VALIDATION_ERROR, **NOT_A_MEMBER, **BAD_FILTER},
    summary="List time entries — a person's, an issue's, or everyone's — paged",
    operation_id="list_time_entries",
)
def list_entries(
    access: WorkspaceAccess = Depends(as_guest),
    db: Session = Depends(get_db),
    user_id: str | None = Query(None, description="An account id, or `me`. Omit for "
                                                  "everyone you can see."),
    issue_id: str | None = Query(None, description="Only this issue."),
    date_from: date | None = Query(None, description="First work date included."),
    date_to: date | None = Query(None, description="Last work date included."),
    page: int = Query(1, ge=1),
    limit: int = Query(50, ge=1, le=200),
):
    """Entries on deleted issues are left out, as the issues are."""
    return ctl.list_entries(access, db, user_id=user_id, issue_id=issue_id,
                            date_from=date_from, date_to=date_to, page=page, limit=limit)


@router.get(
    "/time-report",
    response_model=SuccessResponse[TimeReport],
    responses={**COMMON_ERRORS, **VALIDATION_ERROR, **NOT_A_MEMBER, **BAD_FILTER},
    summary="Sum time by person, issue and day over a date range — the timesheet grid",
    operation_id="get_time_report",
)
def get_report(
    access: WorkspaceAccess = Depends(as_guest),
    db: Session = Depends(get_db),
    date_from: date = Query(description="First work date included."),
    date_to: date = Query(description="Last work date included; at most 92 days on."),
    user_id: str | None = Query(None, description="An account id, or `me`. Omit for "
                                                  "everyone you can see."),
):
    return ctl.get_report(access, db, date_from=date_from, date_to=date_to, user_id=user_id)


@router.post(
    "/issues/{issue_id}/timer",
    response_model=SuccessResponse[TimerStartResult],
    status_code=201,
    responses={**COMMON_ERRORS, **VALIDATION_ERROR, **NOT_A_MEMBER, **RULE_BROKEN,
               409: {"model": ErrorResponse,
                     "description": "Another timer was started at the same instant."},
               423: {"model": ErrorResponse,
                     "description": "Locked: the issue's list, folder or space is archived "
                                    "or frozen — or the timer you already have running is on "
                                    "such an issue, and starting would stop it and log its "
                                    "time there. That timer keeps running "
                                    "(`data.timer_running`); stop it once its space is "
                                    "writable again, or discard it."}},
    summary="Start your timer on an issue",
    operation_id="start_timer",
)
def start_timer(issue_id: str, body: TimerStart | None = None,
                access: WorkspaceAccess = Depends(as_member),
                db: Session = Depends(get_db)):
    """You have one timer. A timer already running elsewhere is stopped and saved first, and
    comes back as `stopped_entry`; starting the one already running changes nothing."""
    return ctl.start_timer(access, issue_id, body, db)


@router.get(
    "/settings/time-tracking",
    response_model=SuccessResponse[TimeTrackingSettings],
    responses={**COMMON_ERRORS, **NOT_A_MEMBER},
    summary="Get the workspace's time-tracking settings",
    operation_id="get_time_tracking_settings",
)
def get_settings(access: WorkspaceAccess = Depends(as_guest),
                 db: Session = Depends(get_db)):
    return ctl.get_settings(access, db)


@router.patch(
    "/settings/time-tracking",
    response_model=SuccessResponse[TimeTrackingSettings],
    responses={**COMMON_ERRORS, **VALIDATION_ERROR, **NOT_A_MEMBER},
    summary="Change the workspace's time-tracking settings",
    operation_id="update_time_tracking_settings",
)
def update_settings(body: TimeTrackingSettingsUpdate, request: Request,
                    access: WorkspaceAccess = Depends(as_admin),
                    db: Session = Depends(get_db)):
    """Admins only. Only the fields present change; the change is written to the audit log."""
    return ctl.update_settings(access, body, db, request)


@timer_router.get(
    "",
    response_model=SuccessResponse[ActiveTimerSummary | None],
    responses={**COMMON_ERRORS},
    summary="Get your running timer, or null",
    operation_id="get_timer",
)
def get_timer(request: Request, db: Session = Depends(get_db)):
    """`data` is null when nothing is running — a normal state, so not a 404."""
    return ctl.get_timer(caller_id(request), db)


@timer_router.post(
    "/stop",
    response_model=SuccessResponse[TimerStopResult],
    responses={**COMMON_ERRORS, **VALIDATION_ERROR, **TIMER_LOCKED},
    summary="Stop your timer and log the time",
    operation_id="stop_timer",
)
def stop_timer(request: Request, body: TimerStop | None = None,
               db: Session = Depends(get_db)):
    """Safe to retry: with nothing running it answers `entry: null`. Refused, with the timer
    left running, while its issue's container is archived or frozen."""
    return ctl.stop_timer(caller_id(request), body, db)


@timer_router.delete(
    "",
    response_model=SuccessResponse[dict],
    responses={**COMMON_ERRORS},
    summary="Discard your timer without logging anything",
    operation_id="discard_timer",
)
def discard_timer(request: Request, db: Session = Depends(get_db)):
    return ctl.discard_timer(caller_id(request), db)
