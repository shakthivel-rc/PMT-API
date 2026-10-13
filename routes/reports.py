"""Report routes. Thin: path, contract, the tenancy gate, a controller call.

All under `/workspaces/{workspace_id}`, so a non-member gets the same 404 as everywhere else.
A report is read with GET and its parameters in the query string — so a report is a URL that
can be bookmarked, shared and cached — and exported with POST, because an export can be a
write (a queued job) and carries format options.

The parameters are the fields of `schemas.reports.ReportParams`; list parameters repeat
(`?user_ids=a&user_ids=b`) or are comma-separated (`?user_ids=a,b`).
"""
from datetime import date

from fastapi import APIRouter, Depends, Query, Request
from fastapi.exceptions import RequestValidationError
from pydantic import ValidationError
from sqlalchemy.orm import Session

from controllers import report_controller as ctl
from db.session import get_db
from dependencies.workspace import WorkspaceAccess, as_admin, as_guest, as_member
from routes.workspaces import NOT_A_MEMBER
from schemas.errors import COMMON_ERRORS, VALIDATION_ERROR
from schemas.reports import (
    BillableFilter,
    Bucket,
    DatePreset,
    ExportRequest,
    ReportParams,
    ReportsSettings,
    ReportsSettingsUpdate,
    RoundingMode,
    SavedReportCreate,
    SavedReportUpdate,
    ScheduleCreate,
    ScheduleUpdate,
    Unit,
)
from schemas.response import ErrorResponse, SuccessResponse
from schemas.responses.reports import (
    ReportCatalog,
    ReportJobSummary,
    ReportResult,
    SavedReportSummary,
    ScheduleSummary,
)

router = APIRouter(prefix="/workspaces/{workspace_id}", tags=["Reports"])

NOT_FOUND = {404: {"model": ErrorResponse,
                   "description": "No such workspace, report type, scope, saved report, job "
                                  "or schedule — or one in another workspace."}}
FORBIDDEN = {403: {"model": ErrorResponse,
                   "description": "Your role may not do this here: guests when guest access "
                                  "is off, or a role below the workspace's export / "
                                  "schedule minimum."}}
BAD_RANGE = {400: {"model": ErrorResponse,
                   "description": "The date range is backwards or wider than this report "
                                  "covers, or the parameters do not fit this report."}}
TOO_LARGE = {413: {"model": ErrorResponse,
                   "description": "More rows than a report shows at once. `data.hint` is "
                                  "`export`: export it instead."}}
