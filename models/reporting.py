"""Reporting support tables: daily snapshots, a result cache, saved reports, schedules, jobs.

docs/design/08-reports.md is the design; the short version of each table:

* `issue_daily_status` — every live issue's status, assignee, points and sprint at the end
  of each day, in the workspace's time zone. Cumulative flow, burndown and burnup read it.
  It is a *cache of a replay*: `issue_events` remains the record, and
  `services/reports/history.py` can rebuild any day from the events alone, which is what it
  does for a day nobody has snapshotted yet. Every live issue is stored, not only open ones,
  because a cumulative flow diagram's `done` band is a count of done issues.
* `issue_snapshot_days` — which days have been written. A day with no issues has no rows in
  `issue_daily_status`, so "no rows" cannot tell "nothing existed" from "never snapshotted".
* `report_cache` — a computed `ReportResult`, keyed by a hash of the request and of the
  workspace's data version (see `services/reports/cache.py`). Short-lived; a cache, never a
  record.
* `saved_reports`, `report_schedules` — a named report (its type and parameters) and when to
  email it to whom.
* `report_jobs` — the export and schedule queue, claimed by `workers/report_worker.py` with
  `FOR UPDATE SKIP LOCKED` and a lease, exactly like `ai_jobs`. The finished file is kept in
  the row (`file_blob`) rather than on disk: the containers write nothing to disk
  (CLAUDE.md §14.3), there is no object store yet, and files expire after a day.
"""
import uuid
from datetime import UTC, datetime

from sqlalchemy import (
    CHAR,
    JSON,
    Boolean,
    Column,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    String,
    Text,
)
from sqlalchemy.dialects import mysql

from db.session import Base

#: MySQL's BLOB and TEXT stop at 64 KiB; an export of a month's timesheet does not fit. The
#: LONG variants hold 4 GiB; the application caps a file far below that (MAX_FILE_BYTES in
#: services/reports/jobs.py).
_LONG_BLOB = LargeBinary().with_variant(mysql.LONGBLOB(), "mysql")
_LONG_TEXT = Text().with_variant(mysql.LONGTEXT(), "mysql")

REPORT_JOB_KINDS: tuple[str, ...] = ("export", "schedule")
REPORT_JOB_STATUSES: tuple[str, ...] = ("queued", "running", "done", "failed")
SAVED_REPORT_VISIBILITY: tuple[str, ...] = ("private", "workspace")
SCHEDULE_CADENCES: tuple[str, ...] = ("daily", "weekly", "monthly")


def _now() -> datetime:
    return datetime.now(UTC)


class IssueDailyStatus(Base):
    __tablename__ = "issue_daily_status"
    __table_args__ = (
        Index("ix_issue_daily_status_scope", "workspace_id", "space_id", "snapshot_date"),
        Index("ix_issue_daily_status_sprint", "sprint_id", "snapshot_date"),
    )

    snapshot_date = Column(Date, primary_key=True)
    issue_id = Column(CHAR(36), ForeignKey("issues.id"), primary_key=True)
    workspace_id = Column(CHAR(36), ForeignKey("workspaces.id"), nullable=False)
    space_id = Column(CHAR(36), nullable=False)
    list_id = Column(CHAR(36), nullable=False)
    status_id = Column(CHAR(36), nullable=False)
    status_category = Column(String(20), nullable=False)
    assignee_id = Column(CHAR(36), nullable=True)
    story_points = Column(Integer, nullable=True)
    time_estimate_seconds = Column(Integer, nullable=True)
    sprint_id = Column(CHAR(36), nullable=True)


class IssueSnapshotDay(Base):
    __tablename__ = "issue_snapshot_days"

    workspace_id = Column(CHAR(36), ForeignKey("workspaces.id"), primary_key=True)
    snapshot_date = Column(Date, primary_key=True)
    issue_count = Column(Integer, nullable=False, default=0)
    created_at = Column(DateTime, default=_now, nullable=False)


class ReportCache(Base):
    __tablename__ = "report_cache"
    __table_args__ = (
        Index("ix_report_cache_expires", "expires_at"),
        Index("ix_report_cache_workspace", "workspace_id"),
    )

    key = Column(String(64), primary_key=True)
    workspace_id = Column(CHAR(36), ForeignKey("workspaces.id"), nullable=False)
    report_type = Column(String(40), nullable=False)
    params_json = Column(JSON, nullable=True)
    payload_json = Column(_LONG_TEXT, nullable=False)
    computed_at = Column(DateTime, default=_now, nullable=False)
    expires_at = Column(DateTime, nullable=False)


