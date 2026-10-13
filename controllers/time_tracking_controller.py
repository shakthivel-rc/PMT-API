"""Time tracking — the transaction boundary for entries, timers, reports and settings.

Who may do what (docs/design/07-time-tracking.md has the reasoning):

| Act | guest | member | admin / owner |
|---|---|---|---|
| See an issue's totals | yes | yes | yes |
| See other people's entries | under `everyone` | under `everyone` | always |
| Log, edit and delete own time; run a timer | — | yes | yes |
| Log on someone's behalf; edit or delete anyone's | — | — | yes |
| Change the workspace's settings | — | — | yes |

Refusals follow CLAUDE.md §8 and the workspace gate's own rule: an entry the caller cannot
see (another tenant's, or another member's under `own` visibility) is a 404; one they can
see but not change is a 403.

Administrative acts on somebody else's time — logging for them, changing or deleting their
entry — also go to the shared audit trail (`activity_logs`), the way the workspace
controller audits membership changes. A person's acts on their own time do not: their
`issue_events` rows already say who did what.

**Time on an archived or frozen container is read-only** (docs/design/09-restructuring.md
§3.4): logging, changing or deleting an entry and starting a timer answer 423, decided by
`services/write_guard` from the issue's list, folder and space. A frozen space's history is
exactly what a migration wave reconciles against (`history_reconciles`: seconds logged per
issue must equal the baseline), so time moving under it would fail the wave for a reason
nobody could see. Stopping a timer logs time too, so it is refused the same way — but the
timer is not lost: it keeps running and can be stopped once the space is writable again, or
discarded, which writes no time and so is always allowed. The guard answers as soon as the
issue is known — after its 404, and for an existing entry after the 403 that says it is not
yours to change — and before any rule about the entry itself (422s, and the admin-only
logging on someone's behalf): those are worth fixing only once the space can take the write.
"""
from datetime import date

from fastapi import Request
from fastapi.responses import JSONResponse
from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from controllers.workspace_controller import _audit
from dependencies.workspace import WorkspaceAccess
from models.issue import Issue
from models.time_tracking import ActiveTimer, TimeEntry
from models.workspace import Workspace
from schemas.response import SuccessResponse, error_json
from schemas.time_tracking import (
    TimeEntryCreate,
    TimeEntryUpdate,
    TimerStart,
    TimerStop,
    TimeTrackingSettings,
    TimeTrackingSettingsUpdate,
)
from services import issue as issue_svc
from services import time_tracking as svc
from services import write_guard
from utils import time_tracking as tt

#: A page of entries is capped for the same reason the issue list is
#: (controllers/issue_controller.MAX_PAGE_SIZE): one request must not read a tenant's history.
MAX_PAGE_SIZE = 200

#: The most rows one report answers with. A week for a team of fifty, each on a handful of
#: issues a day, is well under a thousand; this leaves room for a month and still bounds a
#: response that would otherwise grow with the size of the tenant.
MAX_REPORT_ROWS = 5_000

_SWITCHED_OFF = "Time tracking is switched off in this workspace."
_FUTURE_DATE = "That day has not started anywhere yet. Log time for today or earlier."
_FUTURE_START = "The start time is in the future."
_TIMER_KEEPS_RUNNING = ("Your timer keeps running: stop it once the space is writable again, "
                        "or discard it, which logs nothing.")


def _settings(db: Session, access: WorkspaceAccess) -> TimeTrackingSettings:
    return svc.load_settings(db, access.workspace_id)


def _locked(db: Session, issue: Issue) -> JSONResponse | None:
    """The 423 for time on `issue` when its list, folder or space is read-only, else None."""
    block = write_guard.issue_block(db, issue)
    return block.response() if block is not None else None


def _timer_locked(db: Session, timer: ActiveTimer) -> JSONResponse | None:
    """The 423 for stopping `timer` and saving its time now, or None when it may be.

    Only when stopping would write an entry. A timer whose issue is gone, or whose owner can
    no longer log time in its workspace, is discarded by `svc.finish_timer` and writes no
    time, so there is nothing for the guard to protect and it is let through as before.
    """
    issue = issue_svc.get_issue(db, timer.workspace_id, timer.issue_id)
    if issue is None or not svc.can_hold_time(db, timer.workspace_id, timer.user_id):
        return None
    block = write_guard.issue_block(db, issue)
    if block is None:
        return None
    return error_json(block.status_code, f"{block.message} {_TIMER_KEEPS_RUNNING}",
                      {"timer_running": True, "issue_id": timer.issue_id})


