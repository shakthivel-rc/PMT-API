"""Request bodies and parameters for the reports routes (`routes/reports.py`), and the
settings document stored in `workspaces.settings["reports"]`.

`ReportParams` is the one parameter model every report reads. A report ignores the fields
that mean nothing to it (a velocity chart has no date range); keeping one model is what lets
a saved report, an export and a schedule all store "the parameters" as one JSON document,
and lets the SPA keep them in the URL (docs/design/08-reports.md §8.1).
"""
from __future__ import annotations

from datetime import date
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from utils import report_math as rm

WorkspaceRole = Literal["guest", "member", "admin", "owner"]
IssueType = Literal["epic", "story", "task", "subtask", "bug"]
Priority = Literal["urgent", "high", "medium", "low", "none"]
#: Spelled out because a `Literal` must be, and kept equal to
#: `utils/workflow_rules.STATUS_CATEGORIES` by tests/integration/test_reports_canceled.py:
#: `canceled` joined on 2026-10-05 (docs/design/09-restructuring.md §4.2), and a filter that
#: refused it would leave canceled work the one thing a report could not be narrowed to.
StatusCategory = Literal["todo", "in_progress", "done", "canceled"]
DatePreset = Literal["today", "yesterday", "this_week", "last_week", "this_month",
                     "last_month", "last_30_days", "this_quarter", "last_quarter",
                     "this_year", "custom"]
GroupBy = Literal["user", "project", "list", "epic", "issue", "type", "billable", "week"]
Unit = Literal["points", "count", "hours"]
Bucket = Literal["day", "week", "month"]
BillableFilter = Literal["all", "billable", "non_billable"]
RoundingMode = Literal["up", "down", "nearest"]
ExportFormat = Literal["csv", "xlsx", "pdf"]
DurationFormat = Literal["decimal", "hhmm", "iso", "seconds"]
DateFormat = Literal["iso", "normal", "epoch"]

#: A filter list longer than this is a request to read the tenant, not a filter. 100 is more
#: people than a workspace in this phase has and more issues than anyone picks by hand.
MAX_FILTER_VALUES = 100

#: `workspace`, or `kind:id` for a space (project), folder, list or sprint.
SCOPE_PATTERN = r"^(workspace|(space|folder|list|sprint):[A-Za-z0-9-]{1,36})$"


