"""The report catalogue: every report type, what it reads and how its dates are chosen.

`GET /reports/catalog` serves this list, the SPA builds its filter bar from `params`, and
the engine (`engine.py`) dispatches on `compute`. Adding a report is one function and one
entry here.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from services.reports import flow_reports, project_reports, sprint_reports, time_reports
from services.reports.base import ReportContext

#: The longest range a report of each kind covers. Time reports keep the Timesheet's
#: 92 days (utils/time_tracking.MAX_REPORT_DAYS); the others a year, which bounds the replay
#: at 366 days x the issues in scope.
MAX_TIME_DAYS = 92
MAX_RANGE_DAYS = 366

_TIME_FILTERS = ["scope", "preset", "date_from", "date_to", "user_ids", "assignee_ids",
                 "types", "priorities", "status_categories", "epic_id", "issue_ids", "billable",
                 "rounding_minutes", "rounding_mode"]
_ISSUE_FILTERS = ["scope", "assignee_ids", "reporter_ids", "types", "priorities",
                  "status_categories", "epic_id"]
# Reports over a date range read issues at each date, so a status-category filter (which is
# about *now*) does not apply to them.
_RANGE_FILTERS = (*_ISSUE_FILTERS[:-2], "epic_id", "preset", "date_from", "date_to")


@dataclass(frozen=True)
class ReportDefinition:
    type: str
    title: str
    category: str  # time | project | sprint | flow
    description: str
    compute: Callable[[ReportContext], dict]
    #: How the dates are chosen: `time` (≤ 92 days, default this week), `range` (≤ 366 days,
    #: default last 30 days), `week` (exactly one week), `sprint` (the sprint's own dates),
    #: or `none` (a report of the present state).
    dates: str
    params: tuple[str, ...]
    chart: str
    needs_sprints: bool = False

    def catalog_entry(self) -> dict:
        return {"type": self.type, "title": self.title, "category": self.category,
                "description": self.description, "dates": self.dates,
                "params": list(self.params), "chart": self.chart,
                "needs_sprints": self.needs_sprints,
                "max_days": MAX_TIME_DAYS if self.dates == "time" else
                (MAX_RANGE_DAYS if self.dates == "range" else None)}


_DEFINITIONS = [
    ReportDefinition(
        "timesheet", "Timesheet", "time",
        "Time logged per person per day, with totals — the team's week at a glance.",
        time_reports.timesheet, "time", tuple(_TIME_FILTERS), "stacked_bar"),
    ReportDefinition(
        "time_by_group", "Time by grouping", "time",
        "Time grouped by one or two of person, project, list, epic, issue, type, billable or "
        "week. Open a group to see its entries.",
        time_reports.time_by_group, "time", (*_TIME_FILTERS, "group_by"), "bar"),
    ReportDefinition(
        "time_entries", "Time entries", "time",
        "Every time entry in the range, newest first — the detail behind every time report.",
        time_reports.time_entries, "time", tuple(_TIME_FILTERS), "none"),
    ReportDefinition(
        "estimate_vs_actual", "Estimate vs actual", "time",
        "Each estimated or worked issue: estimate, time spent on it and beneath it, "
        "remaining, and the over-run.",
        time_reports.estimate_vs_actual, "none", (*_ISSUE_FILTERS, "issue_ids"),
        "bullet"),
    ReportDefinition(
        "weekly", "Weekly report", "time",
        "One week on one page: hours per person and project, issues completed and created, "
        "overdue issues and the top time sinks.",
        time_reports.weekly, "week", ("scope", "preset", "date_from", "user_ids",
                                      "assignee_ids", "types", "billable",
                                      "rounding_minutes", "rounding_mode"), "stacked_bar"),
    ReportDefinition(
        "project_overview", "Project overview", "project",
        "Where a project stands: issues by status, type, priority and assignee, progress by "
        "count and points, overdue issues, and time against estimate.",
        project_reports.project_overview, "none", tuple(_ISSUE_FILTERS), "donut"),
    ReportDefinition(
        "created_vs_resolved", "Created vs resolved", "project",
        "Issues created against issues resolved per day, week or month — is the backlog "
        "growing or shrinking?",
        project_reports.created_vs_resolved, "range",
        (*_RANGE_FILTERS, "bucket", "cumulative"), "line"),
    ReportDefinition(
        "workload", "Workload", "project",
        "Open issues, points and remaining estimate per assignee, with the time each logged "
        "in the period.",
        project_reports.workload, "time", _RANGE_FILTERS, "bar"),
    ReportDefinition(
        "epic_progress", "Epic progress", "project",
        "Each epic's issues done by count and by points, and time against estimate.",
        project_reports.epic_progress, "none", ("scope",), "bar"),
    ReportDefinition(
        "sprint_report", "Sprint report", "sprint",
        "What a sprint committed and completed; the issues not completed, removed, and added "
        "after it started; points that changed; time logged.",
        sprint_reports.sprint_report, "sprint", ("scope", "sprint_id", "assignee_ids",
                                                 "types"), "bar", needs_sprints=True),
    ReportDefinition(
        "burndown", "Burndown", "sprint",
        "Work remaining in a sprint at the end of each day, against the guideline. Points, "
        "issue count or hours.",
        sprint_reports.burndown, "sprint", ("scope", "sprint_id", "unit", "assignee_ids",
                                            "types"), "line", needs_sprints=True),
    ReportDefinition(
        "burnup", "Burnup", "sprint",
        "Work completed against total scope in a sprint, day by day.",
        sprint_reports.burnup, "sprint", ("scope", "sprint_id", "unit", "assignee_ids",
                                          "types"), "line", needs_sprints=True),
    ReportDefinition(
        "velocity", "Velocity", "sprint",
        "Committed and completed points of the last closed sprints, and the average.",
        sprint_reports.velocity, "none", ("scope", "sprint_count"), "grouped_bar",
        needs_sprints=True),
    ReportDefinition(
        "cfd", "Cumulative flow", "flow",
        "Issues in each status at the end of every day: widening bands are bottlenecks.",
        flow_reports.cfd, "range", _RANGE_FILTERS, "stacked_area"),
    ReportDefinition(
        "cycle_time", "Cycle and lead time", "flow",
        "How long resolved issues took, from created and from first in progress to done; "
        "median, 85th percentile and rolling average.",
        flow_reports.cycle_time, "range", _RANGE_FILTERS, "scatter"),
]

REPORTS: dict[str, ReportDefinition] = {d.type: d for d in _DEFINITIONS}
