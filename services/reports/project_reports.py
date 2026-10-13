"""Project reports: P1 overview, P2 created vs resolved, P3 workload, and epic progress
(docs/design/08-reports.md §4).

Under the four categories (docs/design/09-restructuring.md §9): progress percentages are of
the issues that are not canceled, "open" means not closed, and "resolved" means reached
`done` — services/reports/common.py says why each is the question it is.
"""
from __future__ import annotations

from collections import Counter, defaultdict

from models.issue import Issue
from services.reports import common as cm
from services.reports import scope as sc
from services.reports.base import ReportContext, ReportError, column, kpi, result, section
from services.reports.history import History, naive
from utils import report_math as rm

#: The donut's slices, open work first. Canceled is last, beside Done: both are closed.
_DONUT_ORDER = ("todo", "in_progress", "done", cm.CANCELED)
_PRIORITY_ORDER = ("urgent", "high", "medium", "low", "none")


def _pct(part: int | float, whole: int | float) -> float | None:
    return round(100 * part / whole, 1) if whole else None


# ---- P1 --------------------------------------------------------------------------------------


def project_overview(ctx: ReportContext) -> dict:
    """Where the project stands now: counts by status, type, priority and assignee; how much
    is done by count and by points; what is overdue; time logged against the estimate.

    Canceled issues are counted and shown — their stage's row, a donut slice, a column per
    assignee, a KPI — but the two "Done" percentages are of the issues that are NOT canceled:
    a project that dropped half its backlog is not half-way to done because of it, and one
    whose remaining work was all canceled is not finished at 50% (09-restructuring.md §9).
    The status table's `share` stays a share of every issue, so its rows still add up to
    100%. Overdue is open work only: a canceled issue past its due date is not late, it is
    not happening — and a done one never was.
    """
    issues = sc.issue_query(ctx).all()
    statuses = cm.status_names(ctx)
    done = cm.done_ids(ctx, statuses)
    canceled = cm.canceled_ids(ctx, statuses)
    names = cm.user_names(ctx, (i.assignee_id for i in issues))
    refs = cm.issue_refs(ctx, (i.id for i in issues))

    by_status: dict[str, list] = defaultdict(lambda: [0, 0])
    by_category: Counter = Counter()
    by_type: Counter = Counter()
    by_priority: Counter = Counter()
    # open, done, open points, done points, canceled
    by_assignee: dict[str | None, list[int]] = defaultdict(lambda: [0, 0, 0, 0, 0])
    points_total = points_done = points_canceled = 0
    overdue = []
    for issue in issues:
        _name, category = statuses.get(issue.status_id, ("Unknown", "todo"))
        is_done = issue.status_id in done
        is_canceled = issue.status_id in canceled
        points = issue.story_points or 0
        by_status[issue.status_id][0] += 1
        by_status[issue.status_id][1] += points
        by_category[category] += 1
        by_type[issue.type] += 1
        by_priority[issue.priority] += 1
        entry = by_assignee[issue.assignee_id]
        if is_canceled:
            entry[4] += 1
        else:
            entry[1 if is_done else 0] += 1
            entry[3 if is_done else 2] += points
        points_total += points
        points_done += points if is_done else 0
        points_canceled += points if is_canceled else 0
        if (issue.due_date is not None and issue.due_date < ctx.today
                and not is_done and not is_canceled):
            overdue.append({"issue": refs[issue.id]["key"], "title": issue.title,
                            "assignee": names.get(issue.assignee_id, ""),
                            "due": issue.due_date,
                            "days_overdue": (ctx.today - issue.due_date).days,
                            "_issue_id": issue.id})
    overdue.sort(key=lambda r: -r["days_overdue"])
    spent = sum(cm.spent_by_issue(ctx, (i.id for i in issues)).values())
    estimate = sum(i.time_estimate_seconds or 0 for i in issues)
    total = len(issues)
    done_count = by_category.get("done", 0)
    canceled_count = by_category.get(cm.CANCELED, 0)
    if canceled_count:
        ctx.notes.append(f"{canceled_count} canceled issue(s) are counted but left out of "
                         "the done percentages.")

    order = list(statuses)
    rows = [{"status": statuses.get(sid, ("Unknown", ""))[0],
             "category": cm.CATEGORY_LABELS.get(statuses.get(sid, ("", ""))[1], ""),
             "count": by_status[sid][0], "points": by_status[sid][1],
             "share": _pct(by_status[sid][0], total), "_status_id": sid}
            for sid in sorted(by_status, key=lambda s: order.index(s) if s in order else 999)]
    columns = [column("status", "Status"), column("category", "Category"),
               column("count", "Issues", "number"), column("points", "Points", "number"),
               column("share", "Share", "percent")]
    assignee_rows = sorted(
        ({"assignee": names.get(a, "Unassigned") if a else "Unassigned",
          "open": v[0], "done": v[1], "open_points": v[2], "done_points": v[3],
          "canceled": v[4], "_user_id": a} for a, v in by_assignee.items()),
        key=lambda r: (-(r["open"] + r["done"]), r["assignee"]))
    sections = [
        section("by_type", "By type", [column("type", "Type"),
                                       column("count", "Issues", "number")],
                [{"type": t.capitalize(), "count": c} for t, c in by_type.most_common()]),
        section("by_priority", "By priority", [column("priority", "Priority"),
                                               column("count", "Issues", "number")],
                [{"priority": p.capitalize(), "count": by_priority[p]}
                 for p in _PRIORITY_ORDER if by_priority.get(p)]),
        section("by_assignee", "By assignee",
                [column("assignee", "Assignee", "user"), column("open", "Open", "number"),
                 column("done", "Done", "number"),
                 column("open_points", "Open points", "number"),
                 column("done_points", "Done points", "number"),
                 column("canceled", "Canceled", "number")], assignee_rows),
        section("overdue", "Overdue",
                [column("issue", "Issue", "issue"), column("title", "Title"),
                 column("assignee", "Assignee", "user"), column("due", "Due", "date"),
                 column("days_overdue", "Days overdue", "number")], overdue),
    ]
    chart = {"kind": "donut", "x": [cm.CATEGORY_LABELS[c] for c in _DONUT_ORDER],
             "series": [{"key": "count", "label": "Issues",
                         "values": [by_category.get(c, 0) for c in _DONUT_ORDER]}]}
    kpis = [kpi("issues", "Issues", total),
            kpi("done_pct", "Done (by count)", _pct(done_count, total - canceled_count),
                "percent"),
            kpi("points_pct", "Done (by points)",
                _pct(points_done, points_total - points_canceled), "percent"),
            kpi("canceled", "Canceled", canceled_count),
            kpi("overdue", "Overdue", len(overdue)),
            kpi("spent", "Time logged", spent, "duration"),
            kpi("estimate", "Estimated", estimate, "duration")]
    return result(ctx, "project_overview", "Project overview", columns=columns, rows=rows,
                  totals={"status": "Total", "count": total, "points": points_total,
                          "share": 100.0 if total else None},
                  chart=chart, kpis=kpis, sections=sections)