def _entry_problem(work_date: date | None, started_at) -> str | None:
    """Why the dates on an entry cannot be right, or None."""
    now = tt.utc_now()
    if work_date is not None and tt.is_future_work_date(work_date, now):
        return _FUTURE_DATE
    started = tt.to_naive_utc(started_at)
    if started is not None and tt.is_future_start(started, now):
        return _FUTURE_START
    return None


def _one_entry(db: Session, access: WorkspaceAccess, entry: TimeEntry) -> dict:
    return svc.entry_payloads(db, access, [entry])[0]


def _visible_entry(db: Session, access: WorkspaceAccess, settings: TimeTrackingSettings,
                   entry_id: str) -> TimeEntry | None:
    """The entry, if the caller may see it — otherwise None, which the caller answers 404."""
    entry = svc.get_entry(db, access.workspace_id, entry_id)
    only = svc.visible_user_id(access, settings)
    if entry is None or (only is not None and entry.user_id != only):
        return None
    return entry


# ---- the issue's time --------------------------------------------------------------------------


def get_issue_time(access: WorkspaceAccess, issue_id: str, db: Session):
    issue = issue_svc.get_issue(db, access.workspace_id, issue_id)
    if issue is None:
        return error_json(404, "Issue not found")
    settings = _settings(db, access)
    query = svc.entries_query(db, access.workspace_id).filter(TimeEntry.issue_id == issue.id)
    only = svc.visible_user_id(access, settings)
    if only is not None:
        query = query.filter(TimeEntry.user_id == only)
    total = query.count()
    rows = svc.newest_first(query).limit(svc.ISSUE_LOG_LIMIT).all()
    return SuccessResponse(
        status_code=200,
        message="Time tracking fetched",
        data={
            "summary": svc.issue_summary(db, access, issue, settings),
            "entries": svc.entry_payloads(db, access, rows),
            "entries_total": total,
            "settings": settings.model_dump(),
            "can_log": svc.can_log(access, settings),
            "can_log_for_others": svc.can_log_for_others(access, settings),
        },
    )


def create_entry(access: WorkspaceAccess, issue_id: str, body: TimeEntryCreate, db: Session,
                 request: Request):
    issue = issue_svc.get_issue(db, access.workspace_id, issue_id)
    if issue is None:
        return error_json(404, "Issue not found")
    locked = _locked(db, issue)
    if locked is not None:
        return locked
    settings = _settings(db, access)
    if not settings.enabled:
        return error_json(422, _SWITCHED_OFF)

    worker_id = body.user_id or access.user_id
    on_behalf = worker_id != access.user_id
    if on_behalf:
        if not svc.can_log_for_others(access, settings):
            return error_json(403, "Only an admin can log time on someone else's behalf.")
        if not svc.can_hold_time(db, access.workspace_id, worker_id):
            return error_json(422, "Time can only be logged for members of this workspace "
                                   "who can log time themselves.")
    problem = _entry_problem(body.work_date, body.started_at)
    if problem:
        return error_json(422, problem)

    entry = svc.log_time(
        db, issue, user_id=worker_id, created_by=access.user_id, work_date=body.work_date,
        started_at=body.started_at, duration_seconds=body.duration_seconds, note=body.note,
        billable=body.billable if body.billable is not None else settings.billable_default,
    )
    if on_behalf:
        _audit(db, request, access.user_id, "CREATE", "time_entry", entry.id,
               f"Logged {body.duration_seconds}s on issue {issue.id} for user {worker_id}")
    db.commit()
    return SuccessResponse(status_code=201, message="Time logged",
                           data=_one_entry(db, access, entry))


