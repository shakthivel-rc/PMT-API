"""Reports — the transaction boundary for running, exporting, saving and scheduling reports.

Who may do what (docs/design/08-reports.md §6 has the reasoning):

| Act | guest | member | admin / owner |
|---|---|---|---|
| Run a report | if `guest_access` (default on) | yes | yes |
| Export a file | if `export_min_role` allows (default: member) |||
| Save a report, privately or for the workspace | yes, when they may run reports |||
| Change or delete a saved report | its owner | its owner | anyone's |
| Schedule an emailed report | if `schedule_min_role` allows (default: admin) |||
| Change the report settings | — | — | yes |

What a report *shows* is narrowed further by the time-tracking visibility (`own`: a member's
time reports hold their own time only) — applied inside the engine, so every route and the
worker get it the same way.

A report computed for the screen is cached (`report_cache`); writing the cache is the only
write a GET makes, and losing that race to a concurrent identical request is harmless, so an
`IntegrityError` there is rolled back and the computed answer returned anyway.
"""
from __future__ import annotations

from fastapi import Request
from fastapi.responses import JSONResponse, Response
from pydantic import ValidationError
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from controllers.workspace_controller import _audit
from dependencies.workspace import WorkspaceAccess
from models.reporting import ReportJob, ReportSchedule, SavedReport
from models.workspace import Workspace
from schemas.reports import (
    ExportRequest,
    ReportParams,
    ReportsSettings,
    ReportsSettingsUpdate,
    SavedReportCreate,
    SavedReportUpdate,
    ScheduleCreate,
    ScheduleUpdate,
)
from schemas.response import SuccessResponse, error_json
from services.reports import engine, jobs, saved, scheduler
from services.reports import settings as report_settings
from services.reports.base import ReportError
from services.reports.exporters import export
from utils import time_tracking as tt

#: The job list shows this many of your most recent jobs.
JOB_LIST_SIZE = 50

_NO_EXPORT = "Your role cannot export reports in this workspace."
_NO_SCHEDULE = "Your role cannot schedule reports in this workspace."


def _refused(exc: ReportError):
    return error_json(exc.status, exc.message, exc.data)


def _settings(db: Session, access: WorkspaceAccess) -> ReportsSettings:
    return report_settings.load_settings(db, access.workspace_id)


def _may_view(db: Session, access: WorkspaceAccess):
    if not engine.can_view(access, _settings(db, access)):
        return error_json(403, "Guests cannot run reports in this workspace.")
    return None


# ---- running ---------------------------------------------------------------------------------


def catalog(access: WorkspaceAccess, db: Session):
    return SuccessResponse(status_code=200, message="Report catalog fetched",
                           data=engine.catalog(access, _settings(db, access)))


def run_report(access: WorkspaceAccess, report_type: str, params: ReportParams, db: Session):
    try:
        output = engine.run(db, access, report_type, params)
    except ReportError as exc:
        db.rollback()
        return _refused(exc)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
    return SuccessResponse(status_code=200, message="Report computed", data=output)


def export_report(access: WorkspaceAccess, report_type: str, body: ExportRequest,
                  db: Session, request: Request):
    settings = _settings(db, access)
    if not engine.can_view(access, settings):
        return error_json(403, "Guests cannot run reports in this workspace.")
    if not access.at_least(settings.export_min_role):
        return error_json(403, _NO_EXPORT)
    options = jobs.options_of(body)
    params = body.params.normalized()
    result = None
    try:
        engine.definition(report_type)
        if not body.background:
            result = engine.compute(db, access, report_type, body.params,
                                    max_rows=engine.MAX_EXPORT_ROWS)
        else:
            engine.build_context(db, access, report_type, body.params)
    except ReportError as exc:
        db.rollback()
        return _refused(exc)

    if result is not None and jobs.total_rows(result) <= jobs.SYNC_EXPORT_ROWS:
        content, content_type, file_name = export(result, body.format, options)
        _audit(db, request, access.user_id, "EXPORT", "report", report_type,
               f"Exported {report_type} as {body.format} ({jobs.total_rows(result)} rows)")
        db.commit()
        return Response(content=content, media_type=content_type, headers={
            "Content-Disposition": f'attachment; filename="{file_name}"',
            "X-Report-Rows": str(jobs.total_rows(result)),
            "Cache-Control": "no-store"})

    job = jobs.enqueue(db, workspace_id=access.workspace_id, requested_by=access.user_id,
                       kind="export", report_type=report_type, params=params,
                       fmt=body.format, options=options)
    _audit(db, request, access.user_id, "EXPORT", "report", report_type,
           f"Queued export of {report_type} as {body.format} (job {job.id})")
    db.commit()
    # The route answers 200 with a file or 202 with a job, so the status is set here.
    queued = SuccessResponse(status_code=202, message="Export queued",
                             data=jobs.payload(job, access.user_id))
    return JSONResponse(status_code=202, content=queued.model_dump(mode="json"))


