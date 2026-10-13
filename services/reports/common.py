"""Lookups and aggregations more than one report family needs.

**Reports reason about categories, never about stage names or positions**
(docs/design/09-restructuring.md §4.2). Since 2026-10-05 there are four: `todo` and
`in_progress` are open; `done` and `canceled` are closed; only `done` counts as delivered.
Every report asks the three questions below and nothing else:

* *Was it delivered?* — `done_ids`: velocity, completed points, burndown "completed", cycle
  and lead time, "resolved".
* *Was it given up?* — `canceled_ids`: closed, never completed, and out of every percentage
  of progress, because a canceled story is not a part of an epic still waiting to be done.
* *Is it still open?* — not in `closed_ids`: workload, overdue, anything counted as work
  still to do.

Before the fourth category the only way to close work was a `done` stage, so "not done" and
"open" were the same set and most reports tested the first meaning the second. Each place
that did is now explicit about which one it means.
"""
from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable
from datetime import date, datetime, timedelta

from sqlalchemy import func

from models.hierarchy import ProjectList, Space, WorkflowStatus
from models.issue import Issue
from models.time_tracking import TimeEntry
from services import issue as issue_svc
from services.reports import scope as sc
from services.reports.base import ReportContext, ReportError
from utils import report_math as rm
from utils import workflow_rules as rules
from utils.issue_keys import format_issue_key

#: The category of work closed without being delivered. `utils/workflow_rules` names the
#: closed set and the completed category; this is the difference between the two, spelled
#: once for the reports.
CANCELED = "canceled"

#: How every report labels a category. The reports' own words ("To do", "Done") rather than
#: the stage editor's ("Not started", "Completed", utils/workflow_rules.CATEGORY_LABELS):
#: they are what exported files and saved reports have always said, and a schedule that
#: emailed "Done" last week should not email "Completed" this week for the same thing.
CATEGORY_LABELS = {"todo": "To do", "in_progress": "In progress", "done": "Done",
                   CANCELED: "Canceled"}

#: SQL expressions for the time dimensions that are columns. `epic` and `week` are derived
#: from `issue` and `date` after the query (`grouped_seconds`).
_SQL_DIMS = {
    "user": TimeEntry.user_id,
    "issue": TimeEntry.issue_id,
    "date": TimeEntry.work_date,
    "project": Issue.space_id,
    "list": Issue.list_id,
    "type": Issue.type,
    "billable": TimeEntry.billable,
}

DIM_LABELS = {"user": "Person", "issue": "Issue", "date": "Date", "project": "Project",
              "list": "List", "type": "Type", "billable": "Billable", "epic": "Epic",
              "week": "Week"}


def range_bounds(ctx: ReportContext) -> tuple[datetime, datetime]:
    """The report's dates as a half-open naive-UTC interval [start, end)."""
    if ctx.date_from is None or ctx.date_to is None:
        raise ReportError(400, "Choose a date range.")
    return rm.start_of_day_utc(ctx.date_from, ctx.tz), rm.end_of_day_utc(ctx.date_to, ctx.tz)


def check_rows(ctx: ReportContext, count: int) -> None:
    """Refuse a result too large to send at once; the client offers an export instead."""
    if count > ctx.max_rows:
        raise ReportError(413, f"This report has {count:,} rows, more than {ctx.max_rows:,} "
                               "can be shown at once. Narrow the filters or export it.",
                          {"hint": "export", "row_count": count, "limit": ctx.max_rows})


def user_names(ctx: ReportContext, ids: Iterable[str | None]) -> dict[str, str]:
    users = issue_svc.users_by_id(ctx.db, ids)
    return {user_id: issue_svc.display_name(user) for user_id, user in users.items()}


def space_names(ctx: ReportContext, ids: Iterable[str | None] | None = None) -> dict[str, str]:
    query = ctx.db.query(Space.id, Space.name).filter(
        Space.workspace_id == ctx.access.workspace_id)
    if ids is not None:
        query = query.filter(Space.id.in_({i for i in ids if i} or {"-"}))
    return dict(query.all())


def list_names(ctx: ReportContext, ids: Iterable[str | None]) -> dict[str, str]:
    wanted = {i for i in ids if i}
    if not wanted:
        return {}
    return dict(ctx.db.query(ProjectList.id, ProjectList.name).filter(
        ProjectList.workspace_id == ctx.access.workspace_id,
        ProjectList.id.in_(wanted)).all())


def status_names(ctx: ReportContext) -> dict[str, tuple[str, str]]:
    rows = ctx.db.query(WorkflowStatus.id, WorkflowStatus.name, WorkflowStatus.category).filter(
        WorkflowStatus.workspace_id == ctx.access.workspace_id).all()
    return {status_id: (name, category) for status_id, name, category in rows}