class ReportParams(BaseModel):
    """What a report is about. Every field is optional; each report documents which it reads
    (`GET /reports/catalog`)."""

    model_config = ConfigDict(extra="forbid", json_schema_extra={
        "examples": [{"scope": "space:3f2a…", "preset": "last_week",
                      "group_by": ["user", "project"]}],
    })

    scope: str = Field(default="workspace", pattern=SCOPE_PATTERN,
                       description="`workspace`, or `space:<id>`, `folder:<id>`, "
                                   "`list:<id>`, `sprint:<id>`.")
    preset: DatePreset | None = Field(default=None,
                                      description="A named period, in the workspace's time "
                                                  "zone and week start. `custom` or omitted "
                                                  "uses `date_from`/`date_to`.")
    date_from: date | None = Field(default=None, description="First day included.")
    date_to: date | None = Field(default=None, description="Last day included.")
    user_ids: list[str] = Field(default_factory=list, max_length=MAX_FILTER_VALUES,
                                description="Time reports: only these people's time.")
    assignee_ids: list[str] = Field(default_factory=list, max_length=MAX_FILTER_VALUES,
                                    description="Only issues assigned to these people. "
                                                "`none` matches unassigned.")
    reporter_ids: list[str] = Field(default_factory=list, max_length=MAX_FILTER_VALUES)
    issue_ids: list[str] = Field(default_factory=list, max_length=MAX_FILTER_VALUES)
    types: list[IssueType] = Field(default_factory=list)
    priorities: list[Priority] = Field(default_factory=list)
    status_categories: list[StatusCategory] = Field(
        default_factory=list,
        description="Only issues whose stage is in one of these categories now: `todo`, "
                    "`in_progress`, `done` (delivered) or `canceled` (closed, not "
                    "delivered).")
    epic_id: str | None = Field(default=None, description="Only this epic and everything "
                                                          "beneath it.")
    sprint_id: str | None = Field(default=None, description="Sprint reports: which sprint. "
                                                            "Default: the scope's active one.")
    billable: BillableFilter = Field(default="all", description="Time reports only.")
    group_by: list[GroupBy] = Field(default_factory=list, max_length=2,
                                    description="Time by grouping: one or two levels.")
    unit: Unit = Field(default="points", description="Burndown and burnup: what is counted.")
    bucket: Bucket = Field(default="week", description="Created vs resolved: bucket size.")
    cumulative: bool = Field(default=False, description="Created vs resolved: running totals.")
    rounding_minutes: Literal[0, 1, 5, 6, 10, 15, 30] = Field(
        default=0, description="Round each time entry to this many minutes before summing. "
                               "0: no rounding.")
    rounding_mode: RoundingMode = Field(default="nearest")
    sprint_count: int = Field(default=7, ge=1, le=25,
                              description="Velocity: how many closed sprints. Jira shows 7.")

    @model_validator(mode="after")
    def _distinct_groups(self) -> ReportParams:
        if len(set(self.group_by)) != len(self.group_by):
            raise ValueError("group_by levels must differ")
        return self

    def normalized(self) -> dict:
        """A canonical JSON document of the parameters: sorted lists, defaults dropped.
        The cache key and saved reports use it, so two spellings of one request match."""
        data = self.model_dump(mode="json", exclude_defaults=True)
        for key, value in list(data.items()):
            if isinstance(value, list) and key != "group_by":
                data[key] = sorted(value)
        return data


class ExportRequest(BaseModel):
    """Export a report as a file."""

    model_config = ConfigDict(extra="forbid", json_schema_extra={
        "examples": [{"params": {"preset": "last_week"}, "format": "csv",
                      "duration_format": "decimal"}],
    })

    params: ReportParams = Field(default_factory=ReportParams)
    format: ExportFormat = Field(default="csv")
    date_format: DateFormat = Field(default="iso")
    duration_format: DurationFormat = Field(default="decimal")
    background: bool = Field(default=False,
                             description="Always run as a job, even when small enough to "
                                         "download at once.")


class ReportsSettings(BaseModel):
    """How reports behave in one workspace. Every field has a default."""

    model_config = ConfigDict(json_schema_extra={
        "examples": [{"timezone": "Asia/Kolkata", "working_days": [0, 1, 2, 3, 4],
                      "guest_access": True, "export_min_role": "member",
                      "schedule_min_role": "admin", "sprint_manage_role": "admin"}],
    })

    timezone: str = Field(default="UTC", max_length=64,
                          description="IANA zone. Day boundaries of every report, snapshot "
                                      "and schedule. There is no per-user zone yet.")
    working_days: list[int] = Field(default_factory=lambda: list(rm.DEFAULT_WORKING_DAYS),
                                    max_length=7,
                                    description="0 = Monday … 6 = Sunday. The burndown "
                                                "guideline stays flat on the other days.")
    guest_access: bool = Field(default=True, description="Whether guests may run reports.")
    export_min_role: WorkspaceRole = Field(default="member",
                                           description="The least role that may export "
                                                       "(ClickUp's Exporting permission).")
    schedule_min_role: WorkspaceRole = Field(default="admin",
                                             description="The least role that may schedule "
                                                         "emailed reports.")
    sprint_manage_role: WorkspaceRole = Field(default="admin",
                                              description="The least role that may start "
                                                          "and close sprints.")

    @field_validator("timezone")
    @classmethod
    def _known_zone(cls, value: str) -> str:
        if not rm.is_known_zone(value):
            raise ValueError("unknown time zone; use an IANA name such as Asia/Kolkata")
        return value

    @field_validator("working_days")
    @classmethod
    def _weekdays(cls, value: list[int]) -> list[int]:
        if any(day < 0 or day > 6 for day in value):
            raise ValueError("working days are 0 (Monday) to 6 (Sunday)")
        return sorted(set(value))