def update_entry(access: WorkspaceAccess, entry_id: str, body: TimeEntryUpdate, db: Session,
                 request: Request):
    settings = _settings(db, access)
    entry = _visible_entry(db, access, settings, entry_id)
    if entry is None:
        return error_json(404, "Time entry not found")
    if not svc.can_edit(access, entry.user_id):
        return error_json(403, "Only an admin can change someone else's time.")
    # `get_entry` joined the live issue, so it is there; the lookup carries the tenant all
    # the same (CLAUDE.md §8).
    issue = issue_svc.get_issue(db, access.workspace_id, entry.issue_id)
    if issue is None:
        return error_json(404, "Time entry not found")
    locked = _locked(db, issue)
    if locked is not None:
        return locked
    if not settings.enabled:
        return error_json(422, _SWITCHED_OFF)

    changed = body.model_fields_set
    for field in ("duration_seconds", "work_date", "billable"):
        if field in changed and getattr(body, field) is None:
            return error_json(422, f"{field} cannot be empty.")
    problem = _entry_problem(body.work_date if "work_date" in changed else None,
                             body.started_at if "started_at" in changed else None)
    if problem:
        return error_json(422, problem)

    before = entry.duration_seconds
    if "duration_seconds" in changed:
        entry.duration_seconds = body.duration_seconds
    if "work_date" in changed:
        entry.work_date = body.work_date
    if "started_at" in changed:
        entry.started_at = tt.to_naive_utc(body.started_at)
    if "note" in changed:
        entry.note = svc.clean_note(body.note)
    if "billable" in changed:
        entry.billable = body.billable
    if changed:
        issue_svc.record_event(db, issue, "time_updated", access.user_id,
                               from_value=str(before), to_value=str(entry.duration_seconds))
        if entry.user_id != access.user_id:
            _audit(db, request, access.user_id, "UPDATE", "time_entry", entry.id,
                   f"Changed {', '.join(sorted(changed))} of user {entry.user_id}'s time")
    db.commit()
    return SuccessResponse(status_code=200, message="Time entry updated",
                           data=_one_entry(db, access, entry))


def delete_entry(access: WorkspaceAccess, entry_id: str, db: Session, request: Request):
    settings = _settings(db, access)
    entry = _visible_entry(db, access, settings, entry_id)
    if entry is None:
        return error_json(404, "Time entry not found")
    if not svc.can_edit(access, entry.user_id):
        return error_json(403, "Only an admin can delete someone else's time.")
    issue = issue_svc.get_issue(db, access.workspace_id, entry.issue_id)
    if issue is None:
        return error_json(404, "Time entry not found")
    locked = _locked(db, issue)
    if locked is not None:
        return locked
    entry.deleted = 1
    entry.deleted_at = tt.utc_now()
    issue_svc.record_event(db, issue, "time_deleted", access.user_id,
                           from_value=str(entry.duration_seconds))
    if entry.user_id != access.user_id:
        _audit(db, request, access.user_id, "DELETE", "time_entry", entry.id,
               f"Deleted {entry.duration_seconds}s of user {entry.user_id}'s time")
    db.commit()
    return SuccessResponse(status_code=200, message="Time entry deleted", data={"id": entry.id})


# ---- the person's time ---------------------------------------------------------------------------


def _person_filter(access: WorkspaceAccess, settings: TimeTrackingSettings,
                   user_id: str | None):
    """The person a list or report is about, or a refusal.

    `me` is the caller. Under `own` visibility a member asking about someone else is told so
    with a 403 — they are a member, so there is nothing to disclose about the workspace, and
    "you cannot see this" is the useful answer — and asking about nobody in particular means
    asking about themselves.
    """
    wanted = access.user_id if user_id == "me" else user_id
    only = svc.visible_user_id(access, settings)
    if only is None:
        return wanted, None
    if wanted is not None and wanted != only:
        return None, error_json(403, "In this workspace only admins can see other people's "
                                     "time.")
    return only, None


def list_entries(access: WorkspaceAccess, db: Session, *, user_id: str | None,
                 issue_id: str | None, date_from: date | None, date_to: date | None,
                 page: int, limit: int):
    settings = _settings(db, access)
    person, refusal = _person_filter(access, settings, user_id)
    if refusal is not None:
        return refusal
    if date_from and date_to and date_to < date_from:
        return error_json(400, "The end date is before the start date.")
    limit = max(1, min(limit, MAX_PAGE_SIZE))

    query = svc.entries_query(db, access.workspace_id)
    if person:
        query = query.filter(TimeEntry.user_id == person)
    if issue_id:
        query = query.filter(TimeEntry.issue_id == issue_id)
    if date_from:
        query = query.filter(TimeEntry.work_date >= date_from)
    if date_to:
        query = query.filter(TimeEntry.work_date <= date_to)

    total = query.count()
    total_seconds = int(query.with_entities(func.sum(TimeEntry.duration_seconds)).scalar() or 0)
    rows = svc.newest_first(query).offset((page - 1) * limit).limit(limit).all()
    return SuccessResponse(
        status_code=200,
        message="Time entries fetched",
        data={"records": svc.entry_payloads(db, access, rows), "total": total,
              "total_seconds": total_seconds},
    )


