"""Run a report: check who may, resolve the scope and dates, compute or read the cache.

The one entry point for the API, the exports and the worker alike, so all of them apply the
same permission, visibility and limit rules (docs/design/08-reports.md §6).
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy.orm import Session

from dependencies.workspace import WorkspaceAccess
from schemas.reports import ReportParams
from services import time_tracking as tt_svc
from services.reports import cache
from services.reports import settings as report_settings
from services.reports.base import ReportContext, ReportError
from services.reports.registry import MAX_RANGE_DAYS, MAX_TIME_DAYS, REPORTS, ReportDefinition
from services.reports.scope import resolve_scope
from services.reports.time_reports import week_range
from utils import report_math as rm
from utils import time_tracking as tt

#: Rows a report shows at once. Above it the answer is 413 with `{"hint": "export"}`.
MAX_SYNC_ROWS = 10_000
#: Rows an export job writes. Bounded so one job cannot hold the worker for an hour or put a
#: gigabyte in a LONGBLOB; 200,000 time entries is years of a fifty-person team.
MAX_EXPORT_ROWS = 200_000


def definition(report_type: str) -> ReportDefinition:
    found = REPORTS.get(report_type)
    if found is None:
        raise ReportError(404, "No such report")
    return found


def can_view(access: WorkspaceAccess, settings) -> bool:
    return access.role != "guest" or settings.guest_access


def build_context(db: Session, access: WorkspaceAccess, report_type: str,
                  params: ReportParams, *, now: datetime | None = None,
                  max_rows: int | None = None) -> tuple[ReportDefinition, ReportContext]:
    spec = definition(report_type)
    settings = report_settings.load_settings(db, access.workspace_id)
    if not can_view(access, settings):
        raise ReportError(403, "Guests cannot run reports in this workspace.")
    time_settings = tt_svc.load_settings(db, access.workspace_id)
    tz = rm.zone(settings.timezone)
    instant = now or tt.utc_now()
    scope = resolve_scope(db, access.workspace_id, params.scope)
    ctx = ReportContext(
        db=db, access=access, params=params, settings=settings, time_settings=time_settings,
        tz=tz, now=instant, today=rm.local_today(instant, tz), scope=scope,
        only_user_id=tt_svc.visible_user_id(access, time_settings),
        max_rows=MAX_SYNC_ROWS if max_rows is None else max_rows)
    _resolve_dates(ctx, spec)
    return spec, ctx


def _resolve_dates(ctx: ReportContext, spec: ReportDefinition) -> None:
    params = ctx.params
    if spec.dates == "none":
        return
    if spec.dates == "sprint":
        return  # the report sets them from its sprint
    if spec.dates == "week":
        week_range(ctx)
        return
    if params.preset and params.preset != "custom":
        ctx.date_from, ctx.date_to = rm.preset_range(params.preset, ctx.today, ctx.week_start)
    elif params.date_from or params.date_to:
        ctx.date_from = params.date_from or params.date_to
        ctx.date_to = params.date_to or params.date_from
    elif ctx.scope.sprint is not None and ctx.scope.sprint.starts_on:
        ctx.date_from = ctx.scope.sprint.starts_on
        ctx.date_to = ctx.scope.sprint.ends_on or ctx.today
    elif spec.dates == "time":
        ctx.date_from, ctx.date_to = rm.preset_range("this_week", ctx.today, ctx.week_start)
    else:
        ctx.date_from, ctx.date_to = rm.preset_range("last_30_days", ctx.today, ctx.week_start)
    if ctx.date_from is None or ctx.date_to is None:
        raise ReportError(400, "Choose a date range.")
    if ctx.date_to < ctx.date_from:
        raise ReportError(400, "The end date is before the start date.")
    limit = MAX_TIME_DAYS if spec.dates == "time" else MAX_RANGE_DAYS
    if (ctx.date_to - ctx.date_from).days + 1 > limit:
        raise ReportError(400, f"This report covers at most {limit} days. Narrow the range.")


def compute(db: Session, access: WorkspaceAccess, report_type: str, params: ReportParams, *,
            now: datetime | None = None, max_rows: int | None = None) -> dict:
    """Compute a report without the cache — the export path, which must see current data."""
    spec, ctx = build_context(db, access, report_type, params, now=now, max_rows=max_rows)
    return spec.compute(ctx)


def run(db: Session, access: WorkspaceAccess, report_type: str, params: ReportParams, *,
        now: datetime | None = None, use_cache: bool = True) -> dict:
    """Compute a report for the screen, served from `report_cache` when it can be."""
    spec, ctx = build_context(db, access, report_type, params, now=now)
    if not use_cache:
        return spec.compute(ctx)
    normalized = params.normalized()
    version = cache.data_version(db, access.workspace_id)
    key = cache.cache_key(access.workspace_id, report_type, normalized,
                          f"{access.user_id}:{access.role}", version, ctx.today.isoformat())
    hit = cache.get(db, key, ctx.now)
    if hit is not None:
        hit["meta"]["cached"] = True
        return hit
    output = spec.compute(ctx)
    sprint_meta = output["meta"].get("sprint") or {}
    ttl = cache.CLOSED_SPRINT_TTL if sprint_meta.get("state") == "closed" else cache.LIVE_TTL
    cache.put(db, key, access.workspace_id, report_type, normalized, output, ctx.now, ttl)
    output["meta"]["cached"] = False
    return output


def catalog(access: WorkspaceAccess, settings) -> dict:
    can_export = access.at_least(settings.export_min_role)
    return {
        "reports": [spec.catalog_entry() for spec in REPORTS.values()],
        "can_view": can_view(access, settings),
        "export_formats": ["csv", "xlsx", "pdf"] if can_export else [],
        "can_schedule": access.at_least(settings.schedule_min_role),
        "can_manage_sprints": access.at_least(settings.sprint_manage_role),
        "presets": list(rm.DATE_PRESETS),
        "limits": {"sync_rows": MAX_SYNC_ROWS, "export_rows": MAX_EXPORT_ROWS,
                   "time_days": MAX_TIME_DAYS, "range_days": MAX_RANGE_DAYS},
        "settings": settings.model_dump(),
    }

