"""Sprint reports: S1 sprint report, S2 burndown, S3 burnup, S4 velocity
(docs/design/08-reports.md §4, §5.5).

Membership comes from `sprint_issues` (who was in the sprint at instant T) and everything
else from `History` (status, points and estimate at T). A closed sprint is read at the
instant before it closed: closing removes the unfinished issues at `closed_at`, and the
report is about what the sprint held up to then.

**Canceled work** (docs/design/09-restructuring.md §9) is closed but not delivered. It is
never "completed": the sprint report gives it its own outcome, and the burndown and burnup
take it out of the scope at the instant it was canceled — remaining work falls without
anything being burned, and the scope-change list says why. Moving an issue out of a canceled
stage puts it back in the scope ("Restored"), so the list always explains the line.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta

from sqlalchemy import func

from models.issue import Issue
from models.sprint import Sprint, SprintIssue
from models.time_tracking import TimeEntry
from services import sprint as sprint_svc
from services.reports import common as cm
from services.reports import scope as sc
from services.reports.base import ReportContext, ReportError, column, kpi, result, section
from services.reports.history import History, naive
from utils import report_math as rm

#: Two timestamps this close are one act: `close_sprint` writes `closed_at` and the removal
#: of the unfinished issues in one transaction, but MySQL stores whole seconds.
_SAME_ACT = timedelta(seconds=2)

_UNIT_LABELS = {"points": "Story points", "count": "Issue count", "hours": "Remaining hours"}

#: A sprint report row's outcome, in the order the rows are listed: what was delivered, what
#: is still open, what the team decided not to do, and what left the sprint.
_OUTCOME_LABELS = {"completed": "Completed", "not_completed": "Not completed",
                   "canceled": "Canceled", "removed": "Removed"}


def resolve_sprint(ctx: ReportContext) -> Sprint:
    """The sprint a sprint report is about: `sprint_id`, the sprint scope, or the scope
    project's active sprint, else its most recently closed one."""
    db, ws = ctx.db, ctx.access.workspace_id
    if ctx.params.sprint_id:
        sprint = sprint_svc.get_sprint(db, ws, ctx.params.sprint_id)
        if sprint is None:
            raise ReportError(404, "Sprint not found")
        return sprint
    if ctx.scope.sprint is not None:
        return ctx.scope.sprint
    if ctx.scope.space_id:
        sprint = sprint_svc.active_sprint(db, ws, ctx.scope.space_id)
        if sprint is None:
            closed = sprint_svc.space_sprints(db, ws, ctx.scope.space_id, ["closed"])
            sprint = closed[0] if closed else None
        if sprint is not None:
            return sprint
        raise ReportError(422, "This project has no started sprint yet.")
    raise ReportError(422, "Choose a sprint, or a project with one.")


def _window(ctx: ReportContext, sprint: Sprint) -> tuple[datetime, datetime]:
    """[started, end): when the sprint ran, as naive UTC."""
    if sprint.state == "planned" or sprint.started_at is None:
        raise ReportError(422, "This sprint has not started yet.")
    started = naive(sprint.started_at)
    end = naive(sprint.closed_at) if sprint.state == "closed" and sprint.closed_at else ctx.now
    if started is None or end is None:
        raise ReportError(422, "This sprint has not started yet.")
    ctx.date_from = sprint.starts_on
    ctx.date_to = sprint.ends_on
    return started, end


def _memberships(ctx: ReportContext, sprint: Sprint) -> dict[str, list[SprintIssue]]:
    rows = ctx.db.query(SprintIssue).filter(
        SprintIssue.workspace_id == sprint.workspace_id,
        SprintIssue.sprint_id == sprint.id).order_by(SprintIssue.added_at).all()
    out: dict[str, list[SprintIssue]] = defaultdict(list)
    for row in rows:
        out[row.issue_id].append(row)
    return out


