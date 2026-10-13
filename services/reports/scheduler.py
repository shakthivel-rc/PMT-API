"""Scheduled reports: when the next run is, and turning a due schedule into jobs.

**Times.** A schedule says "Mondays at 09:00 in Asia/Kolkata". `next_run` finds the first
such local wall-clock moment after a given instant and stores it as naive UTC, the way every
datetime in this database is stored. The arithmetic is done on local *dates* and the hour is
attached afterwards, so a daylight-saving change is absorbed rather than accumulated: a
09:00 London schedule is 08:00 UTC in summer and 09:00 UTC in winter, never 10:00. A local
hour that does not exist (the spring-forward gap) resolves the way `zoneinfo` resolves it —
an hour later — and is still a single run that day.

**Who sees what.** A scheduled report is computed as someone, because what a report may
show depends on who reads it (time visibility, guests). When the workspace's time
visibility is `own`, every recipient gets their own job, computed as them — one rendering
sent to everyone would show the schedule owner's view to people not allowed it. Otherwise
one job, computed as the person who created the schedule, goes to all recipients.
"""
from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta

from sqlalchemy.orm import Session

from models.reporting import ReportSchedule, SavedReport
from services import time_tracking as tt_svc
from services.reports import jobs
from utils import report_math as rm
from utils import time_tracking as tt

#: Schedules claimed per tick. A backlog drains over a few ticks rather than one long one.
BATCH = 50


def _local_moment(day: date, hour: int, zone_name: str) -> datetime:
    """`day` at `hour`:00 in the zone, as naive UTC."""
    local = datetime.combine(day, time(hour), tzinfo=rm.zone(zone_name))
    return local.astimezone(UTC).replace(tzinfo=None)


def _matches(day: date, cadence: str, weekday: int | None, day_of_month: int | None) -> bool:
    if cadence == "daily":
        return True
    if cadence == "weekly":
        return day.weekday() == (weekday if weekday is not None else 0)
    return day.day == (day_of_month or 1)


def next_run(cadence: str, weekday: int | None, day_of_month: int | None, hour_local: int,
             zone_name: str, after: datetime) -> datetime:
    """The first scheduled moment strictly after `after` (naive UTC), as naive UTC."""
    tz = rm.zone(zone_name)
    start = after.replace(tzinfo=UTC).astimezone(tz).date()
    # 62 days covers a monthly schedule from any starting day.
    for offset in range(0, 63):
        day = start + timedelta(days=offset)
        if not _matches(day, cadence, weekday, day_of_month):
            continue
        moment = _local_moment(day, hour_local, zone_name)
        if moment > after:
            return moment
    raise ValueError("no run found in two months")  # unreachable for valid input


def schedule_next(schedule: ReportSchedule, after: datetime) -> datetime:
    return next_run(schedule.cadence, schedule.weekday, schedule.day_of_month,
                    schedule.hour_local, schedule.timezone, after)


def enqueue_for(db: Session, schedule: ReportSchedule, saved: SavedReport) -> list[str]:
    """The job(s) one run of `schedule` needs. Returns their ids. Caller commits."""
    recipients = [r for r in (schedule.recipients or [])
                  if tt_svc.member_role(db, schedule.workspace_id, r)]
    if not recipients:
        return []
    visibility = tt_svc.load_settings(db, schedule.workspace_id).visibility
    base_options = {"schedule_name": saved.name, "saved_report_id": saved.id}
    if visibility == "own":
        groups = [(recipient, [recipient]) for recipient in recipients]
    else:
        groups = [(schedule.created_by, recipients)]
    ids = []
    for computed_as, to in groups:
        job = jobs.enqueue(db, workspace_id=schedule.workspace_id, requested_by=computed_as,
                           kind="schedule", report_type=saved.report_type,
                           params=saved.params or {}, fmt=schedule.format,
                           options={**base_options, "recipients": to},
                           schedule_id=schedule.id)
        ids.append(job.id)
    return ids


def run_due(db: Session, now: datetime | None = None) -> int:
    """Enqueue every due schedule and move it to its next run. Returns jobs enqueued.

    Rows are taken with `SKIP LOCKED`, so two workers ticking at once split the due
    schedules rather than both sending them. A run missed while no worker was up is sent
    once, late — not once per missed slot.
    """
    now = now or tt.utc_now()
    due = (db.query(ReportSchedule)
           .filter(ReportSchedule.enabled.is_(True), ReportSchedule.deleted == 0,
                   ReportSchedule.next_run_at <= now)
           .order_by(ReportSchedule.next_run_at)
           .limit(BATCH)
           .with_for_update(skip_locked=True)
           .all())
    count = 0
    for schedule in due:
        saved = db.get(SavedReport, schedule.saved_report_id)
        if saved is not None and not saved.deleted:
            count += len(enqueue_for(db, schedule, saved))
        schedule.last_run_at = now
        schedule.next_run_at = schedule_next(schedule, now)
    db.commit()
    return count