class SavedReport(Base):
    __tablename__ = "saved_reports"
    __table_args__ = (Index("ix_saved_reports_workspace_owner", "workspace_id", "owner_id"),)

    id = Column(CHAR(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    workspace_id = Column(CHAR(36), ForeignKey("workspaces.id"), nullable=False)
    owner_id = Column(CHAR(36), ForeignKey("users.id"), nullable=False)
    name = Column(String(120), nullable=False)
    report_type = Column(String(40), nullable=False)
    params = Column(JSON, nullable=True)
    visibility = Column(String(20), nullable=False, default="private")
    pinned = Column(Boolean, nullable=False, default=False)
    created_at = Column(DateTime, default=_now, nullable=False)
    updated_at = Column(DateTime, default=_now, onupdate=_now, nullable=False)
    deleted = Column(Integer, nullable=False, default=0)
    deleted_at = Column(DateTime, nullable=True)


class ReportSchedule(Base):
    __tablename__ = "report_schedules"
    __table_args__ = (
        # The scheduler's tick: enabled schedules that are due.
        Index("ix_report_schedules_due", "enabled", "next_run_at"),
        Index("ix_report_schedules_saved", "saved_report_id"),
    )

    id = Column(CHAR(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    workspace_id = Column(CHAR(36), ForeignKey("workspaces.id"), nullable=False)
    saved_report_id = Column(CHAR(36), ForeignKey("saved_reports.id"), nullable=False)
    cadence = Column(String(20), nullable=False, default="weekly")
    # 0 = Monday … 6 = Sunday, Python's `date.weekday()`. Weekly schedules only.
    weekday = Column(Integer, nullable=True)
    # 1-28. Monthly schedules only; 28 so every month has the day.
    day_of_month = Column(Integer, nullable=True)
    hour_local = Column(Integer, nullable=False, default=9)
    timezone = Column(String(64), nullable=False, default="UTC")
    format = Column(String(10), nullable=False, default="pdf")
    # A list of user ids, every one a member of the workspace when saved and when sent.
    recipients = Column(JSON, nullable=True)
    next_run_at = Column(DateTime, nullable=False)
    last_run_at = Column(DateTime, nullable=True)
    enabled = Column(Boolean, nullable=False, default=True)
    created_by = Column(CHAR(36), ForeignKey("users.id"), nullable=False)
    created_at = Column(DateTime, default=_now, nullable=False)
    updated_at = Column(DateTime, default=_now, onupdate=_now, nullable=False)
    deleted = Column(Integer, nullable=False, default=0)


class ReportJob(Base):
    __tablename__ = "report_jobs"
    __table_args__ = (
        Index("ix_report_jobs_status_available", "status", "available_at"),
        Index("ix_report_jobs_workspace_requester", "workspace_id", "requested_by",
              "created_at"),
        Index("ix_report_jobs_expires", "expires_at"),
    )

    id = Column(CHAR(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    workspace_id = Column(CHAR(36), ForeignKey("workspaces.id"), nullable=False)
    requested_by = Column(CHAR(36), ForeignKey("users.id"), nullable=False)
    kind = Column(String(20), nullable=False, default="export")
    report_type = Column(String(40), nullable=False)
    params = Column(JSON, nullable=True)
    # Export options: format, date_format, duration_format, rounding.
    options = Column(JSON, nullable=True)
    format = Column(String(10), nullable=False)
    schedule_id = Column(CHAR(36), ForeignKey("report_schedules.id"), nullable=True)
    status = Column(String(20), nullable=False, default="queued")
    attempts = Column(Integer, nullable=False, default=0)
    error = Column(Text, nullable=True)
    file_blob = Column(_LONG_BLOB, nullable=True)
    file_name = Column(String(200), nullable=True)
    content_type = Column(String(100), nullable=True)
    size_bytes = Column(Integer, nullable=True)
    row_count = Column(Integer, nullable=True)
    expires_at = Column(DateTime, nullable=True)
    available_at = Column(DateTime, default=_now, nullable=False)
    lease_expires_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=_now, nullable=False)
    started_at = Column(DateTime, nullable=True)
    finished_at = Column(DateTime, nullable=True)