def issue_refs(ctx: ReportContext, ids: Iterable[str]) -> dict[str, dict]:
    """`{id: {key, title, type, space_id, list_id, parent_id}}` for live or deleted issues
    in this workspace; one query per chunk."""
    wanted = list({i for i in ids if i})
    found: dict[str, dict] = {}
    if not wanted:
        return found
    keys = dict(ctx.db.query(Space.id, Space.key).filter(
        Space.workspace_id == ctx.access.workspace_id).all())
    for start in range(0, len(wanted), 900):
        rows = ctx.db.query(Issue.id, Issue.space_id, Issue.number, Issue.title, Issue.type,
                            Issue.list_id, Issue.parent_id).filter(
            Issue.workspace_id == ctx.access.workspace_id,
            Issue.id.in_(wanted[start:start + 900])).all()
        for issue_id, space_id, number, title, kind, list_id, parent_id in rows:
            found[issue_id] = {"id": issue_id, "key": format_issue_key(keys.get(space_id, "?"),
                                                                       number),
                               "title": title, "type": kind, "space_id": space_id,
                               "list_id": list_id, "parent_id": parent_id}
    return found


def parent_map(ctx: ReportContext) -> dict[str, tuple[str | None, str]]:
    """Every live issue in the workspace: `id -> (parent_id, type)`. One query; the hierarchy
    walks below run in memory instead of one query per level per issue."""
    rows = ctx.db.query(Issue.id, Issue.parent_id, Issue.type).filter(
        Issue.workspace_id == ctx.access.workspace_id, Issue.deleted == 0).all()
    return {issue_id: (parent_id, kind) for issue_id, parent_id, kind in rows}


def epic_of(parents: dict[str, tuple[str | None, str]], issue_id: str) -> str | None:
    """The nearest epic at or above `issue_id`, or None."""
    current: str | None = issue_id
    for _ in range(16):
        if current is None or current not in parents:
            return None
        parent_id, kind = parents[current]
        if kind == "epic":
            return current
        current = parent_id
    return None


def children_of(parents: dict[str, tuple[str | None, str]]) -> dict[str, list[str]]:
    children: dict[str, list[str]] = defaultdict(list)
    for issue_id, (parent_id, _kind) in parents.items():
        if parent_id:
            children[parent_id].append(issue_id)
    return children


def subtree(children: dict[str, list[str]], root: str) -> list[str]:
    """`root` and everything beneath it, depth-bounded."""
    found = [root]
    frontier = [root]
    for _ in range(16):
        nxt = [c for parent in frontier for c in children.get(parent, []) if c not in found]
        if not nxt:
            break
        found.extend(nxt)
        frontier = nxt
    return found


def grouped_seconds(ctx: ReportContext, dims: list[str], *,
                    restrict: bool = True) -> dict[tuple, int]:
    """Seconds of time in the report's range and filters, summed by `dims`.

    Without rounding this is one `GROUP BY` in SQL. With rounding, each entry is rounded
    before it is summed (docs/design/08-reports.md §5.5), so the entries are read one by one
    — still one query, of the columns the dimensions need and the duration.
    """
    sql_dims: list[str] = []
    for dim in dims:
        base = {"epic": "issue", "week": "date"}.get(dim, dim)
        if base not in sql_dims:
            sql_dims.append(base)
    exprs = [_SQL_DIMS[d] for d in sql_dims]
    rounding = ctx.params.rounding_minutes
    raw: dict[tuple, int] = defaultdict(int)
    if rounding:
        query = sc.entry_query(ctx, *exprs, TimeEntry.duration_seconds, restrict=restrict)
        for row in query.all():
            *keys, seconds = row
            raw[tuple(keys)] += rm.round_seconds(int(seconds), rounding,
                                                 ctx.params.rounding_mode)
    else:
        query = sc.entry_query(ctx, *exprs, func.sum(TimeEntry.duration_seconds),
                               restrict=restrict).group_by(*exprs)
        for row in query.all():
            *keys, seconds = row
            raw[tuple(keys)] += int(seconds or 0)
    if sql_dims == dims:
        return {tuple(_norm(k) for k in key): value for key, value in raw.items()}
    parents = parent_map(ctx) if "epic" in dims else {}
    out: dict[tuple, int] = defaultdict(int)
    for key, value in raw.items():
        values = dict(zip(sql_dims, key, strict=True))
        mapped = []
        for dim in dims:
            if dim == "epic":
                mapped.append(epic_of(parents, values["issue"]))
            elif dim == "week":
                mapped.append(rm.week_start_of(_as_date(values["date"]), ctx.week_start))
            else:
                mapped.append(_norm(values[dim]))
        out[tuple(mapped)] += value
    return dict(out)


