"""Time reports: T1 timesheet, T2 time by grouping (+ the entries behind a group), T3
estimate vs actual, T4 weekly (docs/design/08-reports.md §4).

Time follows the time-tracking `visibility` setting through `ctx.only_user_id`: under `own`
a member's rows are their own, and `meta.team_total_seconds` carries the total they may still
see — the same rule the Timesheet already applies (docs/design/07-time-tracking.md).
"""
from __future__ import annotations

from collections import defaultdict
from datetime import timedelta

from models.issue import Issue
from models.time_tracking import TimeEntry
from services.reports import common as cm
from services.reports import scope as sc
from services.reports.base import ReportContext, ReportError, column, kpi, result, section
from services.reports.history import History
from services.reports.history import naive as _naive
from utils import report_math as rm


def _team_total(ctx: ReportContext) -> dict:
    """Under `own`, the whole team's total — which the caller may see — in `meta`."""
    if ctx.only_user_id is None:
        return {}
    total = sum(cm.grouped_seconds(ctx, ["user"], restrict=False).values())
    return {"team_total_seconds": total}


# ---- T1 --------------------------------------------------------------------------------------


def timesheet(ctx: ReportContext) -> dict:
    """Seconds per person per day, with totals both ways."""
    if ctx.date_from is None or ctx.date_to is None:
        raise ReportError(400, "Choose a date range.")
    days = rm.days_between(ctx.date_from, ctx.date_to)
    grid = cm.grouped_seconds(ctx, ["user", "date"])
    billable = cm.grouped_seconds(ctx, ["billable"])
    users = sorted({user_id for user_id, _day in grid})
    names = cm.user_names(ctx, users)
    users.sort(key=lambda u: names.get(u, "").lower())
    columns = [column("user", "Person", "user")]
    columns += [column(f"d{day.isoformat()}", day.strftime("%a %d %b"), "duration",
                       date=day.isoformat()) for day in days]
    columns.append(column("total", "Total", "duration"))
    rows = []
    for user_id in users:
        row: dict = {"user": names.get(user_id, "Unknown"), "_user_id": user_id}
        total = 0
        for day in days:
            seconds = grid.get((user_id, day), 0)
            row[f"d{day.isoformat()}"] = seconds
            total += seconds
        row["total"] = total
        row["_drill"] = cm.drill_params(ctx, "user", user_id)
        rows.append(row)
    cm.check_rows(ctx, len(rows))
    totals: dict = {"user": "Total"}
    for day in days:
        totals[f"d{day.isoformat()}"] = sum(r[f"d{day.isoformat()}"] for r in rows)
    grand = sum(r["total"] for r in rows)
    totals["total"] = grand
    billable_seconds = sum(v for (flag,), v in billable.items() if flag)
    worked_days = sum(1 for day in days if totals[f"d{day.isoformat()}"])
    chart = {
        "kind": "stacked_bar", "unit": "duration",
        "x": [day.isoformat() for day in days],
        "series": [{"key": r["_user_id"], "label": r["user"],
                    "values": [r[f"d{day.isoformat()}"] for day in days]} for r in rows],
    }
    kpis = [
        kpi("total", "Total time", grand, "duration"),
        kpi("billable", "Billable", billable_seconds, "duration"),
        kpi("non_billable", "Non-billable", grand - billable_seconds, "duration"),
        kpi("people", "People", len(rows)),
        kpi("per_day", "Average per day worked",
            round(grand / worked_days) if worked_days else 0, "duration"),
    ]
    return result(ctx, "timesheet", "Timesheet", columns=columns, rows=rows, totals=totals,
                  chart=chart, kpis=kpis, meta=_team_total(ctx))


# ---- T2 --------------------------------------------------------------------------------------


