"""Time tracking: time logged against issues, and the one timer each person can have running.

The decisions behind this module are argued in docs/design/07-time-tracking.md, which
translates the Phase 2 sketch in docs/design/schema.sql into what runs today. In brief:

**The entry is the source of truth; every total is derived.** Nothing on `issues` counts
time spent. An issue's total, a person's week and a parent's roll-up are all `SUM`s over
this table, so there is no cached counter to drift (GitLab's cached total famously resets
when an issue moves) and logging time never writes the issue row — which matters, because
writing it would bump `issues.version` and turn every time log into a 409 for whoever is
editing the issue at that moment.

**Seconds, not minutes.** A timer measures seconds, and rounding each entry to a minute
loses up to 59 s per entry, forever: three 20-minute entries made of 19m 40s each are an
hour of work stored as 57 minutes. Storing full precision and rounding only for display is
the lesson the specialist trackers (Clockify, Toggl) teach. An INTEGER of seconds holds
68 years, so the unit costs nothing in range.

**Two dates, for two questions.** `started_at` is an instant (naive UTC, like every other
DateTime here), and it is optional: someone logging "3h yesterday" often does not know when
they started, and inventing "now" for them is the most common data-quality fault in Jira
worklogs. `work_date` is the *calendar day the work counts for*, in the logger's own time
zone, chosen by the client. Timesheets bucket by `work_date`, so a week never depends on
the server's zone or on a `users.timezone` column that does not exist yet.

**`user_id` is who did the work; `created_by` is who typed it in.** They differ only when an
admin logs on someone's behalf, and keeping both is the audit trail Asana and YouTrack keep.
Neither changes after creation: an entry is re-attributed by deleting it and logging anew.

**Soft delete**, like issues, so the `time_deleted` event in `issue_events` always has a row
to point back at.
"""
import uuid
from datetime import UTC, datetime

from sqlalchemy import (
    CHAR,
    Boolean,
    Column,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    text,
)

from db.session import Base
from utils import time_tracking as limits

#: Where an entry came from. `automation` is reserved for the "log time when a PR merges"
#: rule in docs/design/04-api-and-views.md; nothing writes it yet.
TIME_SOURCES: tuple[str, ...] = ("manual", "timer", "automation")

#: The entry and estimate ceilings live with the rest of the arithmetic in
#: utils/time_tracking.py, where the request schemas can import them without importing the
#: ORM; they are re-exported here so the model's limits are readable beside the model.
MAX_ENTRY_SECONDS = limits.MAX_ENTRY_SECONDS
MAX_ESTIMATE_SECONDS = limits.MAX_ESTIMATE_SECONDS


def _now() -> datetime:
    return datetime.now(UTC)


class TimeEntry(Base):
    __tablename__ = "time_entries"
    __table_args__ = (
        # An issue's work log, newest first; also the input to its total.
        Index("ix_time_entries_issue_work_date", "issue_id", "work_date"),
        # "My week" and the team timesheet: one person's entries in a date range, inside one
        # tenant. The same shape as the target schema's ix_time_entries_user_week.
        Index("ix_time_entries_workspace_user_work_date", "workspace_id", "user_id",
              "work_date"),
        # The team view without a person filter.
        Index("ix_time_entries_workspace_work_date", "workspace_id", "work_date"),
    )

    id = Column(CHAR(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    workspace_id = Column(CHAR(36), ForeignKey("workspaces.id"), nullable=False)
    issue_id = Column(CHAR(36), ForeignKey("issues.id"), nullable=False)
    # Who did the work.
    user_id = Column(CHAR(36), ForeignKey("users.id"), nullable=False)
    # Who recorded it. Equal to `user_id` unless an admin logged on someone's behalf.
    created_by = Column(CHAR(36), ForeignKey("users.id"), nullable=False)
    work_date = Column(Date, nullable=False)
    started_at = Column(DateTime, nullable=True)
    duration_seconds = Column(Integer, nullable=False)
    note = Column(Text, nullable=True)
    source = Column(String(20), nullable=False, default="manual")
    # Whether the time is billable to a client. Added 2026-10-05 with reports
    # (docs/design/08-reports.md §12, question 3): KnackForge bills clients by the hour, and
    # a billable report is the one finance asks for first. New entries take the workspace's
    # `billable_default` (schemas/time_tracking.TimeTrackingSettings) unless the request
    # says otherwise; the server default makes every entry logged before the column existed
    # billable, which is what the time was logged for.
    billable = Column(Boolean, nullable=False, default=True, server_default=text("1"))
    # Snapshot billing rate per hour and calculated cost amount for financial accounting
    billing_rate = Column(Numeric(10, 2), nullable=True)
    cost_amount = Column(Numeric(12, 2), nullable=True)
    created_at = Column(DateTime, default=_now, nullable=False)
    updated_at = Column(DateTime, default=_now, onupdate=_now, nullable=False)
    deleted = Column(Integer, nullable=False, default=0)
    deleted_at = Column(DateTime, nullable=True)


class ActiveTimer(Base):
    """A running timer. At most one per person, across every workspace they belong to.

    **A table of its own, keyed by the person, rather than an entry with no end.** "One
    running timer per user" is then the primary key, enforced by every database we run on.
    The alternative — `time_entries.ended_at IS NULL` plus a partial unique index — needs a
    partial index, which MySQL does not have; it becomes possible after the PostgreSQL
    cut-over, and nothing outside `services/time_tracking.py` would notice the switch.

    One per *person* rather than per workspace because that is what a person is doing: you
    cannot work on two tickets at once, and a header that shows "the" running timer needs
    exactly one to show (ClickUp's model). Stopping it writes a `time_entries` row with
    `source="timer"` and deletes this one in the same transaction.
    """

    __tablename__ = "active_timers"

    user_id = Column(CHAR(36), ForeignKey("users.id"), primary_key=True)
    workspace_id = Column(CHAR(36), ForeignKey("workspaces.id"), nullable=False)
    issue_id = Column(CHAR(36), ForeignKey("issues.id"), nullable=False)
    started_at = Column(DateTime, nullable=False)
    # The day the work counts for, in the starter's zone, fixed at start: a timer that runs
    # past midnight still belongs to the day it was started on, as in every timesheet tool.
    work_date = Column(Date, nullable=False)
    note = Column(Text, nullable=True)