def _sprint_issues(ctx: ReportContext, ids: list[str]) -> list[Issue]:
    """The live issues among `ids` that pass the report's filters."""
    if not ids:
        return []
    return (sc.apply_filters(ctx.db.query(Issue).filter(
        Issue.workspace_id == ctx.access.workspace_id, Issue.deleted == 0,
        Issue.id.in_(ids)), ctx, status_categories=False).all())


def _arrow(before: int | None, after: int | None) -> str:
    if before == after:
        return ""
    return f"{'-' if before is None else before} → {'-' if after is None else after}"


# ---- S1 --------------------------------------------------------------------------------------


def sprint_report(ctx: ReportContext) -> dict:
    sprint = resolve_sprint(ctx)
    started, end = _window(ctx, sprint)
    members = _memberships(ctx, sprint)
    issues = _sprint_issues(ctx, list(members))
    history = History(ctx.db, ctx.access.workspace_id, issues)
    statuses = cm.status_names(ctx)
    done = cm.done_ids(ctx, statuses)
    canceled = cm.canceled_ids(ctx, statuses)
    refs = cm.issue_refs(ctx, (i.id for i in issues))
    names = cm.user_names(ctx, (i.assignee_id for i in issues))
    closed = sprint.state == "closed"
    read_at = end - timedelta(microseconds=1) if closed else end

    rows = []
    for issue in issues:
        spans = members[issue.id]
        added_after = any(naive(s.added_at) > started + _SAME_ACT for s in spans)
        in_at_start = any(naive(s.added_at) <= started + _SAME_ACT
                          and (s.removed_at is None or naive(s.removed_at) > started)
                          for s in spans)
        last = spans[-1]
        removed_at = naive(last.removed_at)
        # In a closed sprint, a membership that ended at the close is the carry-over of an
        # unfinished issue, not a removal; in an open sprint any ended membership is one.
        if removed_at is not None and (not closed or removed_at < end - _SAME_ACT):
            outcome = "removed"
            at = removed_at - timedelta(microseconds=1)
        else:
            at = read_at
            status_then = history.status_at(issue.id, at)
            # Canceled is its own outcome, never "completed" and never "not completed": the
            # first would count work nobody delivered, the second would read as work the
            # sprint failed to finish, when the team decided not to do it.
            outcome = ("completed" if status_then in done
                       else "canceled" if status_then in canceled else "not_completed")
        points_start = history.points_at(issue.id, started) if in_at_start else None
        points_end = history.points_at(issue.id, at)
        status_id = history.status_at(issue.id, at) or issue.status_id
        rows.append({
            "issue": refs[issue.id]["key"] + (" *" if added_after else ""),
            "title": issue.title, "type": issue.type,
            "assignee": names.get(issue.assignee_id, ""),
            "status": statuses.get(status_id, ("", ""))[0],
            "outcome": _OUTCOME_LABELS[outcome],
            "points": points_end,
            "points_change": _arrow(points_start, points_end) if in_at_start else "",
            "added_after_start": added_after, "_outcome": outcome, "_issue_id": issue.id,
        })
    order = {outcome: index for index, outcome in enumerate(_OUTCOME_LABELS)}
    rows.sort(key=lambda r: (order[r["_outcome"]], r["issue"]))
    cm.check_rows(ctx, len(rows))

    completed = [r for r in rows if r["_outcome"] == "completed"]
    not_completed = [r for r in rows if r["_outcome"] == "not_completed"]
    canceled_rows = [r for r in rows if r["_outcome"] == "canceled"]
    removed = [r for r in rows if r["_outcome"] == "removed"]
    added = [r for r in rows if r["added_after_start"]]
    completed_points = (sprint.completed_points if closed and sprint.completed_points is not None
                        else sum(r["points"] or 0 for r in completed))
    committed = sprint.committed_points or 0
    hours_query = sc.entry_query(ctx, func.sum(TimeEntry.duration_seconds)).filter(
        TimeEntry.issue_id.in_([i.id for i in issues] or ["-"]))
    if sprint.starts_on:
        hours_query = hours_query.filter(TimeEntry.work_date >= sprint.starts_on)
    last_day = rm.local_date(end, ctx.tz)
    hours_query = hours_query.filter(TimeEntry.work_date <= last_day)
    logged = int(hours_query.scalar() or 0)

    columns = [column("issue", "Issue", "issue"), column("title", "Title"),
               column("type", "Type"), column("assignee", "Assignee", "user"),
               column("status", "Status"), column("outcome", "Outcome"),
               column("points", "Points", "number"),
               column("points_change", "Points changed"),
               column("added_after_start", "Added after start", "boolean")]
    sub_cols = [c for c in columns if c["key"] not in ("outcome",)]
    sections = [
        section("completed", "Completed issues", sub_cols, completed),
        section("not_completed", "Issues not completed", sub_cols, not_completed),
        section("canceled", "Issues canceled", sub_cols, canceled_rows),
        section("removed", "Issues removed from the sprint", sub_cols, removed),
        section("added", "Issues added after the sprint started (*)", sub_cols, added),
    ]
    kpis = [kpi("committed", "Committed points", committed),
            kpi("completed_points", "Completed points", completed_points),
            kpi("completed", "Issues completed", len(completed)),
            kpi("not_completed", "Not completed", len(not_completed)),
            kpi("canceled", "Canceled", len(canceled_rows)),
            kpi("removed", "Removed", len(removed)), kpi("added", "Added after start", len(added)),
            kpi("logged", "Time logged in the sprint", logged, "duration")]
    chart = {"kind": "bar", "x": ["Committed", "Completed"],
             "series": [{"key": "points", "label": "Points",
                         "values": [committed, completed_points]}]}
    meta = {"sprint": _sprint_meta(sprint), "goal": sprint.goal}
    return result(ctx, "sprint_report", f"Sprint report — {sprint.name}", columns=columns,
                  rows=rows, chart=chart, kpis=kpis, sections=sections, meta=meta)


