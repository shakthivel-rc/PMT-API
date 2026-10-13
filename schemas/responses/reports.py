"""Payloads of the report routes (`routes/reports.py`).

A report result is deliberately one shape for all fifteen reports — `meta`, `columns`,
`rows`, `totals`, `chart`, `kpis`, `sections` — so the SPA has one table, one exporter and
one cache, and a new report is a new function rather than a new contract. The cells of a row
are keyed by `columns[].key`; their types (`duration` is whole seconds, `date` is ISO) are
in `columns[].type`. Charts differ by kind and are described in docs/design/08-reports.md.
"""
from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

from schemas.reports import ReportsSettings


class ReportColumn(BaseModel):
    key: str = Field(description="The row key that holds this column's value.")
    label: str = Field(description="The heading to show.")
    type: str = Field(description="text, number, duration (seconds), percent, date, user, "
                                  "issue or status.")


class ReportKpi(BaseModel):
    key: str = Field(description="Stable id.")
    label: str = Field(description="The caption.")
    value: Any = Field(description="Number, seconds or text, per `type`.")
    type: str = Field(description="number, duration, percent or text.")


class ReportSection(BaseModel):
    key: str = Field(description="Stable id.")
    title: str = Field(description="The heading.")
    columns: list[ReportColumn] = Field(description="Its columns.")
    rows: list[dict[str, Any]] = Field(description="Its rows.")
    totals: dict[str, Any] | None = Field(description="Its totals row, if any.")


class ReportResult(BaseModel):
    """One computed report."""

    meta: dict[str, Any] = Field(description="type, title, scope, scope_name, date_from, "
                                             "date_to, timezone, generated_at, notes, "
                                             "row_count, cached, restricted_to_own and "
                                             "report-specific keys.")
    columns: list[ReportColumn] = Field(description="The main table's columns.")
    rows: list[dict[str, Any]] = Field(description="The main table's rows.")
    totals: dict[str, Any] | None = Field(description="Its totals row, if any.")
    chart: dict[str, Any] | None = Field(description="Chart data: `kind` plus series.")
    kpis: list[ReportKpi] = Field(description="Headline numbers.")
    sections: list[ReportSection] = Field(description="Further tables (sprint report, "
                                                      "weekly report).")


class ReportType(BaseModel):
    type: str = Field(description="The id used in the URL.")
    title: str = Field(description="Its name.")
    category: str = Field(description="time, project, sprint or flow.")
    description: str = Field(description="One sentence.")
    dates: str = Field(description="time, range, week, sprint or none — how dates apply.")
    params: list[str] = Field(description="The parameters it reads.")
    chart: str | None = Field(description="Its chart kind, if it has one.")
    needs_sprints: bool = Field(description="Whether it needs a sprint scope.")
    max_days: int | None = Field(description="The longest range it accepts.")


class ReportCatalog(BaseModel):
    """What reports exist, and what YOU may do with them here."""

    reports: list[ReportType] = Field(description="Every report type.")
    can_view: bool = Field(description="Whether you may run reports at all.")
    export_formats: list[str] = Field(description="Formats you may export; empty if none.")
    can_schedule: bool = Field(description="Whether you may schedule emailed reports.")
    can_manage_sprints: bool = Field(description="Whether you may start and close sprints.")
    presets: list[str] = Field(description="Date presets.")
    limits: dict[str, int] = Field(description="Row and range limits.")
    settings: ReportsSettings = Field(description="The workspace's report settings.")


class ReportJobSummary(BaseModel):
    """A background export or a scheduled run."""

    id: str = Field(description="Opaque id.")
    kind: str = Field(description="export or schedule.")
    report_type: str = Field(description="The report.")
    format: str = Field(description="csv, xlsx or pdf.")
    status: str = Field(description="queued, running, done or failed.")
    error: str | None = Field(description="Why it failed, when it did.")
    file_name: str | None = Field(description="The file's name, when done.")
    size_bytes: int | None = Field(description="Its size.")
    row_count: int | None = Field(description="Rows written.")
    params: dict[str, Any] = Field(description="The report parameters.")
    created_at: datetime = Field(description="UTC.")
    finished_at: datetime | None = Field(description="UTC.")
    expires_at: datetime | None = Field(description="When the file is deleted. UTC.")
    download_path: str | None = Field(description="API path with a signed token, valid 24 "
                                                  "hours, for you only. Null until done.")


class SavedReportSummary(BaseModel):
    id: str = Field(description="Opaque id.")
    name: str = Field(description="Its name.")
    report_type: str = Field(description="The report.")
    params: dict[str, Any] = Field(description="The saved parameters.")
    visibility: str = Field(description="private or workspace.")
    pinned: bool = Field(description="Shown first.")
    owner_id: str = Field(description="Who saved it.")
    owner_name: str | None = Field(description="Their display name.")
    can_edit: bool = Field(description="Whether YOU may change or delete it.")
    schedule_count: int = Field(description="Schedules on it.")
    created_at: datetime = Field(description="UTC.")
    updated_at: datetime = Field(description="UTC.")


class ScheduleRecipient(BaseModel):
    id: str = Field(description="User id.")
    name: str | None = Field(description="Display name.")


class ScheduleSummary(BaseModel):
    id: str = Field(description="Opaque id.")
    saved_report_id: str = Field(description="The saved report it sends.")
    cadence: str = Field(description="daily, weekly or monthly.")
    weekday: int | None = Field(description="0 = Monday … 6 = Sunday, for weekly.")
    day_of_month: int | None = Field(description="1-28, for monthly.")
    hour_local: int = Field(description="Hour of the day in `timezone`.")
    timezone: str = Field(description="IANA zone.")
    format: str = Field(description="csv, xlsx or pdf.")
    recipients: list[ScheduleRecipient] = Field(description="Who gets it.")
    enabled: bool = Field(description="Whether it runs.")
    next_run_at: datetime = Field(description="UTC.")
    last_run_at: datetime | None = Field(description="UTC.")
    created_by: str = Field(description="Who made it.")
    created_at: datetime = Field(description="UTC.")
