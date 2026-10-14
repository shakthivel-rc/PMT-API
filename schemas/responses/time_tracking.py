"""Payloads of the time-tracking routes (`routes/time_tracking.py`).

Durations are whole **seconds** everywhere. Clients round for display; nothing here is
rounded, so a sum of entries is always the sum the person sees when they add them up.
"""
from datetime import date, datetime

from pydantic import BaseModel, Field

from schemas.responses.issues import UserRef
from schemas.time_tracking import TimeTrackingSettings


class TimeIssueRef(BaseModel):
    """Enough of an issue to name it and to open it from a timesheet or the header timer."""

    id: str = Field(description="The issue's id.")
    workspace_id: str = Field(description="Its workspace, for building a link.")
    list_id: str = Field(description="Its list, for building a link.")
    key: str = Field(description="Human key, `PAY-142`.")
    title: str = Field(description="One line.")


class TimeEntrySummary(BaseModel):
    """One piece of time logged against an issue."""

    id: str = Field(description="Opaque id.")
    issue: TimeIssueRef = Field(description="The issue the time is on.")
    user: UserRef = Field(description="Who did the work.")
    created_by: UserRef = Field(description="Who recorded it — differs from `user` when an "
                                            "admin logged on someone's behalf.")
    work_date: date = Field(description="The day it counts for, in the logger's zone.")
    started_at: datetime | None = Field(description="When it started, UTC, if known.")
    duration_seconds: int = Field(description="How long, in seconds.")
    note: str | None = Field(description="What was done. Plain text; render as text.")
    source: str = Field(description="manual, timer or automation.")
    billable: bool = Field(description="Whether the time is billable to a client.")
    billing_rate: float | None = Field(default=None, description="Snapshot hourly billing rate.")
    cost_amount: float | None = Field(default=None, description="Calculated financial cost or value.")
    created_at: datetime = Field(description="UTC.")
    updated_at: datetime = Field(description="UTC.")
    can_edit: bool = Field(description="Whether YOU may change or delete it: your own entry "
                                       "while you can log time, or anyone's if you are an "
                                       "admin.")


class TimeEntryPage(BaseModel):
    """One page of time entries — a timesheet in list form."""

    records: list[TimeEntrySummary] = Field(description="The entries on this page, newest "
                                                        "work date first.")
    total: int = Field(description="How many entries match across ALL pages.")
    total_seconds: int = Field(description="Their summed duration across ALL pages.")
    total_cost_amount: float | None = Field(default=None, description="Summed cost amount across ALL pages.")
    currency: str = Field(default="USD", description="Workspace currency.")


class UserTime(BaseModel):
    """One person's share of the time on an issue."""

    user: UserRef = Field(description="The person.")
    seconds: int = Field(description="Their total on this issue, in seconds.")


class IssueTimeSummary(BaseModel):
    """An issue's time: its own, and rolled up over everything beneath it.

    *Own* and *roll-up* are separate numbers on purpose (ClickUp keeps `time_spent` and
    `rollup_time` apart for the same reason): a story with 2h of its own and 10h across its
    sub-tasks is a different situation from a story with 12h of its own.
    """

    spent_seconds: int = Field(description="Logged on this issue itself.")
    rollup_spent_seconds: int = Field(description="This issue plus every descendant.")
    estimate_seconds: int | None = Field(description="This issue's estimate, or null.")
    rollup_estimate_seconds: int | None = Field(
        description="Sum of the estimates of this issue and its descendants, or null when "
                    "none of them is estimated.",
    )
    remaining_seconds: int | None = Field(
        description="Estimate minus time logged on this issue, never below zero; null when "
                    "there is no estimate. Derived, never stored.",
    )
    entry_count: int = Field(description="How many entries are logged on this issue itself.")
    by_user: list[UserTime] = Field(
        description="Who logged the time on this issue itself, most first. Under the "
                    "workspace's `own` visibility a member sees only their own line.",
    )


class IssueTimeTracking(BaseModel):
    """Everything the issue drawer's time panel draws, in one read."""

    summary: IssueTimeSummary = Field(description="Totals.")
    entries: list[TimeEntrySummary] = Field(description="The work log, newest first, up to "
                                                        "the most recent 200.")
    entries_total: int = Field(description="How many entries you can see on this issue, "
                                           "which may exceed the length of `entries`.")
    settings: TimeTrackingSettings = Field(description="The workspace's settings.")
    can_log: bool = Field(description="Whether you may log time here now.")
    can_log_for_others: bool = Field(description="Whether you may log on someone's behalf.")


class ActiveTimerSummary(BaseModel):
    """Your running timer."""

    issue: TimeIssueRef = Field(description="What it is running on.")
    started_at: datetime = Field(description="When it started, UTC.")
    elapsed_seconds: int = Field(
        description="Seconds since it started, measured by the server as it answered. Tick "
                    "from this rather than from `started_at`: the client's clock is not "
                    "the server's.",
    )
    work_date: date = Field(description="The day the time will count for.")
    note: str | None = Field(description="Note given at start, if any.")


class TimerStartResult(BaseModel):
    """The timer you just started, and the entry saved from the one it replaced."""

    timer: ActiveTimerSummary = Field(description="The running timer.")
    stopped_entry: TimeEntrySummary | None = Field(
        description="The entry saved from the timer that was running before, or null.",
    )


class TimerStopResult(BaseModel):
    """What stopping your timer saved."""

    entry: TimeEntrySummary | None = Field(
        description="The entry saved, or null — when nothing was running (stopping twice is "
                    "harmless) or when the timer's issue or workspace is no longer yours "
                    "to log on, in which case the timer is discarded.",
    )
    capped: bool = Field(description="True when the timer ran past one day and the entry was "
                                     "cut to one day. Edit it to the real figure.")


class TimeReportRow(BaseModel):
    """One person's time on one issue on one day."""

    user: UserRef = Field(description="Who.")
    issue: TimeIssueRef = Field(description="On what.")
    work_date: date = Field(description="Which day.")
    seconds: int = Field(description="How long, summed, in seconds.")
    cost_amount: float | None = Field(default=None, description="Calculated financial value.")


class TimeReport(BaseModel):
    """Time summed by person, issue and day over a date range — the timesheet grid."""

    date_from: date = Field(description="First day included.")
    date_to: date = Field(description="Last day included.")
    total_seconds: int = Field(description="Everything in range you can see.")
    total_cost_amount: float | None = Field(default=None, description="Total financial value in range.")
    currency: str = Field(default="USD", description="Workspace currency.")
    rows: list[TimeReportRow] = Field(description="Ordered by day, then issue key.")
    truncated: bool = Field(description="True when there were more rows than one answer "
                                        "carries; narrow the range or filter by person.")