def _as_date(value) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, str):
        return date.fromisoformat(value[:10])
    return value


def _norm(value):
    # SQLite hands back a Boolean as 0/1 and a Date as a date; MySQL a Boolean as 0/1 too.
    if isinstance(value, datetime):
        return value.date()
    return value


def dim_labels(ctx: ReportContext, dim: str, values: Iterable) -> dict:
    """Display text for each value of a dimension."""
    vals = list(values)
    if dim == "user":
        names = user_names(ctx, vals)
        return {v: names.get(v, "Unknown") for v in vals}
    if dim == "project":
        names = space_names(ctx, vals)
        return {v: names.get(v, "Unknown project") for v in vals}
    if dim == "list":
        names = list_names(ctx, vals)
        return {v: names.get(v, "Unknown list") for v in vals}
    if dim in ("issue", "epic"):
        refs = issue_refs(ctx, [v for v in vals if v])
        return {v: (f"{refs[v]['key']} {refs[v]['title']}" if v in refs
                    else ("No epic" if dim == "epic" else "Unknown issue")) for v in vals}
    if dim == "billable":
        return {v: ("Billable" if v else "Non-billable") for v in vals}
    if dim in ("date", "week"):
        return {v: (f"Week of {v.isoformat()}" if dim == "week" else v.isoformat())
                for v in vals}
    if dim == "type":
        return {v: str(v).capitalize() for v in vals}
    return {v: str(v) for v in vals}


def drill_params(ctx: ReportContext, dim: str, value) -> dict:
    """The `time_entries` parameters that show the entries behind one group."""
    base = ctx.params.normalized()
    base.pop("group_by", None)
    if ctx.date_from:
        base["date_from"] = ctx.date_from.isoformat()
        base["date_to"] = ctx.date_to.isoformat() if ctx.date_to else None
        base.pop("preset", None)
    if dim == "user":
        base["user_ids"] = [value]
    elif dim == "issue":
        base["issue_ids"] = [value]
    elif dim == "project":
        base["scope"] = f"space:{value}"
    elif dim == "list":
        base["scope"] = f"list:{value}"
    elif dim == "type":
        base["types"] = [value]
    elif dim == "billable":
        base["billable"] = "billable" if value else "non_billable"
    elif dim == "epic" and value:
        base["epic_id"] = value
    elif dim == "week":
        base["date_from"] = value.isoformat()
        base["date_to"] = min(value + timedelta(days=6), ctx.date_to or value).isoformat()
    return base


def spent_by_issue(ctx: ReportContext, issue_ids: Iterable[str],
                   through: date | None = None) -> dict[str, int]:
    """All-time seconds logged on each issue (to `through`, inclusive), whoever logged them —
    an issue's totals are visible to every member whatever the visibility setting."""
    ids = list(set(issue_ids))
    out: dict[str, int] = {}
    for start in range(0, len(ids), 900):
        query = ctx.db.query(TimeEntry.issue_id, func.sum(TimeEntry.duration_seconds)).filter(
            TimeEntry.workspace_id == ctx.access.workspace_id, TimeEntry.deleted == 0,
            TimeEntry.issue_id.in_(ids[start:start + 900]))
        if through is not None:
            query = query.filter(TimeEntry.work_date <= through)
        for issue_id, seconds in query.group_by(TimeEntry.issue_id).all():
            out[issue_id] = int(seconds or 0)
    return out


def ids_in(names: dict[str, tuple[str, str]], categories: Iterable[str]) -> set[str]:
    """The stage ids of `names` (`status_names`) whose category is one of `categories`.
    Removed stages included: history names them, and a removed stage keeps its category."""
    wanted = set(categories)
    return {sid for sid, (_name, category) in names.items() if category in wanted}


def done_ids(ctx: ReportContext, names: dict[str, tuple[str, str]] | None = None) -> set[str]:
    """Stages whose work counts as delivered. Pass `names` when the report already read
    `status_names`, so the three sets below cost one query between them, not three."""
    return ids_in(status_names(ctx) if names is None else names, {rules.COMPLETED_CATEGORY})


def canceled_ids(ctx: ReportContext,
                 names: dict[str, tuple[str, str]] | None = None) -> set[str]:
    """Stages whose work was closed without being delivered."""
    return ids_in(status_names(ctx) if names is None else names, {CANCELED})


def closed_ids(ctx: ReportContext, names: dict[str, tuple[str, str]] | None = None) -> set[str]:
    """Stages whose work is finished either way. Everything else is still open."""
    return ids_in(status_names(ctx) if names is None else names, rules.CLOSED_CATEGORIES)
