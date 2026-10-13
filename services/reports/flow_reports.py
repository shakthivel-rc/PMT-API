"""Flow reports: F1 cumulative flow, F2 cycle and lead time (docs/design/08-reports.md §4).

Both read the four categories of docs/design/09-restructuring.md §4.2: the CFD draws a
Canceled band, and cycle and lead time measure delivered work only — reaching `done`.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass

from sqlalchemy import func, select

from models.hierarchy import Space, WorkflowStatus
from models.issue import Issue
from models.reporting import IssueDailyStatus
from services.reports import common as cm
from services.reports import scope as sc
from services.reports import snapshot
from services.reports.base import ReportContext, ReportError, column, kpi, result
from services.reports.history import History, naive
from utils import report_math as rm

#: The bands from the bottom of the chart up. **Done stays the bottom band**, as in Jira, so
#: the top edge of the lowest band is still the delivered-throughput line a team reads its
#: rate of finishing from. **Canceled sits directly on top of it** (added 2026-10-05,
#: 09-restructuring.md §9): the two closed bands together are everything that has left the
#: flow, so the bands above them are still exactly the work in flight — the widening a CFD
#: exists to show. Canceled at the very bottom would put dropped work under the throughput
#: line and make the team look faster than it is; Canceled on top would make it read as the
#: newest, least-started work.
_CATEGORY_ORDER = ("done", cm.CANCELED, "in_progress", "todo")


@dataclass(frozen=True)
class _Band:
    """A stage drawn as a band of a one-project CFD: one of the project's own, live or
    removed, or a stage of another project an issue was in before it moved here."""

    id: str
    name: str
    category: str
    position: int
    removed: bool
    space_key: str | None = None

    @property
    def label(self) -> str:
        # A removed stage keeps its name for history; the suffix is what tells a reader why
        # a column they cannot find on the board is in the chart. A stage of another project
        # names that project, for the same reason (09-restructuring.md §8.7: an issue moved
        # to another space takes its history with it).
        label = self.name if self.space_key is None else f"{self.name} ({self.space_key})"
        return f"{label} (removed)" if self.removed else label

    def rank(self) -> tuple:
        category = (_CATEGORY_ORDER.index(self.category) if self.category in _CATEGORY_ORDER
                    else len(_CATEGORY_ORDER))
        # Within a category the later stage is lower, so the bands stack in flow order.
        return (category, self.space_key is not None, -self.position, self.name)


def _stage_bands(ctx: ReportContext, held: set[str]) -> list[_Band]:
    """The bands of a one-project CFD, bottom first.

    Every live stage of the project, whether it held anything or not — an empty band is a
    stage nothing reached, which is itself worth seeing. A removed stage only when it held
    issues on some day of the range: before 2026-10-05 removing a stage was impossible, so
    every stage it lists was once a column, but a stage removed last year has nothing to say
    about this month and would only be a zero-height band with a confusing name. Any other
    stage an issue was in on a day of the range belongs to another project it has since
    moved from; it is drawn too, because without it the issues behind that day's total
    would be in no band at all. One query for those, and only when there are some.
    """
    own = sc.statuses(ctx)
    bands = [_Band(s.id, str(s.name), str(s.category), int(s.position or 0),
                   bool(s.deleted))
             for s in own if not s.deleted or s.id in held]
    foreign = held - {s.id for s in own}
    if foreign:
        rows = (ctx.db.query(WorkflowStatus.id, WorkflowStatus.name, WorkflowStatus.category,
                             WorkflowStatus.position, WorkflowStatus.deleted, Space.key)
                .join(Space, Space.id == WorkflowStatus.space_id)
                .filter(WorkflowStatus.workspace_id == ctx.access.workspace_id,
                        WorkflowStatus.id.in_(foreign))
                .all())
        bands += [_Band(sid, str(name), str(category), int(position or 0), bool(deleted),
                        str(key))
                  for sid, name, category, position, deleted, key in rows]
    return sorted(bands, key=_Band.rank)


# ---- F1 --------------------------------------------------------------------------------------


def cfd(ctx: ReportContext) -> dict:
    """Issues per status at the end of each day. One project: a band per status. Wider
    scopes have different workflows, so the bands are the four categories instead.
    Done is the bottom band, as in Jira, with Canceled on it, so the bands above them are
    the work in flight (`_CATEGORY_ORDER` says why that order)."""
    if ctx.date_from is None or ctx.date_to is None:
        raise ReportError(400, "Choose a date range.")
    last = min(ctx.date_to, ctx.today)
    days = rm.days_between(ctx.date_from, last) if last >= ctx.date_from else []
    per_status = ctx.scope.space_id is not None
    categories = cm.status_names(ctx)

    def band(status_id: str | None) -> str | None:
        if status_id is None:
            return None
        return status_id if per_status else categories.get(status_id, ("", "todo"))[1]

    counts: dict = defaultdict(Counter)
    ids_query = sc.issue_query(ctx, columns=[Issue.id], status_categories=False)
    marked = snapshot.marked_days(ctx.db, ctx.access.workspace_id, ctx.date_from, last)
    marked.discard(ctx.today)
    if marked:
        rows = ctx.db.query(IssueDailyStatus.snapshot_date, IssueDailyStatus.status_id,
                            func.count(IssueDailyStatus.issue_id)).filter(
            IssueDailyStatus.workspace_id == ctx.access.workspace_id,
            IssueDailyStatus.snapshot_date.in_(sorted(marked)),
            IssueDailyStatus.issue_id.in_(select(ids_query.subquery().c.id))).group_by(
            IssueDailyStatus.snapshot_date, IssueDailyStatus.status_id).all()
        for day, status_id, count in rows:
            key = band(status_id)
            if key is not None:
                counts[cm._as_date(day)][key] += int(count)
    replay_days = [d for d in days if d not in marked]
    if replay_days:
        issues = sc.issue_query(ctx, status_categories=False).all()
        history = History(ctx.db, ctx.access.workspace_id, issues)
        for day in replay_days:
            at = ctx.now if day == ctx.today else rm.end_of_day_utc(day, ctx.tz)
            for issue in issues:
                key = band(history.status_at(issue.id, at))
                if key is not None:
                    counts[day][key] += 1
    if per_status:
        held = {key for day_counts in counts.values()
                for key, count in day_counts.items() if count}
        bands = [(stage.id, stage.label) for stage in _stage_bands(ctx, held)]
    else:
        bands = [(c, cm.CATEGORY_LABELS[c]) for c in _CATEGORY_ORDER]
    rows_out = []
    for day in days:
        row: dict = {"date": day}
        for key, _label in bands:
            row[f"s{key}"] = counts[day].get(key, 0)
        row["total"] = sum(counts[day].values())
        rows_out.append(row)
    cm.check_rows(ctx, len(rows_out))
    columns = [column("date", "Date", "date")]
    columns += [column(f"s{key}", label, "number") for key, label in bands]
    columns.append(column("total", "Total", "number"))
    chart = {"kind": "stacked_area", "x": [d.isoformat() for d in days],
             "series": [{"key": key, "label": label,
                         "values": [r[f"s{key}"] for r in rows_out]} for key, label in bands]}
    latest = rows_out[-1] if rows_out else {}
    kpis = [kpi("total", "Issues today", latest.get("total", 0))]
    meta = {"bands": "status" if per_status else "category",
            "snapshot_days": len(marked), "replayed_days": len(replay_days)}
    return result(ctx, "cfd", "Cumulative flow", columns=columns, rows=rows_out, chart=chart,
                  kpis=kpis, meta=meta)


# ---- F2 --------------------------------------------------------------------------------------


def cycle_time(ctx: ReportContext) -> dict:
    """For issues resolved in the range: lead time (created → first done) and cycle time
    (first in progress → final done), in days; median, 85th percentile, rolling average.
    Issues that never passed through an in-progress status count in lead time only.

    Resolved means reached `done` (09-restructuring.md §9). Canceled work is left out
    entirely: how long it took to give something up says nothing about how long delivering
    takes, and counting it would make a team that cancels quickly look fast."""
    start, end = cm.range_bounds(ctx)
    issues = sc.issue_query(ctx, status_categories=False).all()
    names = cm.status_names(ctx)
    done = cm.done_ids(ctx, names)
    in_progress = cm.ids_in(names, {"in_progress"})
    history = History(ctx.db, ctx.access.workspace_id, issues)
    refs = cm.issue_refs(ctx, (i.id for i in issues))
    rows = []
    skipped = 0
    for issue in issues:
        resolved = history.resolved_at(issue.id, done)
        if resolved is None or not (start <= resolved < end):
            continue
        created = naive(issue.created_at)
        first_done = history.first_done_at(issue.id, done) or resolved
        lead = (first_done - created).total_seconds() / 86400 if created else None
        began = history.first_entered(issue.id, in_progress)
        cycle = (resolved - began).total_seconds() / 86400 if began else None
        if cycle is None:
            skipped += 1
        rows.append({"issue": refs[issue.id]["key"], "title": issue.title, "type": issue.type,
                     "resolved": rm.local_date(resolved, ctx.tz),
                     "lead_days": round(max(lead, 0), 2) if lead is not None else None,
                     "cycle_days": round(max(cycle, 0), 2) if cycle is not None else None,
                     "_resolved_at": resolved, "_issue_id": issue.id})
    rows.sort(key=lambda r: r["_resolved_at"])
    for row in rows:
        row["_resolved_at"] = row["_resolved_at"].isoformat()
    cm.check_rows(ctx, len(rows))
    cycles = [r["cycle_days"] for r in rows if r["cycle_days"] is not None]
    leads = [r["lead_days"] for r in rows if r["lead_days"] is not None]
    rolling = rm.rolling_average(cycles)

    def stat(values: list[float], fraction: float) -> float | None:
        value = rm.percentile(values, fraction)
        return round(value, 2) if value is not None else None

    columns = [column("issue", "Issue", "issue"), column("title", "Title"),
               column("type", "Type"), column("resolved", "Resolved", "date"),
               column("lead_days", "Lead time (days)", "number"),
               column("cycle_days", "Cycle time (days)", "number")]
    with_cycle = [r for r in rows if r["cycle_days"] is not None]
    chart = {"kind": "scatter", "unit": "days",
             "points": [{"x": r["_resolved_at"], "y": r["cycle_days"], "label": r["issue"]}
                        for r in with_cycle],
             "lines": [{"key": "rolling", "label": f"Rolling average ({rm.ROLLING_WINDOW})",
                        "points": [{"x": r["_resolved_at"], "y": round(v, 2)}
                                   for r, v in zip(with_cycle, rolling, strict=True)]}],
             "references": [{"label": "Median", "value": stat(cycles, 0.5)},
                            {"label": "85th percentile", "value": stat(cycles, 0.85)}]}
    kpis = [kpi("cycle_median", "Cycle time, median (days)", stat(cycles, 0.5)),
            kpi("cycle_p85", "Cycle time, 85th percentile (days)", stat(cycles, 0.85)),
            kpi("lead_median", "Lead time, median (days)", stat(leads, 0.5)),
            kpi("lead_p85", "Lead time, 85th percentile (days)", stat(leads, 0.85)),
            kpi("resolved", "Issues resolved", len(rows))]
    if skipped:
        ctx.notes.append(f"{skipped} issue(s) never entered an in-progress status: they count "
                         "in lead time but not in cycle time.")
    meta = {}
    if history.earliest_event is not None:
        meta["data_from"] = history.earliest_event
    return result(ctx, "cycle_time", "Cycle and lead time", columns=columns, rows=rows,
                  chart=chart, kpis=kpis, meta=meta)