# ---- jobs ------------------------------------------------------------------------------------


def list_jobs(access: WorkspaceAccess, db: Session):
    rows = (db.query(ReportJob).filter(ReportJob.workspace_id == access.workspace_id)
            .order_by(ReportJob.created_at.desc()).limit(JOB_LIST_SIZE * 4).all())
    mine = [job for job in rows if jobs.can_read(job, access.user_id)][:JOB_LIST_SIZE]
    return SuccessResponse(status_code=200, message="Report jobs fetched",
                           data=[jobs.payload(job, access.user_id) for job in mine])


def _readable_job(db: Session, access: WorkspaceAccess, job_id: str) -> ReportJob | None:
    job = jobs.get_job(db, access.workspace_id, job_id)
    return job if job is not None and jobs.can_read(job, access.user_id) else None


def get_job(access: WorkspaceAccess, job_id: str, db: Session):
    job = _readable_job(db, access, job_id)
    if job is None:
        return error_json(404, "Report job not found")
    return SuccessResponse(status_code=200, message="Report job fetched",
                           data=jobs.payload(job, access.user_id))


def download_job(access: WorkspaceAccess, job_id: str, token: str | None, db: Session):
    job = _readable_job(db, access, job_id)
    if job is None:
        return error_json(404, "Report job not found")
    if not jobs.token_valid(token, job.id, access.user_id):
        return error_json(403, "This download link is invalid or has expired.")
    if job.status != "done":
        return error_json(409, "The report is not ready yet.", {"status": job.status})
    if job.file_blob is None or (job.expires_at and job.expires_at <= tt.utc_now()):
        return error_json(410, "This file has expired. Export the report again.")
    return Response(content=job.file_blob, media_type=job.content_type, headers={
        "Content-Disposition": f'attachment; filename="{job.file_name}"',
        "Cache-Control": "no-store"})


# ---- saved reports ---------------------------------------------------------------------------


def list_saved(access: WorkspaceAccess, db: Session):
    refused = _may_view(db, access)
    if refused is not None:
        return refused
    return SuccessResponse(status_code=200, message="Saved reports fetched",
                           data=saved.list_payloads(db, access))


def get_saved(access: WorkspaceAccess, saved_id: str, db: Session):
    refused = _may_view(db, access)
    if refused is not None:
        return refused
    try:
        found = saved.get_visible(db, access, saved_id)
    except ReportError as exc:
        return _refused(exc)
    return SuccessResponse(status_code=200, message="Saved report fetched",
                           data=saved.payload(db, access, found))


def create_saved(access: WorkspaceAccess, body: SavedReportCreate, db: Session):
    refused = _may_view(db, access)
    if refused is not None:
        return refused
    try:
        saved.check_type(body.report_type)
    except ReportError as exc:
        return _refused(exc)
    if body.visibility == "workspace" and access.role == "guest":
        return error_json(403, "Guests can only save private reports.")
    row = SavedReport(workspace_id=access.workspace_id, owner_id=access.user_id,
                      name=body.name.strip(), report_type=body.report_type,
                      params=body.params.normalized(), visibility=body.visibility,
                      pinned=body.pinned)
    db.add(row)
    db.commit()
    return SuccessResponse(status_code=201, message="Report saved",
                           data=saved.payload(db, access, row))