FILE = {200: {"description": "The file.", "content": {
    "text/csv": {}, "application/pdf": {},
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": {}}}}

_LIST_FIELDS = ("user_ids", "assignee_ids", "reporter_ids", "issue_ids", "types",
                "priorities", "status_categories", "group_by")


def _split(values: list[str]) -> list[str]:
    return [part.strip() for value in values for part in value.split(",") if part.strip()]


def report_params(
    scope: str = Query("workspace", description="`workspace`, or `space:<id>`, "
                                                "`folder:<id>`, `list:<id>`, `sprint:<id>`."),
    preset: DatePreset | None = Query(None),
    date_from: date | None = Query(None),
    date_to: date | None = Query(None),
    user_ids: list[str] = Query([]),
    assignee_ids: list[str] = Query([]),
    reporter_ids: list[str] = Query([]),
    issue_ids: list[str] = Query([]),
    types: list[str] = Query([]),
    priorities: list[str] = Query([]),
    status_categories: list[str] = Query([]),
    epic_id: str | None = Query(None),
    sprint_id: str | None = Query(None),
    billable: BillableFilter = Query("all"),
    group_by: list[str] = Query([]),
    unit: Unit = Query("points"),
    bucket: Bucket = Query("week"),
    cumulative: bool = Query(False),
    rounding_minutes: int = Query(0),
    rounding_mode: RoundingMode = Query("nearest"),
    sprint_count: int = Query(7),
) -> ReportParams:
    """`ReportParams` from the query string. A dependency, so a bad value is the same 422
    every other validation failure is."""
    raw = dict(locals().items())
    for key in _LIST_FIELDS:
        raw[key] = _split(raw[key])
    try:
        return ReportParams.model_validate(raw)
    except ValidationError as exc:
        raise RequestValidationError(exc.errors(include_url=False)) from exc


# ---- running -----------------------------------------------------------------------------


@router.get(
    "/reports/catalog",
    response_model=SuccessResponse[ReportCatalog],
    responses={**COMMON_ERRORS, **NOT_A_MEMBER},
    summary="List the report types and what you may do with them here",
    operation_id="get_report_catalog",
)
def get_catalog(access: WorkspaceAccess = Depends(as_guest), db: Session = Depends(get_db)):
    """Every report, its parameters and limits, plus your permissions: whether you may run,
    export and schedule reports, and manage sprints."""
    return ctl.catalog(access, db)


@router.get(
    "/reports/{report_type}",
    response_model=SuccessResponse[ReportResult],
    responses={**COMMON_ERRORS, **VALIDATION_ERROR, **NOT_A_MEMBER, **NOT_FOUND, **FORBIDDEN,
               **BAD_RANGE, **TOO_LARGE},
    summary="Run a report",
    operation_id="run_report",
)
def run_report(report_type: str, params: ReportParams = Depends(report_params),
               access: WorkspaceAccess = Depends(as_guest), db: Session = Depends(get_db)):
    """Compute one report (`GET /reports/catalog` lists them). Served from a short-lived cache
    when nothing in the workspace has changed since; `meta.cached` says which."""
    return ctl.run_report(access, report_type, params, db)


@router.post(
    "/reports/{report_type}/export",
    response_model=SuccessResponse[ReportJobSummary],
    status_code=200,
    responses={**FILE, 202: {"model": SuccessResponse[ReportJobSummary],
                             "description": "Too big to answer at once, or `background` was "
                                            "set: a job was queued. Poll it."},
               **COMMON_ERRORS, **VALIDATION_ERROR, **NOT_A_MEMBER, **NOT_FOUND,
               **FORBIDDEN, **BAD_RANGE},
    summary="Export a report as CSV, Excel or PDF",
    operation_id="export_report",
)
def export_report(report_type: str, body: ExportRequest, request: Request,
                  access: WorkspaceAccess = Depends(as_guest), db: Session = Depends(get_db)):
    """Up to 5,000 rows: the file itself, at once. More, or with `background`: 202 and a job
    to poll (`GET /report-jobs/{id}`); its file is kept 24 hours. Audited."""
    return ctl.export_report(access, report_type, body, db, request)


# ---- jobs --------------------------------------------------------------------------------


@router.get(
    "/report-jobs",
    response_model=SuccessResponse[list[ReportJobSummary]],
    responses={**COMMON_ERRORS, **NOT_A_MEMBER},
    summary="List your recent export jobs and scheduled reports",
    operation_id="list_report_jobs",
)
def list_jobs(access: WorkspaceAccess = Depends(as_guest), db: Session = Depends(get_db)):
    """The last 50 jobs you asked for or received, newest first."""
    return ctl.list_jobs(access, db)


@router.get(
    "/report-jobs/{job_id}",
    response_model=SuccessResponse[ReportJobSummary],
    responses={**COMMON_ERRORS, **NOT_A_MEMBER, **NOT_FOUND},
    summary="Get an export job",
    operation_id="get_report_job",
)
def get_job(job_id: str, access: WorkspaceAccess = Depends(as_guest),
            db: Session = Depends(get_db)):
    """Its status and, once done, a download path signed for you."""
    return ctl.get_job(access, job_id, db)


@router.get(
    "/report-jobs/{job_id}/download",
    responses={**FILE, **COMMON_ERRORS, **NOT_A_MEMBER, **NOT_FOUND,
               403: {"model": ErrorResponse, "description": "The token is invalid, expired "
                                                            "or someone else's."},
               409: {"model": ErrorResponse, "description": "Not finished yet."},
               410: {"model": ErrorResponse, "description": "The file has expired."}},
    summary="Download an export job's file",
    operation_id="download_report_job",
)
def download_job(job_id: str, token: str | None = Query(None, max_length=200),
                 access: WorkspaceAccess = Depends(as_guest), db: Session = Depends(get_db)):
    """With the `token` from the job's `download_path`. Signed-in AND signed for you: a
    forwarded link does not open for anyone else."""
    return ctl.download_job(access, job_id, token, db)


# ---- saved reports -----------------------------------------------------------------------


@router.get(
    "/saved-reports",
    response_model=SuccessResponse[list[SavedReportSummary]],
    responses={**COMMON_ERRORS, **NOT_A_MEMBER, **FORBIDDEN},
    summary="List saved reports",
    operation_id="list_saved_reports",
)
def list_saved(access: WorkspaceAccess = Depends(as_guest), db: Session = Depends(get_db)):
    """Yours, and those shared with the workspace. Pinned first."""
    return ctl.list_saved(access, db)


@router.post(
    "/saved-reports",
    response_model=SuccessResponse[SavedReportSummary],
    status_code=201,
    responses={**COMMON_ERRORS, **VALIDATION_ERROR, **NOT_A_MEMBER, **FORBIDDEN, **BAD_RANGE},
    summary="Save a report",
    operation_id="create_saved_report",
)
def create_saved(body: SavedReportCreate, access: WorkspaceAccess = Depends(as_guest),
                 db: Session = Depends(get_db)):
    """A report type and its parameters, under a name. `workspace` visibility shares it."""
    return ctl.create_saved(access, body, db)


@router.get(
    "/saved-reports/{saved_id}",
    response_model=SuccessResponse[SavedReportSummary],
    responses={**COMMON_ERRORS, **NOT_A_MEMBER, **NOT_FOUND, **FORBIDDEN},
    summary="Get a saved report",
    operation_id="get_saved_report",
)
def get_saved(saved_id: str, access: WorkspaceAccess = Depends(as_guest),
              db: Session = Depends(get_db)):
    """One saved report you can see."""
    return ctl.get_saved(access, saved_id, db)


@router.patch(
    "/saved-reports/{saved_id}",
    response_model=SuccessResponse[SavedReportSummary],
    responses={**COMMON_ERRORS, **VALIDATION_ERROR, **NOT_A_MEMBER, **NOT_FOUND, **FORBIDDEN},
    summary="Change a saved report",
    operation_id="update_saved_report",
)
def update_saved(saved_id: str, body: SavedReportUpdate,
                 access: WorkspaceAccess = Depends(as_guest), db: Session = Depends(get_db)):
    """Only the fields present change. Its owner, or an admin."""
    return ctl.update_saved(access, saved_id, body, db)


@router.delete(
    "/saved-reports/{saved_id}",
    response_model=SuccessResponse[dict],
    responses={**COMMON_ERRORS, **NOT_A_MEMBER, **NOT_FOUND, **FORBIDDEN},
    summary="Delete a saved report and its schedules",
    operation_id="delete_saved_report",
)
def delete_saved(saved_id: str, request: Request, access: WorkspaceAccess = Depends(as_guest),
                 db: Session = Depends(get_db)):
    """Its owner, or an admin. Its schedules stop. Audited."""
    return ctl.delete_saved(access, saved_id, db, request)


# ---- schedules ---------------------------------------------------------------------------


@router.get(
    "/saved-reports/{saved_id}/schedules",
    response_model=SuccessResponse[list[ScheduleSummary]],
    responses={**COMMON_ERRORS, **NOT_A_MEMBER, **NOT_FOUND},
    summary="List a saved report's schedules",
    operation_id="list_report_schedules",
)
def list_schedules(saved_id: str, access: WorkspaceAccess = Depends(as_guest),
                   db: Session = Depends(get_db)):
    """When it is emailed, and to whom."""
    return ctl.list_schedules(access, saved_id, db)


@router.post(
    "/saved-reports/{saved_id}/schedules",
    response_model=SuccessResponse[ScheduleSummary],
    status_code=201,
    responses={**COMMON_ERRORS, **VALIDATION_ERROR, **NOT_A_MEMBER, **NOT_FOUND, **FORBIDDEN,
               **BAD_RANGE},
    summary="Email a saved report on a schedule",
    operation_id="create_report_schedule",
)
def create_schedule(saved_id: str, body: ScheduleCreate, request: Request,
                    access: WorkspaceAccess = Depends(as_member),
                    db: Session = Depends(get_db)):
    """Daily, weekly or monthly at an hour in a time zone, to members of the workspace.
    Needs the workspace's `schedule_min_role` (default: admin). Audited."""
    return ctl.create_schedule(access, saved_id, body, db, request)


@router.patch(
    "/saved-reports/{saved_id}/schedules/{schedule_id}",
    response_model=SuccessResponse[ScheduleSummary],
    responses={**COMMON_ERRORS, **VALIDATION_ERROR, **NOT_A_MEMBER, **NOT_FOUND, **FORBIDDEN,
               **BAD_RANGE},
    summary="Change a schedule",
    operation_id="update_report_schedule",
)
def update_schedule(saved_id: str, schedule_id: str, body: ScheduleUpdate, request: Request,
                    access: WorkspaceAccess = Depends(as_member),
                    db: Session = Depends(get_db)):
    """Only the fields present change; the next run is recomputed. Audited."""
    return ctl.update_schedule(access, saved_id, schedule_id, body, db, request)


@router.delete(
    "/saved-reports/{saved_id}/schedules/{schedule_id}",
    response_model=SuccessResponse[dict],
    responses={**COMMON_ERRORS, **NOT_A_MEMBER, **NOT_FOUND, **FORBIDDEN},
    summary="Delete a schedule",
    operation_id="delete_report_schedule",
)
def delete_schedule(saved_id: str, schedule_id: str, request: Request,
                    access: WorkspaceAccess = Depends(as_member),
                    db: Session = Depends(get_db)):
    """It stops. Audited."""
    return ctl.delete_schedule(access, saved_id, schedule_id, db, request)


@router.post(
    "/saved-reports/{saved_id}/schedules/{schedule_id}/run",
    response_model=SuccessResponse[dict],
    status_code=202,
    responses={**COMMON_ERRORS, **NOT_A_MEMBER, **NOT_FOUND, **FORBIDDEN},
    summary="Send a scheduled report now",
    operation_id="run_report_schedule",
)
def run_schedule(saved_id: str, schedule_id: str, request: Request,
                 access: WorkspaceAccess = Depends(as_member), db: Session = Depends(get_db)):
    """Queue it now, to its recipients, without moving its next run. Audited."""
    return ctl.run_schedule_now(access, saved_id, schedule_id, db, request)


# ---- settings ----------------------------------------------------------------------------


@router.get(
    "/settings/reports",
    response_model=SuccessResponse[ReportsSettings],
    responses={**COMMON_ERRORS, **NOT_A_MEMBER},
    summary="Get the workspace's report settings",
    operation_id="get_reports_settings",
)
def get_settings(access: WorkspaceAccess = Depends(as_guest), db: Session = Depends(get_db)):
    """Time zone, working days, and who may run, export, schedule and manage sprints."""
    return ctl.get_settings(access, db)


@router.patch(
    "/settings/reports",
    response_model=SuccessResponse[ReportsSettings],
    responses={**COMMON_ERRORS, **VALIDATION_ERROR, **NOT_A_MEMBER},
    summary="Change the workspace's report settings",
    operation_id="update_reports_settings",
)
def update_settings(body: ReportsSettingsUpdate, request: Request,
                    access: WorkspaceAccess = Depends(as_admin),
                    db: Session = Depends(get_db)):
    """Admins. Only the fields present change. Audited."""
    return ctl.update_settings(access, body, db, request)