def get_report(access: WorkspaceAccess, db: Session, *, date_from: date, date_to: date,
               user_id: str | None):
    settings = _settings(db, access)
    person, refusal = _person_filter(access, settings, user_id)
    if refusal is not None:
        return refusal
    problem = tt.report_range_problem(date_from, date_to)
    if problem:
        return error_json(400, problem)

    query = (
        svc.entries_query(db, access.workspace_id)
        .filter(TimeEntry.work_date >= date_from, TimeEntry.work_date <= date_to)
    )
    if person:
        query = query.filter(TimeEntry.user_id == person)
    grouped = (
        query.with_entities(TimeEntry.user_id, TimeEntry.issue_id, TimeEntry.work_date,
                            func.sum(TimeEntry.duration_seconds))
        .group_by(TimeEntry.user_id, TimeEntry.issue_id, TimeEntry.work_date)
        .order_by(TimeEntry.work_date, TimeEntry.issue_id, TimeEntry.user_id)
        .limit(MAX_REPORT_ROWS + 1)
        .all()
    )
    truncated = len(grouped) > MAX_REPORT_ROWS
    grouped = grouped[:MAX_REPORT_ROWS]
    total = int(query.with_entities(func.sum(TimeEntry.duration_seconds)).scalar() or 0)

    users = issue_svc.users_by_id(db, (row[0] for row in grouped))
    issues = svc.issue_refs(db, (row[1] for row in grouped))
    rows = [
        {"user": issue_svc.user_ref(users.get(uid)) or {"id": uid, "display_name": ""},
         "issue": issues[iid], "work_date": day, "seconds": int(seconds or 0)}
        for uid, iid, day, seconds in grouped
    ]
    rows.sort(key=lambda r: (r["work_date"], r["issue"]["key"], r["user"]["display_name"]))
    return SuccessResponse(
        status_code=200,
        message="Time report fetched",
        data={"date_from": date_from, "date_to": date_to, "total_seconds": total,
              "rows": rows, "truncated": truncated},
    )


# ---- timers ----------------------------------------------------------------------------------


def start_timer(access: WorkspaceAccess, issue_id: str, body: TimerStart | None, db: Session):
    """Start a timer on the issue, stopping and saving whatever was running before.

    Starting the timer that is already running is answered with it, unchanged: a double
    click or a retried request must not reset the clock or save a one-second entry.

    A timer on an issue whose container is read-only cannot be started (423). Nor can one
    anywhere while the timer already running is on such an issue: starting would stop that
    timer and log its time there — the write the stop route refuses — and starting while
    throwing the old timer away would lose the time without being asked. That timer keeps
    running; the answer says how to stop it, or discard it.
    """
    issue = issue_svc.get_issue(db, access.workspace_id, issue_id)
    if issue is None:
        return error_json(404, "Issue not found")
    locked = _locked(db, issue)
    if locked is not None:
        return locked
    settings = _settings(db, access)
    if not settings.enabled:
        return error_json(422, _SWITCHED_OFF)
    body = body or TimerStart()
    now = tt.utc_now()
    if body.work_date is not None and tt.is_future_work_date(body.work_date, now):
        return error_json(422, _FUTURE_DATE)

    running = svc.active_timer(db, access.user_id)
    if running is not None and running.issue_id == issue.id:
        return SuccessResponse(status_code=201, message="Timer already running",
                               data={"timer": svc.timer_payload(db, running, now),
                                     "stopped_entry": None})
    stopped = None
    if running is not None:
        held = _timer_locked(db, running)
        if held is not None:
            return held
        stopped, _capped = svc.finish_timer(db, running, now)
    timer = ActiveTimer(user_id=access.user_id, workspace_id=access.workspace_id,
                        issue_id=issue.id, started_at=now,
                        work_date=body.work_date or now.date(), note=svc.clean_note(body.note))
    db.add(timer)
    try:
        db.commit()
    except IntegrityError:
        # Two starts from two tabs at the same instant: the primary key let one through.
        db.rollback()
        return error_json(409, "Another timer was started at the same moment. Reload to see "
                               "which one is running.")
    stopped_payload = None
    if stopped is not None:
        # The stopped entry may be in another workspace; `can_edit` is about the caller's
        # own entry there, which is theirs by construction.
        stopped_payload = _one_entry(db, access, stopped)
        stopped_payload["can_edit"] = True
    return SuccessResponse(status_code=201, message="Timer started",
                           data={"timer": svc.timer_payload(db, timer, now),
                                 "stopped_entry": stopped_payload})


