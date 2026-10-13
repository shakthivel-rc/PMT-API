"""The shapes every report shares: the context it computes in, the result it returns, and
the refusal it raises.

**One result shape** (docs/design/08-reports.md §5.3). Every report returns a dict with

* `meta` — title, type, scope, range, time zone, generated-at, rounding, and the notes a
  reader needs (`restricted_to_own`, `data_from`);
* `columns` — `{key, label, type}`, type one of `text | number | duration | date | datetime |
  user | issue | percent | boolean`;
* `rows` — dicts keyed by column key; a row may carry `_depth` (tree tables) and `_drill`
  (the parameters of the report one level down);
* `totals` — a row of the same keys, or None;
* `chart` — `{kind, x, series: [{key, label, values}], …}` or None;
* `kpis` — `[{key, label, value, type}]`;
* `sections` — further titled tables, each `{key, title, columns, rows, totals}`.

The table on screen, the CSV, the XLSX, the PDF and the email all render from it, so an
export cannot disagree with the screen — ClickUp's most-reported reporting complaint.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from zoneinfo import ZoneInfo

from sqlalchemy.orm import Session

from dependencies.workspace import WorkspaceAccess
from models.hierarchy import Space
from models.sprint import Sprint
from schemas.reports import ReportParams, ReportsSettings
from schemas.time_tracking import TimeTrackingSettings


class ReportError(Exception):
    """A request the engine refuses: the controller answers `status` with `message`."""

    def __init__(self, status: int, message: str, data: dict | None = None):
        super().__init__(message)
        self.status = status
        self.message = message
        self.data = data


@dataclass
class Scope:
    """What `ReportParams.scope` resolved to, after the tenancy check."""

    kind: str  # workspace | space | folder | list | sprint
    id: str | None
    name: str
    space_id: str | None = None
    list_ids: list[str] | None = None
    sprint: Sprint | None = None
    space: Space | None = None

    @property
    def token(self) -> str:
        return "workspace" if self.kind == "workspace" else f"{self.kind}:{self.id}"


@dataclass
class ReportContext:
    db: Session
    access: WorkspaceAccess
    params: ReportParams
    settings: ReportsSettings
    time_settings: TimeTrackingSettings
    tz: ZoneInfo
    now: datetime  # naive UTC
    today: date  # in the workspace zone
    scope: Scope
    date_from: date | None = None
    date_to: date | None = None
    #: Under `own` time visibility, the one person whose time this caller may see.
    only_user_id: str | None = None
    #: The most rows a result may hold: the synchronous limit on screen, the export limit in
    #: a job (services/reports/engine.py).
    max_rows: int = 10_000
    notes: list[str] = field(default_factory=list)

    @property
    def week_start(self) -> str:
        return self.time_settings.week_start


def column(key: str, label: str, kind: str = "text", **extra) -> dict:
    return {"key": key, "label": label, "type": kind, **extra}


def section(key: str, title: str, columns: list[dict], rows: list[dict],
            totals: dict | None = None) -> dict:
    return {"key": key, "title": title, "columns": columns, "rows": rows, "totals": totals}


def kpi(key: str, label: str, value, kind: str = "number") -> dict:
    return {"key": key, "label": label, "value": value, "type": kind}


def result(ctx: ReportContext, report_type: str, title: str, *, columns: list[dict],
           rows: list[dict], totals: dict | None = None, chart: dict | None = None,
           kpis: list[dict] | None = None, sections: list[dict] | None = None,
           meta: dict | None = None) -> dict:
    return {
        "meta": {
            "type": report_type,
            "title": title,
            "scope": ctx.scope.token,
            "scope_name": ctx.scope.name,
            "date_from": ctx.date_from,
            "date_to": ctx.date_to,
            "timezone": str(ctx.tz.key),
            "week_start": ctx.week_start,
            "generated_at": ctx.now,
            "rounding_minutes": ctx.params.rounding_minutes,
            "rounding_mode": ctx.params.rounding_mode,
            "restricted_to_own": ctx.only_user_id is not None,
            "notes": list(ctx.notes),
            "row_count": len(rows),
            **(meta or {}),
        },
        "columns": columns,
        "rows": rows,
        "totals": totals,
        "chart": chart,
        "kpis": kpis or [],
        "sections": sections or [],
    }