def update_saved(access: WorkspaceAccess, saved_id: str, body: SavedReportUpdate,
                 db: Session):
    try:
        row = saved.get_visible(db, access, saved_id)
    except ReportError as exc:
        return _refused(exc)
    if not saved.can_change(access, row):
        return error_json(403, "Only its owner or an admin can change this report.")
    changes = body.model_fields_set
    for field in changes:
        if getattr(body, field) is None:
            return error_json(422, f"{field} cannot be empty.")
    if "visibility" in changes and body.visibility == "workspace" and access.role == "guest":
        return error_json(403, "Guests can only save private reports.")
    if "name" in changes and body.name is not None:
        row.name = body.name.strip()
    if "params" in changes and body.params is not None:
        row.params = body.params.normalized()
    if "visibility" in changes:
        row.visibility = body.visibility
    if "pinned" in changes:
        row.pinned = bool(body.pinned)
    db.commit()
    return SuccessResponse(status_code=200, message="Saved report updated",
                           data=saved.payload(db, access, row))


def delete_saved(access: WorkspaceAccess, saved_id: str, db: Session, request: Request):
    try:
        row = saved.get_visible(db, access, saved_id)
    except ReportError as exc:
        return _refused(exc)
    if not saved.can_change(access, row):
        return error_json(403, "Only its owner or an admin can delete this report.")
    saved.soft_delete(db, row)
    _audit(db, request, access.user_id, "DELETE", "saved_report", row.id,
           f"Deleted saved report {row.name}")
    db.commit()
    return SuccessResponse(status_code=200, message="Saved report deleted",
                           data={"id": row.id})


# ---- schedules -------------------------------------------------------------------------------


def _schedulable(db: Session, access: WorkspaceAccess, saved_id: str):
    """The saved report, when the caller may schedule it; else the refusal to return."""
    settings = _settings(db, access)
    try:
        row = saved.get_visible(db, access, saved_id)
    except ReportError as exc:
        return None, _refused(exc)
    if not access.at_least(settings.schedule_min_role):
        return None, error_json(403, _NO_SCHEDULE)
    return row, None


def list_schedules(access: WorkspaceAccess, saved_id: str, db: Session):
    try:
        row = saved.get_visible(db, access, saved_id)
    except ReportError as exc:
        return _refused(exc)
    return SuccessResponse(status_code=200, message="Schedules fetched",
                           data=saved.schedule_payloads(db, saved.schedules_of(db, row)))


def create_schedule(access: WorkspaceAccess, saved_id: str, body: ScheduleCreate,
                    db: Session, request: Request):
    row, refused = _schedulable(db, access, saved_id)
    if refused is not None:
        return refused
    try:
        recipients = saved.check_recipients(db, access.workspace_id, body.recipients)
    except ReportError as exc:
        return _refused(exc)
    schedule = ReportSchedule(
        workspace_id=access.workspace_id, saved_report_id=row.id, cadence=body.cadence,
        weekday=body.weekday if body.cadence == "weekly" else None,
        day_of_month=body.day_of_month if body.cadence == "monthly" else None,
        hour_local=body.hour_local,
        timezone=body.timezone or _settings(db, access).timezone, format=body.format,
        recipients=recipients, enabled=body.enabled, created_by=access.user_id)
    saved.reschedule(schedule)
    db.add(schedule)
    db.flush()
    _audit(db, request, access.user_id, "CREATE", "report_schedule", schedule.id,
           f"Scheduled {row.name}: {body.cadence} at {body.hour_local}:00, "
           f"{len(recipients)} recipient(s)")
    db.commit()
    return SuccessResponse(status_code=201, message="Schedule created",
                           data=saved.schedule_payloads(db, [schedule])[0])