# ---- P2 --------------------------------------------------------------------------------------


def created_vs_resolved(ctx: ReportContext) -> dict:
    """Per bucket: issues created, issues resolved (their last move into done, if still
    done), and the difference; optionally as running totals.

    Resolved means delivered: reaching a `done` stage (09-restructuring.md §9). A canceled
    issue is closed but was never resolved, so it does not count — the question this chart
    answers is whether the team finishes work as fast as it arrives, and canceling work is
    not finishing it. An issue canceled after being done stops counting, like a reopened one.
    """
    if ctx.date_from is None or ctx.date_to is None:
        raise ReportError(400, "Choose a date range.")
    start, end = cm.range_bounds(ctx)
    bucket = ctx.params.bucket
    issues = sc.issue_query(ctx, status_categories=False).all()
    done = cm.done_ids(ctx)
    history = History(ctx.db, ctx.access.workspace_id, issues)
    keys = rm.bucket_keys(ctx.date_from, ctx.date_to, bucket, ctx.week_start)
    created: Counter = Counter()
    resolved: Counter = Counter()
    for issue in issues:
        created_at = naive(issue.created_at)
        if created_at is not None and start <= created_at < end:
            created[rm.bucket_key(rm.local_date(created_at, ctx.tz), bucket,
                                  ctx.week_start)] += 1
        resolved_at = history.resolved_at(issue.id, done)
        if resolved_at is not None and start <= resolved_at < end:
            resolved[rm.bucket_key(rm.local_date(resolved_at, ctx.tz), bucket,
                                   ctx.week_start)] += 1
    rows = []
    run_created = run_resolved = 0
    for key in keys:
        run_created += created.get(key, 0)
        run_resolved += resolved.get(key, 0)
        rows.append({"bucket": key, "created": created.get(key, 0),
                     "resolved": resolved.get(key, 0),
                     "net": created.get(key, 0) - resolved.get(key, 0),
                     "created_total": run_created, "resolved_total": run_resolved})
    cm.check_rows(ctx, len(rows))
    cumulative = ctx.params.cumulative
    columns = [column("bucket", {"day": "Day", "week": "Week of", "month": "Month"}[bucket],
                      "date"),
               column("created", "Created", "number"), column("resolved", "Resolved", "number"),
               column("net", "Net", "number"),
               column("created_total", "Created (running)", "number"),
               column("resolved_total", "Resolved (running)", "number")]
    chart = {"kind": "line", "x": [k.isoformat() for k in keys],
             "series": [{"key": "created", "label": "Created", "color": "danger",
                         "values": [r["created_total" if cumulative else "created"]
                                    for r in rows]},
                        {"key": "resolved", "label": "Resolved", "color": "success",
                         "values": [r["resolved_total" if cumulative else "resolved"]
                                    for r in rows]}]}
    kpis = [kpi("created", "Created", run_created), kpi("resolved", "Resolved", run_resolved),
            kpi("net", "Net change", run_created - run_resolved)]
    meta = {"bucket": bucket, "cumulative": cumulative}
    if history.earliest_event is not None:
        meta["data_from"] = history.earliest_event
    return result(ctx, "created_vs_resolved", "Created vs resolved", columns=columns,
                  rows=rows, totals={"bucket": "Total", "created": run_created,
                                     "resolved": run_resolved,
                                     "net": run_created - run_resolved},
                  chart=chart, kpis=kpis, meta=meta)