def time_by_group(ctx: ReportContext) -> dict:
    """Time grouped by one or two dimensions — a tree of group → sub-group, each row with
    the parameters of the entries behind it (`_drill`)."""
    dims = list(ctx.params.group_by) or ["user"]
    sums = cm.grouped_seconds(ctx, dims)
    grand = sum(sums.values())
    labels = {dim: cm.dim_labels(ctx, dim, {key[i] for key in sums})
              for i, dim in enumerate(dims)}
    level1: dict = defaultdict(int)
    for key, seconds in sums.items():
        level1[key[0]] += seconds
    rows = []
    order = sorted(level1, key=lambda v: (-level1[v], str(labels[dims[0]].get(v))))
    for value in order:
        seconds = level1[value]
        rows.append({
            "group": labels[dims[0]].get(value), "level": cm.DIM_LABELS[dims[0]],
            "seconds": seconds, "share": round(100 * seconds / grand, 1) if grand else 0.0,
            "_depth": 0, "_key": _key(value),
            "_drill": cm.drill_params(ctx, dims[0], value),
        })
        if len(dims) == 2:
            subs = [(key[1], s) for key, s in sums.items() if key[0] == value]
            subs.sort(key=lambda item: (-item[1], str(labels[dims[1]].get(item[0]))))
            for sub_value, sub_seconds in subs:
                drill = cm.drill_params(ctx, dims[0], value)
                drill.update({k: v for k, v in cm.drill_params(ctx, dims[1], sub_value).items()
                              if k not in drill or drill[k] != v})
                rows.append({
                    "group": labels[dims[1]].get(sub_value), "level": cm.DIM_LABELS[dims[1]],
                    "seconds": sub_seconds,
                    "share": round(100 * sub_seconds / grand, 1) if grand else 0.0,
                    "_depth": 1, "_key": f"{_key(value)}/{_key(sub_value)}", "_drill": drill,
                })
    cm.check_rows(ctx, len(rows))
    columns = [column("group", " / ".join(cm.DIM_LABELS[d] for d in dims), "text"),
               column("level", "Level", "text"),
               column("seconds", "Time", "duration"),
               column("share", "Share", "percent")]
    top = [r for r in rows if r["_depth"] == 0]
    chart = {"kind": "bar", "orientation": "horizontal", "unit": "duration",
             "x": [r["group"] for r in top],
             "series": [{"key": "seconds", "label": "Time",
                         "values": [r["seconds"] for r in top]}]}
    kpis = [kpi("total", "Total time", grand, "duration"),
            kpi("groups", f"{cm.DIM_LABELS[dims[0]]}s", len(top))]
    return result(ctx, "time_by_group", "Time by grouping", columns=columns, rows=rows,
                  totals={"group": "Total", "seconds": grand, "share": 100.0 if grand else 0.0},
                  chart=chart, kpis=kpis, meta={"group_by": dims, **_team_total(ctx)})


def _key(value) -> str:
    return "none" if value is None else str(value.isoformat() if hasattr(value, "isoformat")
                                            else value)


def time_entries(ctx: ReportContext) -> dict:
    """The entries themselves, newest first — the drill-down of every time report."""
    query = sc.entry_query(ctx, TimeEntry.id, TimeEntry.work_date, TimeEntry.user_id,
                           TimeEntry.issue_id, TimeEntry.duration_seconds, TimeEntry.billable,
                           TimeEntry.note, Issue.space_id)
    count = query.count()
    cm.check_rows(ctx, count)
    rows_raw = query.order_by(TimeEntry.work_date.desc(), TimeEntry.created_at.desc(),
                              TimeEntry.id).all()
    names = cm.user_names(ctx, (r[2] for r in rows_raw))
    refs = cm.issue_refs(ctx, (r[3] for r in rows_raw))
    spaces = cm.space_names(ctx, (r[7] for r in rows_raw))
    rows = []
    total = 0
    billable_total = 0
    for entry_id, work_date, user_id, issue_id, seconds, billable, note, space_id in rows_raw:
        seconds = rm.round_seconds(int(seconds), ctx.params.rounding_minutes,
                                   ctx.params.rounding_mode)
        total += seconds
        billable_total += seconds if billable else 0
        ref = refs.get(issue_id, {})
        rows.append({
            "date": cm._as_date(work_date), "user": names.get(user_id, "Unknown"),
            "issue": ref.get("key", ""), "title": ref.get("title", ""),
            "project": spaces.get(space_id, ""), "seconds": seconds,
            "billable": bool(billable), "note": note or "",
            "_id": entry_id, "_user_id": user_id, "_issue_id": issue_id,
        })
    columns = [column("date", "Date", "date"), column("user", "Person", "user"),
               column("issue", "Issue", "issue"), column("title", "Title", "text"),
               column("project", "Project", "text"), column("seconds", "Time", "duration"),
               column("billable", "Billable", "boolean"), column("note", "Note", "text")]
    kpis = [kpi("total", "Total time", total, "duration"),
            kpi("billable", "Billable", billable_total, "duration"),
            kpi("entries", "Entries", len(rows))]
    return result(ctx, "time_entries", "Time entries", columns=columns, rows=rows,
                  totals={"date": "Total", "seconds": total}, kpis=kpis,
                  meta=_team_total(ctx))


# ---- T3 --------------------------------------------------------------------------------------