def _sprint_meta(sprint: Sprint) -> dict:
    return {"id": sprint.id, "name": sprint.name, "state": sprint.state,
            "space_id": sprint.space_id, "starts_on": sprint.starts_on,
            "ends_on": sprint.ends_on, "started_at": sprint.started_at,
            "closed_at": sprint.closed_at, "committed_points": sprint.committed_points,
            "completed_points": sprint.completed_points}


# ---- S2 / S3 ---------------------------------------------------------------------------------


class _Replay:
    """The sprint's scope and progress at any instant, in the report's unit."""

    def __init__(self, ctx: ReportContext, sprint: Sprint):
        self.ctx = ctx
        self.sprint = sprint
        self.members = _memberships(ctx, sprint)
        self.issues = _sprint_issues(ctx, list(self.members))
        self.history = History(ctx.db, ctx.access.workspace_id, self.issues)
        names = cm.status_names(ctx)
        self.done = cm.done_ids(ctx, names)
        self.canceled = cm.canceled_ids(ctx, names)
        self.unit = ctx.params.unit
        self.spent_by_day: dict[str, list[tuple]] = defaultdict(list)
        if self.unit == "hours" and self.issues:
            rows = ctx.db.query(TimeEntry.issue_id, TimeEntry.work_date,
                                func.sum(TimeEntry.duration_seconds)).filter(
                TimeEntry.workspace_id == ctx.access.workspace_id, TimeEntry.deleted == 0,
                TimeEntry.issue_id.in_([i.id for i in self.issues])).group_by(
                TimeEntry.issue_id, TimeEntry.work_date).all()
            for issue_id, work_date, seconds in rows:
                self.spent_by_day[issue_id].append((cm._as_date(work_date), int(seconds or 0)))

    def in_sprint(self, issue_id: str, at: datetime) -> bool:
        return any(naive(s.added_at) <= at and (s.removed_at is None or naive(s.removed_at) > at)
                   for s in self.members[issue_id])

    def canceled_at(self, issue_id: str, at: datetime) -> bool:
        """Whether the issue was in a canceled stage at `at` — out of the scope then, wherever
        its membership says it was."""
        return self.history.status_at(issue_id, at) in self.canceled

    def value(self, issue_id: str, at: datetime) -> float:
        if self.unit == "count":
            return 1
        if self.unit == "points":
            return self.history.points_at(issue_id, at) or 0
        estimate = self.history.estimate_at(issue_id, at) or 0
        day = rm.local_date(at, self.ctx.tz)
        spent = sum(s for d, s in self.spent_by_day.get(issue_id, []) if d <= day)
        return round(max(estimate - spent, 0) / 3600, 2)

    def totals(self, at: datetime) -> tuple[float, float]:
        """(scope, done) at `at`. An issue canceled by then is in neither: it left the scope
        at the instant it was canceled, without being completed (09-restructuring.md §9)."""
        scope = done = 0.0
        for issue in self.issues:
            if not self.history.existed(issue.id, at) or not self.in_sprint(issue.id, at):
                continue
            status = self.history.status_at(issue.id, at)
            if status in self.canceled:
                continue
            value = self.value(issue.id, at)
            scope += value
            if status in self.done:
                done += value
        return scope, done