class ReportsSettingsUpdate(BaseModel):
    """A partial update of the settings. Only the fields present change."""

    model_config = ConfigDict(extra="forbid", json_schema_extra={
        "examples": [{"timezone": "Europe/Berlin"}]})

    timezone: str | None = Field(default=None, max_length=64)
    working_days: list[int] | None = Field(default=None, max_length=7)
    guest_access: bool | None = None
    export_min_role: WorkspaceRole | None = None
    schedule_min_role: WorkspaceRole | None = None
    sprint_manage_role: WorkspaceRole | None = None


class SavedReportCreate(BaseModel):
    model_config = ConfigDict(extra="forbid", json_schema_extra={
        "examples": [{"name": "Team week", "report_type": "timesheet",
                      "params": {"preset": "last_week"}, "visibility": "workspace"}],
    })

    name: str = Field(min_length=1, max_length=120)
    report_type: str = Field(min_length=1, max_length=40)
    params: ReportParams = Field(default_factory=ReportParams)
    visibility: Literal["private", "workspace"] = Field(default="private")
    pinned: bool = Field(default=False)


class SavedReportUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid", json_schema_extra={
        "examples": [{"pinned": True}]})

    name: str | None = Field(default=None, min_length=1, max_length=120)
    params: ReportParams | None = None
    visibility: Literal["private", "workspace"] | None = None
    pinned: bool | None = None


class ScheduleCreate(BaseModel):
    """Email a saved report on a schedule, to members of the workspace."""

    model_config = ConfigDict(extra="forbid", json_schema_extra={
        "examples": [{"cadence": "weekly", "weekday": 0, "hour_local": 9,
                      "format": "pdf", "recipients": ["<user id>"]}],
    })

    cadence: Literal["daily", "weekly", "monthly"] = Field(default="weekly")
    weekday: int | None = Field(default=0, ge=0, le=6,
                                description="Weekly: 0 = Monday … 6 = Sunday.")
    day_of_month: int | None = Field(default=1, ge=1, le=28,
                                     description="Monthly: 1-28, so every month has it.")
    hour_local: int = Field(default=9, ge=0, le=23)
    timezone: str | None = Field(default=None, max_length=64,
                                 description="IANA zone. Default: the workspace's.")
    format: ExportFormat = Field(default="pdf")
    recipients: list[str] = Field(min_length=1, max_length=MAX_FILTER_VALUES,
                                  description="User ids; every one must be a member.")
    enabled: bool = Field(default=True)

    @field_validator("timezone")
    @classmethod
    def _known_zone(cls, value: str | None) -> str | None:
        if value is not None and not rm.is_known_zone(value):
            raise ValueError("unknown time zone; use an IANA name such as Asia/Kolkata")
        return value


class ScheduleUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid", json_schema_extra={
        "examples": [{"enabled": False}]})

    cadence: Literal["daily", "weekly", "monthly"] | None = None
    weekday: int | None = Field(default=None, ge=0, le=6)
    day_of_month: int | None = Field(default=None, ge=1, le=28)
    hour_local: int | None = Field(default=None, ge=0, le=23)
    timezone: str | None = Field(default=None, max_length=64)
    format: ExportFormat | None = None
    recipients: list[str] | None = Field(default=None, min_length=1,
                                         max_length=MAX_FILTER_VALUES)
    enabled: bool | None = None

    @field_validator("timezone")
    @classmethod
    def _known_zone(cls, value: str | None) -> str | None:
        if value is not None and not rm.is_known_zone(value):
            raise ValueError("unknown time zone; use an IANA name such as Asia/Kolkata")
        return value