def estimate_vs_actual(ctx: ReportContext) -> dict:
    """Each estimated or worked issue: estimate, time spent on it and beneath it, remaining,
    and the over-run. All time ever logged, not the date range: an estimate is for the
    whole issue."""
    issues = sc.issue_query(ctx).all()
    parents = cm.parent_map(ctx)
    children = cm.children_of(parents)
    tree = {issue.id: cm.subtree(children, issue.id) for issue in issues}
    spent = cm.spent_by_issue(ctx, {i for ids in tree.values() for i in ids})
    statuses = cm.status_names(ctx)
    names = cm.user_names(ctx, (issue.assignee_id for issue in issues))
    refs = cm.issue_refs(ctx, (issue.id for issue in issues))
    rows = []
    for issue in issues:
        own = spent.get(issue.id, 0)
        rolled = sum(spent.get(i, 0) for i in tree[issue.id])
        estimate = issue.time_estimate_seconds
        if not estimate and not rolled:
            continue
        variance = rolled - estimate if estimate is not None else None
        rows.append({
            "issue": refs[issue.id]["key"], "title": issue.title, "type": issue.type,
            "assignee": names.get(issue.assignee_id, "") if issue.assignee_id else "",
            "status": statuses.get(issue.status_id, ("", ""))[0],
            "estimate": estimate, "spent": own, "rollup": rolled,
            "remaining": max(estimate - rolled, 0) if estimate is not None else None,
            "variance": variance,
            "accuracy": round(100 * rolled / estimate, 1) if estimate else None,
            "over": bool(estimate is not None and rolled > estimate),
            "_issue_id": issue.id,
        })
    rows.sort(key=lambda r: (-(r["variance"] if r["variance"] is not None else -10**12),
                             r["issue"]))
    cm.check_rows(ctx, len(rows))
    estimated = [r for r in rows if r["estimate"] is not None]
    total_estimate = sum(r["estimate"] for r in estimated)
    total_spent_on_estimated = sum(r["rollup"] for r in estimated)
    columns = [column("issue", "Issue", "issue"), column("title", "Title"),
               column("type", "Type"), column("assignee", "Assignee", "user"),
               column("status", "Status"), column("estimate", "Estimate", "duration"),
               column("spent", "Spent (own)", "duration"),
               column("rollup", "Spent (with sub-issues)", "duration"),
               column("remaining", "Remaining", "duration"),
               column("variance", "Over / under", "duration"),
               column("accuracy", "Spent of estimate", "percent"),
               column("over", "Over-run", "boolean")]
    top = rows[:20]
    chart = {"kind": "bullet", "unit": "duration", "x": [r["issue"] for r in top],
             "series": [{"key": "estimate", "label": "Estimate",
                         "values": [r["estimate"] or 0 for r in top]},
                        {"key": "rollup", "label": "Spent",
                         "values": [r["rollup"] for r in top]}]}
    kpis = [kpi("estimate", "Estimated", total_estimate, "duration"),
            kpi("spent", "Spent on estimated issues", total_spent_on_estimated, "duration"),
            kpi("over", "Over-run issues", sum(1 for r in rows if r["over"])),
            kpi("unestimated", "Worked but not estimated",
                sum(1 for r in rows if r["estimate"] is None)),
            kpi("accuracy", "Spent of estimate",
                round(100 * total_spent_on_estimated / total_estimate, 1)
                if total_estimate else None, "percent")]
    totals = {"issue": "Total", "estimate": total_estimate,
              "spent": sum(r["spent"] for r in rows), "rollup": sum(r["rollup"] for r in rows)}
    return result(ctx, "estimate_vs_actual", "Estimate vs actual", columns=columns, rows=rows,
                  totals=totals, chart=chart, kpis=kpis)


# ---- T4 --------------------------------------------------------------------------------------