# ---- P3 --------------------------------------------------------------------------------------


def workload(ctx: ReportContext) -> dict:
    """Per assignee: open issues and points, remaining estimate, and time logged in the
    period. Under `own` visibility, other people's time is not shown (None).

    Open means not closed: a canceled issue is nobody's load any more, exactly like a done
    one (09-restructuring.md §9)."""
    issues = sc.issue_query(ctx, status_categories=False).all()
    closed = cm.closed_ids(ctx)
    open_issues = [i for i in issues if i.status_id not in closed]
    spent = cm.spent_by_issue(ctx, (i.id for i in open_issues))
    logged = cm.grouped_seconds(ctx, ["user"])
    people: dict[str | None, dict] = defaultdict(_empty_load)
    for issue in open_issues:
        entry = people[issue.assignee_id]
        entry["open"] += 1
        entry["points"] += issue.story_points or 0
        if issue.time_estimate_seconds:
            entry["remaining"] += max(issue.time_estimate_seconds - spent.get(issue.id, 0), 0)
        if issue.due_date is not None and issue.due_date < ctx.today:
            entry["overdue"] += 1
    for (user_id,) in logged:
        # Someone who logged time but holds no open issue still has a row.
        people.setdefault(user_id, _empty_load())
    names = cm.user_names(ctx, (p for p in people if p))
    rows = []
    for person, entry in people.items():
        seconds = logged.get((person,), 0) if person else None
        if ctx.only_user_id is not None and person != ctx.only_user_id:
            seconds = None
        rows.append({"assignee": names.get(person, "Unknown") if person else "Unassigned",
                     "open": entry["open"], "points": entry["points"],
                     "remaining": entry["remaining"], "overdue": entry["overdue"],
                     "logged": seconds, "_user_id": person})
    rows.sort(key=lambda r: (r["assignee"] == "Unassigned", -r["points"], -r["open"],
                             r["assignee"]))
    cm.check_rows(ctx, len(rows))
    columns = [column("assignee", "Assignee", "user"), column("open", "Open issues", "number"),
               column("points", "Open points", "number"),
               column("remaining", "Remaining estimate", "duration"),
               column("overdue", "Overdue", "number"),
               column("logged", "Logged in period", "duration")]
    chart = {"kind": "bar", "orientation": "horizontal", "x": [r["assignee"] for r in rows],
             "series": [{"key": "open", "label": "Open issues",
                         "values": [r["open"] for r in rows]},
                        {"key": "points", "label": "Open points",
                         "values": [r["points"] for r in rows]}]}
    totals = {"assignee": "Total", "open": sum(r["open"] for r in rows),
              "points": sum(r["points"] for r in rows),
              "remaining": sum(r["remaining"] for r in rows),
              "overdue": sum(r["overdue"] for r in rows),
              "logged": sum(r["logged"] or 0 for r in rows)}
    kpis = [kpi("open", "Open issues", totals["open"]),
            kpi("points", "Open points", totals["points"]),
            kpi("unassigned", "Unassigned", next((r["open"] for r in rows
                                                  if r["_user_id"] is None), 0)),
            kpi("logged", "Logged in period", totals["logged"], "duration")]
    return result(ctx, "workload", "Workload", columns=columns, rows=rows, totals=totals,
                  chart=chart, kpis=kpis)