def get_timer(user_id: str, db: Session):
    """Your running timer, or `null`. No timer is a normal state, not a 404."""
    timer = svc.active_timer(db, user_id)
    if timer is None or svc.member_role(db, timer.workspace_id, user_id) is None:
        # A timer in a workspace you have since left is not shown; stopping it would discard
        # it, and it names an issue you can no longer read.
        return SuccessResponse(status_code=200, message="No timer running", data=None)
    return SuccessResponse(status_code=200, message="Timer fetched",
                           data=svc.timer_payload(db, timer, tt.utc_now()))


def stop_timer(user_id: str, body: TimerStop | None, db: Session):
    """Stop your timer and save it. Stopping when nothing runs answers `entry: null`, so a
    retried stop is harmless rather than an error to explain.

    When the timer's issue is in a read-only container the stop is refused (423) and the
    timer is left exactly as it was — still running, its start time untouched — so the time
    is not lost: it can be stopped once the space is writable again, or discarded."""
    timer = svc.active_timer(db, user_id)
    if timer is None:
        return SuccessResponse(status_code=200, message="No timer was running",
                               data={"entry": None, "capped": False})
    held = _timer_locked(db, timer)
    if held is not None:
        return held
    workspace_id = timer.workspace_id
    note = body.note if body is not None and "note" in body.model_fields_set else None
    entry, capped = svc.finish_timer(db, timer, tt.utc_now(), note)
    db.commit()
    if entry is None:
        return SuccessResponse(status_code=200,
                               message="The timer's issue is gone or no longer yours to log "
                                       "on, so the timer was discarded.",
                               data={"entry": None, "capped": False})
    role = svc.member_role(db, workspace_id, user_id) or "member"
    access = WorkspaceAccess(workspace_id=workspace_id, user_id=user_id, role=role)
    message = ("Timer stopped. It ran over a day, so the entry was cut to one day — edit it "
               "to the real figure." if capped else "Timer stopped and time logged")
    return SuccessResponse(status_code=200, message=message,
                           data={"entry": _one_entry(db, access, entry), "capped": capped})


def discard_timer(user_id: str, db: Session):
    """Throw your timer away. Never refused, even in a frozen or archived space: it writes
    no time, and it is the way out of a timer that cannot be stopped there."""
    timer = svc.active_timer(db, user_id)
    if timer is not None:
        db.delete(timer)
        db.commit()
    return SuccessResponse(status_code=200, message="Timer discarded",
                           data={"discarded": timer is not None})


# ---- settings ---------------------------------------------------------------------------------


def get_settings(access: WorkspaceAccess, db: Session):
    return SuccessResponse(status_code=200, message="Settings fetched",
                           data=_settings(db, access).model_dump())


def update_settings(access: WorkspaceAccess, body: TimeTrackingSettingsUpdate, db: Session,
                    request: Request):
    workspace = db.query(Workspace).filter(Workspace.id == access.workspace_id).one()
    current = svc.settings_for(workspace)
    changes = {field: getattr(body, field) for field in body.model_fields_set}
    for field, value in changes.items():
        if value is None:
            return error_json(422, f"{field} cannot be empty.")
    updated = current.model_copy(update=changes)
    svc.save_settings(workspace, updated)
    if changes:
        summary = ", ".join(f"{k}={v}" for k, v in sorted(changes.items()))
        _audit(db, request, access.user_id, "UPDATE", "workspace_settings", workspace.id,
               f"Time tracking settings: {summary}")
    db.commit()
    return SuccessResponse(status_code=200, message="Settings saved", data=updated.model_dump())