def _days(ctx: ReportContext, sprint: Sprint, started: datetime,
          end: datetime) -> tuple[list, int]:
    """The sprint's days, and how many of them have happened (are drawn)."""
    first = sprint.starts_on or rm.local_date(started, ctx.tz)
    last_planned = sprint.ends_on or first
    last_real = rm.local_date(end, ctx.tz)
    days = rm.days_between(first, max(last_planned, last_real))
    elapsed = sum(1 for d in days if d <= last_real)
    return days, elapsed


def _scope_changes(ctx: ReportContext, replay: _Replay, started: datetime,
                   end: datetime, closed: bool) -> list[dict]:
    """Every step the scope took after the start: issues added and removed, and — since the
    `canceled` category (09-restructuring.md §9) — issues canceled while in the sprint and
    restored from a canceled stage. Together they account for every change in the scope
    line, so an added or removed issue that was canceled at that moment is not listed: it
    was not part of the scope either side of the change."""
    # As in the sprint report: a membership ending at the close is carry-over, not scope.
    cutoff = end - _SAME_ACT if closed else end + timedelta(seconds=1)
    refs = cm.issue_refs(ctx, (i.id for i in replay.issues))
    changes = []

    def step(at: datetime, issue_id: str, change: str, value: float) -> None:
        changes.append({"at": at, "date": rm.local_date(at, ctx.tz),
                        "issue": refs[issue_id]["key"], "change": change, "value": value})

    for issue in replay.issues:
        for span in replay.members[issue.id]:
            added = naive(span.added_at)
            if (added > started + _SAME_ACT and added < end
                    and not replay.canceled_at(issue.id, added)):
                step(added, issue.id, "Added", replay.value(issue.id, added))
            removed = naive(span.removed_at)
            if removed is not None and started < removed < cutoff:
                before = removed - timedelta(microseconds=1)
                if not replay.canceled_at(issue.id, before):
                    step(removed, issue.id, "Removed", -replay.value(issue.id, before))
        for at, from_value, to_value in replay.history.status_changes(issue.id):
            if not started < at < end or not replay.in_sprint(issue.id, at):
                continue
            was, now = from_value in replay.canceled, to_value in replay.canceled
            if now and not was:
                before = at - timedelta(microseconds=1)
                step(at, issue.id, "Canceled", -replay.value(issue.id, before))
            elif was and not now:
                step(at, issue.id, "Restored", replay.value(issue.id, at))
    changes.sort(key=lambda c: c["at"])
    for change in changes:
        change.pop("at")
    return changes