def update_schedule(access: WorkspaceAccess, saved_id: str, schedule_id: str,
                    body: ScheduleUpdate, db: Session, request: Request):
    row, refused = _schedulable(db, access, saved_id)
    if refused is not None:
        return refused
    try:
        schedule = saved.get_schedule(db, row, schedule_id)
        changes = {field: getattr(body, field) for field in body.model_fields_set}
        for field, value in changes.items():
            if value is None and field not in ("weekday", "day_of_month"):
                return error_json(422, f"{field} cannot be empty.")
        if "recipients" in changes:
            changes["recipients"] = saved.check_recipients(db, access.workspace_id,
                                                           changes["recipients"])
    except ReportError as exc:
        return _refused(exc)
    for field, value in changes.items():
        setattr(schedule, field, value)
    if schedule.cadence == "weekly" and schedule.weekday is None:
        schedule.weekday = 0
    if schedule.cadence == "monthly" and schedule.day_of_month is None:
        schedule.day_of_month = 1
    saved.reschedule(schedule)
    if changes:
        summary = ", ".join(sorted(changes))
        _audit(db, request, access.user_id, "UPDATE", "report_schedule", schedule.id,
               f"Changed schedule of {row.name}: {summary}")
    db.commit()
    return SuccessResponse(status_code=200, message="Schedule updated",
                           data=saved.schedule_payloads(db, [schedule])[0])


def delete_schedule(access: WorkspaceAccess, saved_id: str, schedule_id: str, db: Session,
                    request: Request):
    row, refused = _schedulable(db, access, saved_id)
    if refused is not None:
        return refused
    try:
        schedule = saved.get_schedule(db, row, schedule_id)
    except ReportError as exc:
        return _refused(exc)
    schedule.deleted = 1
    schedule.enabled = False
    _audit(db, request, access.user_id, "DELETE", "report_schedule", schedule.id,
           f"Deleted schedule of {row.name}")
    db.commit()
    return SuccessResponse(status_code=200, message="Schedule deleted",
                           data={"id": schedule.id})


def run_schedule_now(access: WorkspaceAccess, saved_id: str, schedule_id: str, db: Session,
                     request: Request):
    row, refused = _schedulable(db, access, saved_id)
    if refused is not None:
        return refused
    try:
        schedule = saved.get_schedule(db, row, schedule_id)
    except ReportError as exc:
        return _refused(exc)
    job_ids = scheduler.enqueue_for(db, schedule, row)
    _audit(db, request, access.user_id, "EXPORT", "report_schedule", schedule.id,
           f"Ran schedule of {row.name} now ({len(job_ids)} job(s))")
    db.commit()
    return SuccessResponse(status_code=202, message="Scheduled report queued",
                           data={"job_ids": job_ids})


# ---- settings --------------------------------------------------------------------------------


def get_settings(access: WorkspaceAccess, db: Session):
    return SuccessResponse(status_code=200, message="Report settings fetched",
                           data=_settings(db, access).model_dump())


def update_settings(access: WorkspaceAccess, body: ReportsSettingsUpdate, db: Session,
                    request: Request):
    workspace = db.query(Workspace).filter(Workspace.id == access.workspace_id).one()
    current = report_settings.settings_for(workspace)
    changes = {field: getattr(body, field) for field in body.model_fields_set}
    for field, value in changes.items():
        if value is None:
            return error_json(422, f"{field} cannot be empty.")
    try:
        updated = ReportsSettings.model_validate({**current.model_dump(), **changes})
    except ValidationError as exc:
        return error_json(422, "Invalid report settings.",
                          {"errors": exc.errors(include_url=False, include_context=False)})
    report_settings.save_settings(workspace, updated)
    if changes:
        summary = ", ".join(f"{k}={v}" for k, v in sorted(changes.items()))
        _audit(db, request, access.user_id, "UPDATE", "workspace_settings", workspace.id,
               f"Report settings: {summary}")
    db.commit()
    return SuccessResponse(status_code=200, message="Report settings saved",
                           data=updated.model_dump())