def weekly(ctx: ReportContext) -> dict:
    """One week on one page: hours per person and project, what was completed and created,
    what is overdue, and where the time went.

    "Completed" is delivered work, reaching `done`; "Overdue" is open work, so neither a done
    nor a canceled issue is ever late (docs/design/09-restructuring.md §9)."""
    if ctx.date_from is None or ctx.date_to is None:
        raise ReportError(400, "Choose a date range.")
    start, end = cm.range_bounds(ctx)
    days = rm.days_between(ctx.date_from, ctx.date_to)

    per_user_day = cm.grouped_seconds(ctx, ["user", "date"])
    per_project = cm.grouped_seconds(ctx, ["project"])
    per_issue = cm.grouped_seconds(ctx, ["issue"])
    users = sorted({u for u, _d in per_user_day})
    names = cm.user_names(ctx, users)
    people_rows = []
    grand = sum(per_user_day.values())
    for user_id in sorted(users, key=lambda u: names.get(u, "").lower()):
        seconds = sum(v for (u, _d), v in per_user_day.items() if u == user_id)
        people_rows.append({"user": names.get(user_id, "Unknown"), "seconds": seconds,
                            "share": round(100 * seconds / grand, 1) if grand else 0.0,
                            "_user_id": user_id})
    project_names = cm.space_names(ctx, (k[0] for k in per_project))
    project_rows = sorted(({"project": project_names.get(k[0], "Unknown"), "seconds": v}
                           for k, v in per_project.items()), key=lambda r: -r["seconds"])

    issues = sc.issue_query(ctx, status_categories=False).all()
    stage_names = cm.status_names(ctx)
    done = cm.done_ids(ctx, stage_names)
    closed = cm.closed_ids(ctx, stage_names)
    history = History(ctx.db, ctx.access.workspace_id, issues)
    refs = cm.issue_refs(ctx, (i.id for i in issues))
    assignees = cm.user_names(ctx, (i.assignee_id for i in issues))
    completed = []
    created = []
    for issue in issues:
        resolved = history.resolved_at(issue.id, done)
        if resolved is not None and start <= resolved < end:
            completed.append({"issue": refs[issue.id]["key"], "title": issue.title,
                              "assignee": assignees.get(issue.assignee_id, ""),
                              "resolved": rm.local_date(resolved, ctx.tz),
                              "points": issue.story_points, "_issue_id": issue.id})
        created_at = history.issues[issue.id].created_at
        if created_at is not None and start <= _naive(created_at) < end:
            created.append({"issue": refs[issue.id]["key"], "title": issue.title,
                            "type": issue.type, "created": rm.local_date(_naive(created_at),
                                                                          ctx.tz),
                            "_issue_id": issue.id})
    moves = sum(1 for issue in issues for at, _f, _t in history.status_changes(issue.id)
                if start <= at < end)
    overdue = [{"issue": refs[i.id]["key"], "title": i.title,
                "assignee": assignees.get(i.assignee_id, ""), "due": i.due_date,
                "days_overdue": (ctx.today - i.due_date).days, "_issue_id": i.id}
               for i in issues
               if i.due_date is not None and i.due_date < ctx.today
               and i.status_id not in closed]
    overdue.sort(key=lambda r: -r["days_overdue"])
    issue_refs = cm.issue_refs(ctx, (k[0] for k in per_issue))
    sinks = sorted(per_issue.items(), key=lambda kv: -kv[1])[:10]
    sink_rows = [{"issue": issue_refs.get(k[0], {}).get("key", ""),
                  "title": issue_refs.get(k[0], {}).get("title", ""), "seconds": v,
                  "_issue_id": k[0]} for k, v in sinks]
    completed.sort(key=lambda r: (r["resolved"], r["issue"]))
    created.sort(key=lambda r: (r["created"], r["issue"]))

    person_cols = [column("user", "Person", "user"), column("seconds", "Time", "duration"),
                   column("share", "Share", "percent")]
    sections = [
        section("projects", "Time by project",
                [column("project", "Project"), column("seconds", "Time", "duration")],
                project_rows, {"project": "Total", "seconds": grand}),
        section("completed", "Completed this week",
                [column("issue", "Issue", "issue"), column("title", "Title"),
                 column("assignee", "Assignee", "user"), column("resolved", "Done on", "date"),
                 column("points", "Points", "number")], completed),
        section("created", "Created this week",
                [column("issue", "Issue", "issue"), column("title", "Title"),
                 column("type", "Type"), column("created", "Created on", "date")], created),
        section("overdue", "Overdue now",
                [column("issue", "Issue", "issue"), column("title", "Title"),
                 column("assignee", "Assignee", "user"), column("due", "Due", "date"),
                 column("days_overdue", "Days overdue", "number")], overdue),
        section("time_sinks", "Top time sinks",
                [column("issue", "Issue", "issue"), column("title", "Title"),
                 column("seconds", "Time", "duration")], sink_rows),
    ]
    chart = {"kind": "stacked_bar", "unit": "duration", "x": [d.isoformat() for d in days],
             "series": [{"key": r["_user_id"], "label": r["user"],
                         "values": [per_user_day.get((r["_user_id"], d), 0) for d in days]}
                        for r in people_rows]}
    kpis = [kpi("hours", "Time logged", grand, "duration"),
            kpi("completed", "Issues completed", len(completed)),
            kpi("created", "Issues created", len(created)),
            kpi("moved", "Status changes", moves),
            kpi("overdue", "Overdue", len(overdue))]
    return result(ctx, "weekly", f"Weekly report — week of {ctx.date_from.isoformat()}",
                  columns=person_cols, rows=people_rows,
                  totals={"user": "Total", "seconds": grand, "share": 100.0 if grand else 0.0},
                  chart=chart, kpis=kpis, sections=sections, meta=_team_total(ctx))


def week_range(ctx: ReportContext) -> None:
    """The weekly report covers exactly one week: the preset's, or the week of `date_from`,
    or this week."""
    if ctx.params.preset and ctx.params.preset != "custom":
        start, _end = rm.preset_range(ctx.params.preset, ctx.today, ctx.week_start)
    elif ctx.params.date_from:
        start = ctx.params.date_from
    else:
        start = ctx.today
    week = rm.week_start_of(start, ctx.week_start)
    ctx.date_from, ctx.date_to = week, week + timedelta(days=6)
