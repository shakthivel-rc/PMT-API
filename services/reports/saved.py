"""Saved reports and their schedules: storage, visibility and the payloads the API returns.

A saved report is a report type plus its parameters and a name — ClickUp's "saved report",
Jira's "saved filter + gadget" in one row. `private` ones are seen by their owner only;
`workspace` ones by every member who may run reports. Only the owner or an admin changes or
deletes one; a schedule hangs off it and is deleted with it.
"""
from __future__ import annotations

from sqlalchemy import or_
from sqlalchemy.orm import Session

from dependencies.workspace import WorkspaceAccess
from models.reporting import ReportSchedule, SavedReport
from services import issue as issue_svc
from services import time_tracking as tt_svc
from services.reports import scheduler
from services.reports.base import ReportError
from services.reports.registry import REPORTS
from utils import time_tracking as tt


def user_names(db: Session, ids) -> dict[str, str]:
    users = issue_svc.users_by_id(db, ids)
    return {user_id: issue_svc.display_name(user) for user_id, user in users.items()}


def visible_query(db: Session, access: WorkspaceAccess):
    return (db.query(SavedReport)
            .filter(SavedReport.workspace_id == access.workspace_id, SavedReport.deleted == 0,
                    or_(SavedReport.owner_id == access.user_id,
                        SavedReport.visibility == "workspace")))


def get_visible(db: Session, access: WorkspaceAccess, saved_id: str) -> SavedReport:
    found = visible_query(db, access).filter(SavedReport.id == saved_id).first()
    if found is None:
        raise ReportError(404, "Saved report not found")
    return found


def can_change(access: WorkspaceAccess, saved: SavedReport) -> bool:
    return saved.owner_id == access.user_id or access.at_least("admin")


def check_type(report_type: str) -> None:
    if report_type not in REPORTS:
        raise ReportError(400, f"Unknown report type '{report_type}'.")


def payload(db: Session, access: WorkspaceAccess, saved: SavedReport,
            names: dict[str, str] | None = None) -> dict:
    names = names if names is not None else user_names(db, [saved.owner_id])
    schedule_count = (db.query(ReportSchedule)
                      .filter(ReportSchedule.saved_report_id == saved.id,
                              ReportSchedule.deleted == 0).count())
    return {
        "id": saved.id, "name": saved.name, "report_type": saved.report_type,
        "params": saved.params or {}, "visibility": saved.visibility,
        "pinned": bool(saved.pinned), "owner_id": saved.owner_id,
        "owner_name": names.get(saved.owner_id), "can_edit": can_change(access, saved),
        "schedule_count": schedule_count, "created_at": saved.created_at,
        "updated_at": saved.updated_at,
    }


def list_payloads(db: Session, access: WorkspaceAccess) -> list[dict]:
    rows = (visible_query(db, access)
            .order_by(SavedReport.pinned.desc(), SavedReport.name).all())
    names = user_names(db, {row.owner_id for row in rows})
    return [payload(db, access, row, names) for row in rows]


def soft_delete(db: Session, saved: SavedReport) -> None:
    now = tt.utc_now()
    saved.deleted = 1
    saved.deleted_at = now
    for schedule in (db.query(ReportSchedule)
                     .filter(ReportSchedule.saved_report_id == saved.id,
                             ReportSchedule.deleted == 0)):
        schedule.deleted = 1
        schedule.enabled = False


# ---- schedules ---------------------------------------------------------------------------


def check_recipients(db: Session, workspace_id: str, recipients: list[str]) -> list[str]:
    """Distinct, in order, each a member of the workspace — else 400 naming the first stranger."""
    seen: list[str] = []
    for user_id in recipients:
        if user_id in seen:
            continue
        if tt_svc.member_role(db, workspace_id, user_id) is None:
            raise ReportError(400, "Every recipient must be a member of this workspace.",
                              {"user_id": user_id})
        seen.append(user_id)
    return seen


def get_schedule(db: Session, saved: SavedReport, schedule_id: str) -> ReportSchedule:
    found = (db.query(ReportSchedule)
             .filter(ReportSchedule.id == schedule_id,
                     ReportSchedule.saved_report_id == saved.id,
                     ReportSchedule.deleted == 0).first())
    if found is None:
        raise ReportError(404, "Schedule not found")
    return found


def schedules_of(db: Session, saved: SavedReport) -> list[ReportSchedule]:
    return (db.query(ReportSchedule)
            .filter(ReportSchedule.saved_report_id == saved.id, ReportSchedule.deleted == 0)
            .order_by(ReportSchedule.created_at).all())


def schedule_payload(schedule: ReportSchedule, names: dict[str, str]) -> dict:
    return {
        "id": schedule.id, "saved_report_id": schedule.saved_report_id,
        "cadence": schedule.cadence, "weekday": schedule.weekday,
        "day_of_month": schedule.day_of_month, "hour_local": schedule.hour_local,
        "timezone": schedule.timezone, "format": schedule.format,
        "recipients": [{"id": r, "name": names.get(r)} for r in schedule.recipients or []],
        "enabled": bool(schedule.enabled), "next_run_at": schedule.next_run_at,
        "last_run_at": schedule.last_run_at, "created_by": schedule.created_by,
        "created_at": schedule.created_at,
    }


def schedule_payloads(db: Session, schedules: list[ReportSchedule]) -> list[dict]:
    ids = {r for s in schedules for r in s.recipients or []}
    names = user_names(db, ids)
    return [schedule_payload(s, names) for s in schedules]


def reschedule(schedule: ReportSchedule) -> None:
    schedule.next_run_at = scheduler.schedule_next(schedule, tt.utc_now())