def _empty_load() -> dict:
    return {"open": 0, "points": 0, "remaining": 0, "overdue": 0}


# ---- epics -----------------------------------------------------------------------------------


def epic_progress(ctx: ReportContext) -> dict:
    """Each epic in the scope: its issues done by count and by points, and time against
    estimate across everything beneath it.

    The counts and percentages are of the epic's issues that are not canceled
    (09-restructuring.md §9): a canceled story is no longer part of the outcome the epic
    stands for, so it neither counts as done nor holds the epic back. It is still shown, in
    its own column. Its estimate and its time stay in the two time totals, because the time
    was really spent and leaving out only its estimate would make the epic look over-run.

    An epic is **closed** when it is in a closed category, delivered or not; `epic_canceled`
    says which of the two, because "Closed" alone would let a dropped epic read as shipped.
    """
    epics = sc.issue_query(ctx, status_categories=False).filter_by(type="epic").all()
    parents = cm.parent_map(ctx)
    children = cm.children_of(parents)
    trees = {epic.id: [i for i in cm.subtree(children, epic.id) if i != epic.id]
             for epic in epics}
    all_ids = list({i for ids in trees.values() for i in ids})
    members: dict[str, Issue] = {}
    for start in range(0, len(all_ids), 900):
        for issue in ctx.db.query(Issue).filter(
                Issue.workspace_id == ctx.access.workspace_id,
                Issue.id.in_(all_ids[start:start + 900])).all():
            members[issue.id] = issue
    names = cm.status_names(ctx)
    done = cm.done_ids(ctx, names)
    canceled = cm.canceled_ids(ctx, names)
    closed = done | canceled
    spent = cm.spent_by_issue(ctx, list(members) + [e.id for e in epics])
    refs = cm.issue_refs(ctx, (e.id for e in epics))
    rows = []
    for epic in epics:
        items = [members[i] for i in trees[epic.id] if i in members]
        counted = [i for i in items if i.status_id not in canceled]
        total = len(counted)
        done_count = sum(1 for i in counted if i.status_id in done)
        points = sum(i.story_points or 0 for i in counted)
        done_points = sum(i.story_points or 0 for i in counted if i.status_id in done)
        rows.append({
            "issue": refs[epic.id]["key"], "title": epic.title, "issues": total,
            "done": done_count, "canceled": len(items) - total,
            "done_pct": _pct(done_count, total), "points": points,
            "done_points": done_points, "points_pct": _pct(done_points, points),
            "estimate": sum(i.time_estimate_seconds or 0 for i in items)
            + (epic.time_estimate_seconds or 0),
            "spent": sum(spent.get(i.id, 0) for i in items) + spent.get(epic.id, 0),
            "epic_done": epic.status_id in closed,
            "epic_canceled": epic.status_id in canceled, "_issue_id": epic.id,
        })
    rows.sort(key=lambda r: r["issue"])
    cm.check_rows(ctx, len(rows))
    columns = [column("issue", "Epic", "issue"), column("title", "Title"),
               column("issues", "Issues", "number"), column("done", "Done", "number"),
               column("canceled", "Canceled", "number"),
               column("done_pct", "Done %", "percent"), column("points", "Points", "number"),
               column("done_points", "Done points", "number"),
               column("points_pct", "Points done %", "percent"),
               column("estimate", "Estimate", "duration"), column("spent", "Spent", "duration"),
               column("epic_done", "Epic closed", "boolean"),
               column("epic_canceled", "Epic canceled", "boolean")]
    chart = {"kind": "bar", "orientation": "horizontal", "unit": "percent",
             "x": [r["issue"] for r in rows],
             "series": [{"key": "done_pct", "label": "Done %",
                         "values": [r["done_pct"] or 0 for r in rows]}]}
    kpis = [kpi("epics", "Epics", len(rows)),
            kpi("closed", "Closed epics", sum(1 for r in rows if r["epic_done"])),
            kpi("canceled", "Canceled epics", sum(1 for r in rows if r["epic_canceled"]))]
    return result(ctx, "epic_progress", "Epic progress", columns=columns, rows=rows,
                  chart=chart, kpis=kpis)
