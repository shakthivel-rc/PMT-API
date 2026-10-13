"""Daily snapshots of every issue's state — `issue_daily_status` (docs/design/08-reports.md
§5.2).

The cumulative-flow diagram reads a day from here when the day has a marker row in
`issue_snapshot_days`, and replays history for the days that have none — the marker is what
tells "no issues that day" apart from "never written". A snapshot stores every live issue,
done ones included, because the CFD's done band is made of them.

The worker writes yesterday for each workspace after midnight in its zone
(`workers/report_worker.py`); `scripts/backfill_snapshots.py` writes a range once. Writing
a day again replaces it, so both are safe to repeat.
"""
from __future__ import annotations

from datetime import date

from sqlalchemy.orm import Session

from models.hierarchy import WorkflowStatus
from models.issue import Issue
from models.reporting import IssueDailyStatus, IssueSnapshotDay
from services.reports.history import History
from utils import report_math as rm


def write_day(db: Session, workspace_id: str, day: date, tz_name: str | None) -> int:
    """Write `day` for one workspace — the state at the end of the day in its zone — and
    return how many issues it holds. Flushes; the caller commits."""
    tz = rm.zone(tz_name)
    at = rm.end_of_day_utc(day, tz)
    issues = db.query(Issue).filter(Issue.workspace_id == workspace_id, Issue.deleted == 0,
                                    Issue.created_at < at).all()
    categories = dict(db.query(WorkflowStatus.id, WorkflowStatus.category).filter(
        WorkflowStatus.workspace_id == workspace_id).all())
    history = History(db, workspace_id, issues)
    db.query(IssueDailyStatus).filter(IssueDailyStatus.workspace_id == workspace_id,
                                      IssueDailyStatus.snapshot_date == day).delete(
        synchronize_session=False)
    count = 0
    for issue in issues:
        status_id = history.status_at(issue.id, at)
        if status_id is None:
            continue
        # `status_category` is the stage's category as it is when the day is written —
        # `canceled` included, as itself, never folded into `done` — and it is a record, not
        # an input: the CFD maps `status_id` through the categories as they are now, so a
        # stage re-categorised later re-labels its past days too (09-restructuring.md §4.2).
        db.add(IssueDailyStatus(
            snapshot_date=day, issue_id=issue.id, workspace_id=workspace_id,
            space_id=issue.space_id, list_id=issue.list_id, status_id=status_id,
            status_category=categories.get(status_id, "todo"),
            assignee_id=history.assignee_at(issue.id, at),
            story_points=history.points_at(issue.id, at),
            time_estimate_seconds=history.estimate_at(issue.id, at),
            sprint_id=history.sprint_at(issue.id, at)))
        count += 1
    marker = db.query(IssueSnapshotDay).filter(
        IssueSnapshotDay.workspace_id == workspace_id,
        IssueSnapshotDay.snapshot_date == day).first()
    if marker is None:
        db.add(IssueSnapshotDay(workspace_id=workspace_id, snapshot_date=day,
                                issue_count=count))
    else:
        marker.issue_count = count
    db.flush()
    return count


def has_day(db: Session, workspace_id: str, day: date) -> bool:
    return db.query(IssueSnapshotDay.snapshot_date).filter(
        IssueSnapshotDay.workspace_id == workspace_id,
        IssueSnapshotDay.snapshot_date == day).first() is not None


def marked_days(db: Session, workspace_id: str, start: date, end: date) -> set[date]:
    rows = db.query(IssueSnapshotDay.snapshot_date).filter(
        IssueSnapshotDay.workspace_id == workspace_id,
        IssueSnapshotDay.snapshot_date >= start, IssueSnapshotDay.snapshot_date <= end).all()
    return {d for (d,) in rows}


def backfill(db: Session, workspace_id: str, start: date, end: date,
             tz_name: str | None) -> int:
    """Write every day from `start` to `end`. Returns the number of days written."""
    days = rm.days_between(start, end)
    for day in days:
        write_day(db, workspace_id, day, tz_name)
    return len(days)
