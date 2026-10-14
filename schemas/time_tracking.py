"""Request bodies for the time-tracking routes (`routes/time_tracking.py`), and the settings
document stored in `workspaces.settings["time_tracking"]`.

Instants arrive as **timezone-aware** datetimes (`AwareDatetime`) and are converted to naive
UTC before they are stored. A naive value is refused rather than guessed at: PyMySQL and
SQLite both silently drop an offset, so `10:00+05:30` would otherwise be stored as 10:00 UTC
— five and a half hours wrong, with nothing to show it (verified 2026-10-01; see
docs/design/07-time-tracking.md).
"""
from datetime import date
from typing import Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from utils.time_tracking import MAX_ENTRY_SECONDS

#: Free text on an entry. Long enough for "what I did and why it took so long", short enough
#: that a timesheet export of a month stays readable. Jira caps worklog comments far higher,
#: and the long ones are pasted logs nobody reads.
NOTE_MAX_LENGTH = 2_000

TimeVisibility = Literal["everyone", "own"]
WeekStart = Literal["monday", "sunday"]


class TimeEntryCreate(BaseModel):
    """Time spent on an issue."""

    model_config = ConfigDict(json_schema_extra={
        "examples": [{
            "duration_seconds": 5400, "work_date": "2026-10-01",
            "started_at": "2026-10-01T09:30:00+05:30", "note": "Reproduced the 3DS bug",
        }],
    })

    duration_seconds: int = Field(ge=1, le=MAX_ENTRY_SECONDS,
                                  description="How long, in seconds. At most one day.")
    work_date: date = Field(description="The calendar day the work counts for, in YOUR time "
                                        "zone. Timesheets bucket by this, not by "
                                        "`started_at`.")
    started_at: AwareDatetime | None = Field(
        default=None,
        description="When the work started, with an offset, if you know. Stored as UTC.",
    )
    note: str | None = Field(default=None, max_length=NOTE_MAX_LENGTH,
                             description="What was done. Plain text.")
    user_id: str | None = Field(
        default=None,
        description="Log on someone else's behalf. Admins only; they must be a member who "
                    "can log time. Omit to log your own time.",
    )
    billable: bool | None = Field(
        default=None,
        description="Whether the time is billable to a client. Omit for the workspace's "
                    "`billable_default`.",
    )
    billing_rate: float | None = Field(
        default=None, ge=0,
        description="Snapshot hourly rate. Omit to take the member or workspace default rate.",
    )


class TimeEntryUpdate(BaseModel):
    """A partial update of an entry. Only the fields present change; `null` clears.

    Who the time belongs to and which issue it is on cannot change: delete the entry and
    log it again, so the audit trail shows both acts.
    """

    model_config = ConfigDict(json_schema_extra={
        "examples": [{"duration_seconds": 3600, "note": None}],
    })

    duration_seconds: int | None = Field(default=None, ge=1, le=MAX_ENTRY_SECONDS,
                                         description="How long, in seconds.")
    work_date: date | None = Field(default=None, description="The day it counts for.")
    started_at: AwareDatetime | None = Field(default=None,
                                             description="When it started, or null.")
    note: str | None = Field(default=None, max_length=NOTE_MAX_LENGTH,
                             description="What was done, or null to clear.")
    billable: bool | None = Field(default=None, description="Billable to a client or not.")
    billing_rate: float | None = Field(default=None, ge=0, description="Snapshot hourly rate.")


class TimerStart(BaseModel):
    """Start a timer on an issue. Any timer you already have running is stopped and saved."""

    model_config = ConfigDict(json_schema_extra={
        "examples": [{"work_date": "2026-10-01", "note": "Pairing with Sam"}],
    })

    work_date: date | None = Field(
        default=None,
        description="The day the time will count for, in your zone. Default: today in UTC — "
                    "send it, because UTC's today is not yours for part of every day.",
    )
    note: str | None = Field(default=None, max_length=NOTE_MAX_LENGTH,
                             description="Carried onto the entry the timer becomes.")


class TimerStop(BaseModel):
    """Stop your running timer and save it as an entry."""

    model_config = ConfigDict(json_schema_extra={"examples": [{"note": "Fixed and pushed"}]})

    note: str | None = Field(default=None, max_length=NOTE_MAX_LENGTH,
                             description="Replaces the note given at start, when present.")


class TimeTrackingSettings(BaseModel):
    """How time tracking behaves in one workspace. Every field has a default, so a workspace
    that never saved settings behaves exactly as one that saved the defaults."""

    model_config = ConfigDict(json_schema_extra={
        "examples": [{"enabled": True, "hours_per_day": 8, "days_per_week": 5,
                      "visibility": "everyone", "week_start": "monday",
                      "billable_default": True}],
    })

    enabled: bool = Field(default=True,
                          description="Off: nobody can log time or run a timer here. Time "
                                      "already logged stays readable.")
    # Jira's defaults, and its range: a day of more than 24 hours is a typo.
    hours_per_day: float = Field(default=8, ge=1, le=24,
                                 description="What `1d` means when typing a duration.")
    days_per_week: int = Field(default=5, ge=1, le=7,
                               description="What `1w` means when typing a duration.")
    visibility: TimeVisibility = Field(
        default="everyone",
        description="`everyone`: every member sees who logged what. `own`: members see only "
                    "their own entries; admins see everyone's. Issue totals stay visible to "
                    "all — they are a property of the issue, not of a person.",
    )
    week_start: WeekStart = Field(default="monday", description="First column of a timesheet.")
    # True because the first users of this feature bill clients by the hour; a workspace
    # that tracks internal effort switches it off once (docs/design/08-reports.md §12).
    billable_default: bool = Field(default=True,
                                   description="Whether new time is billable unless the "
                                               "person logging it says otherwise.")


class TimeTrackingSettingsUpdate(BaseModel):
    """A partial update of the settings. Only the fields present change."""

    model_config = ConfigDict(json_schema_extra={"examples": [{"visibility": "own"}]})

    enabled: bool | None = Field(default=None, description="See `TimeTrackingSettings`.")
    hours_per_day: float | None = Field(default=None, ge=1, le=24,
                                        description="See `TimeTrackingSettings`.")
    days_per_week: int | None = Field(default=None, ge=1, le=7,
                                      description="See `TimeTrackingSettings`.")
    visibility: TimeVisibility | None = Field(default=None,
                                              description="See `TimeTrackingSettings`.")
    week_start: WeekStart | None = Field(default=None,
                                         description="See `TimeTrackingSettings`.")
    billable_default: bool | None = Field(default=None,
                                          description="See `TimeTrackingSettings`.")