def burndown(ctx: ReportContext) -> dict:
    """Remaining work at the end of each day; the guideline falls from the starting total
    to zero across the working days and stays flat on the others."""
    sprint = resolve_sprint(ctx)
    started, end = _window(ctx, sprint)
    replay = _Replay(ctx, sprint)
    days, elapsed = _days(ctx, sprint, started, end)
    start_scope, start_done = replay.totals(started)
    start_value = start_scope - start_done
    guide_days = [d for d in days if not sprint.ends_on or d <= sprint.ends_on]
    guide = rm.guideline(start_value, guide_days, ctx.settings.working_days)
    closed_read = end - timedelta(microseconds=1)
    rows = []
    previous_done = start_done
    for index, day in enumerate(days):
        if index < elapsed:
            at = min(rm.end_of_day_utc(day, ctx.tz), closed_read if sprint.state == "closed"
                     else end)
            scope, done_value = replay.totals(at)
            remaining: float | None = round(scope - done_value, 2)
            burned: float | None = round(done_value - previous_done, 2)
            previous_done = done_value
        else:
            remaining = burned = None
        rows.append({"date": day, "remaining": remaining,
                     "guideline": guide[index] if index < len(guide) else 0.0,
                     "completed": burned,
                     "working_day": day.weekday() in ctx.settings.working_days})
    changes = _scope_changes(ctx, replay, started, end, sprint.state == "closed")
    unit = ctx.params.unit
    kind = "number"
    columns = [column("date", "Date", "date"), column("remaining", f"Remaining ({unit})", kind),
               column("guideline", "Guideline", kind),
               column("completed", f"Completed that day ({unit})", kind),
               column("working_day", "Working day", "boolean")]
    chart = {"kind": "line", "step": True, "unit": unit, "x": [d.isoformat() for d in days],
             "series": [{"key": "remaining", "label": "Remaining",
                         "values": [r["remaining"] for r in rows]},
                        {"key": "guideline", "label": "Guideline", "dashed": True,
                         "color": "muted", "values": [r["guideline"] for r in rows]}],
             "non_working": [d.isoformat() for d in days
                             if d.weekday() not in ctx.settings.working_days]}
    last = next((r["remaining"] for r in reversed(rows) if r["remaining"] is not None), None)
    kpis = [kpi("start", f"At start ({_UNIT_LABELS[unit]})", round(start_value, 2)),
            kpi("remaining", "Remaining", last),
            kpi("added", "Scope added", round(sum(c["value"] for c in changes
                                                   if c["change"] == "Added"), 2)),
            kpi("removed", "Scope removed", round(-sum(c["value"] for c in changes
                                                      if c["change"] == "Removed"), 2)),
            # Net of restores: the scope canceling took out, as `removed` is the scope that
            # removals took out. Re-estimates move the line too and are in neither.
            kpi("canceled", "Scope canceled",
                round(-sum(c["value"] for c in changes
                           if c["change"] in ("Canceled", "Restored")), 2))]
    sections = [section("scope_changes", "Scope changes",
                        [column("date", "Date", "date"), column("issue", "Issue", "issue"),
                         column("change", "Change"), column("value", unit, "number")],
                        changes)]
    meta = {"sprint": _sprint_meta(sprint), "unit": unit, "start_value": round(start_value, 2)}
    if replay.history.earliest_event is not None:
        meta["data_from"] = replay.history.earliest_event
    return result(ctx, "burndown", f"Burndown — {sprint.name}", columns=columns, rows=rows,
                  chart=chart, kpis=kpis, sections=sections, meta=meta)


def burnup(ctx: ReportContext) -> dict:
    """Completed work and total scope at the end of each day."""
    sprint = resolve_sprint(ctx)
    started, end = _window(ctx, sprint)
    replay = _Replay(ctx, sprint)
    days, elapsed = _days(ctx, sprint, started, end)
    closed_read = end - timedelta(microseconds=1)
    rows = []
    for index, day in enumerate(days):
        if index < elapsed:
            at = min(rm.end_of_day_utc(day, ctx.tz), closed_read if sprint.state == "closed"
                     else end)
            scope, done_value = replay.totals(at)
            rows.append({"date": day, "scope": round(scope, 2), "completed": round(done_value, 2),
                         "remaining": round(scope - done_value, 2)})
        else:
            rows.append({"date": day, "scope": None, "completed": None, "remaining": None})
    unit = ctx.params.unit
    columns = [column("date", "Date", "date"), column("scope", f"Total scope ({unit})", "number"),
               column("completed", f"Completed ({unit})", "number"),
               column("remaining", f"Remaining ({unit})", "number")]
    chart = {"kind": "line", "step": True, "unit": unit, "x": [d.isoformat() for d in days],
             "series": [{"key": "scope", "label": "Total scope", "color": "muted",
                         "values": [r["scope"] for r in rows]},
                        {"key": "completed", "label": "Completed", "color": "success",
                         "values": [r["completed"] for r in rows]}]}
    drawn = [r for r in rows if r["scope"] is not None]
    kpis = [kpi("scope", "Scope", drawn[-1]["scope"] if drawn else None),
            kpi("completed", "Completed", drawn[-1]["completed"] if drawn else None)]
    return result(ctx, "burnup", f"Burnup — {sprint.name}", columns=columns, rows=rows,
                  chart=chart, kpis=kpis, meta={"sprint": _sprint_meta(sprint), "unit": unit})


# ---- S4 --------------------------------------------------------------------------------------


def velocity(ctx: ReportContext) -> dict:
    """Committed and completed points of the last N closed sprints, frozen at start and
    close, with the average completed.

    Completed is `done` only: `services/sprint.close_sprint` freezes the points of the issues
    in a done stage at the close, never a canceled one, and `start_sprint` leaves an issue
    already canceled out of the commitment (09-restructuring.md §9). Both are frozen, so
    re-categorising a stage later does not rewrite a past sprint's numbers — the same reason
    re-estimating an issue does not."""
    query = ctx.db.query(Sprint).filter(Sprint.workspace_id == ctx.access.workspace_id,
                                        Sprint.state == "closed", Sprint.deleted == 0)
    if ctx.scope.space_id:
        query = query.filter(Sprint.space_id == ctx.scope.space_id)
    sprints = query.order_by(Sprint.closed_at.desc()).limit(ctx.params.sprint_count).all()
    sprints.reverse()
    rows = [{"sprint": s.name, "starts_on": s.starts_on, "ends_on": s.ends_on,
             "committed": s.committed_points or 0, "completed": s.completed_points or 0,
             "pct": round(100 * (s.completed_points or 0) / s.committed_points, 1)
             if s.committed_points else None, "_sprint_id": s.id} for s in sprints]
    average = round(sum(r["completed"] for r in rows) / len(rows), 1) if rows else None
    average_committed = round(sum(r["committed"] for r in rows) / len(rows), 1) if rows else None
    columns = [column("sprint", "Sprint"), column("starts_on", "Start", "date"),
               column("ends_on", "End", "date"), column("committed", "Committed", "number"),
               column("completed", "Completed", "number"),
               column("pct", "Completed of committed", "percent")]
    chart = {"kind": "grouped_bar", "x": [r["sprint"] for r in rows],
             "series": [{"key": "committed", "label": "Commitment", "color": "muted",
                         "values": [r["committed"] for r in rows]},
                        {"key": "completed", "label": "Completed", "color": "success",
                         "values": [r["completed"] for r in rows]}],
             "reference": {"label": "Average completed", "value": average}}
    kpis = [kpi("average", "Average velocity", average),
            kpi("average_committed", "Average commitment", average_committed),
            kpi("sprints", "Sprints", len(rows))]
    return result(ctx, "velocity", "Velocity", columns=columns, rows=rows,
                  totals={"sprint": "Average", "committed": average_committed,
                          "completed": average},
                  chart=chart, kpis=kpis, meta={"sprint_count": ctx.params.sprint_count})
