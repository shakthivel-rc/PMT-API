"""Every report under the fourth category, `canceled` (docs/design/09-restructuring.md §4.2, §9).

Canceled work is closed but not delivered. Before the category existed the only way to close
an issue was a `done` stage, so "not done" and "open" were one set and most reports tested
the first to mean the second. Each test here pins one place where the two now differ: a
percentage of progress that must not count dropped work, an "open" list that must not hold
it, a "resolved" that must not include it — and the CFD band, the removed-stage band and the
cache key that make the category visible and keep it current.

The canceled stage is inserted through the `db` fixture (the workflow editor is another
lane's) and issues reach it through the ordinary board move, so the history every report
replays is the one production writes. The sprint reports — burndown, burnup, velocity, the
sprint report — are in tests/integration/test_sprint_restructure.py with the sprint close.
"""
from datetime import timedelta
from typing import get_args

import pytest

from models.hierarchy import Space, WorkflowStatus
from models.issue import Issue, IssueEvent
from models.reporting import IssueDailyStatus
from schemas.reports import StatusCategory
from services.reports import snapshot
from tests import pm_fixtures as pm
from utils import workflow_rules
from utils.time_tracking import utc_now

pytestmark = pytest.mark.integration

#: A past due date, whatever day the suite runs on.
LONG_AGO = "2026-09-01"


def _stage(db, ws: str, space_id: str, name: str = "Canceled",
           category: str = "canceled") -> str:
    """A stage appended to the space's workflow, written the way a publish would leave it."""
    last = (db.query(WorkflowStatus.position)
            .filter(WorkflowStatus.space_id == space_id)
            .order_by(WorkflowStatus.position.desc()).first())
    row = WorkflowStatus(workspace_id=ws, space_id=space_id, name=name, category=category,
                         position=(last[0] + 1) if last else 0, color="neutral",
                         counts_toward_wip=False, deleted=0)
    db.add(row)
    db.commit()
    return row.id


def _move(client, headers, ws: str, issue_id: str, status_id: str) -> dict:
    return pm.ok(client.post(f"/workspaces/{ws}/issues/{issue_id}/move",
                             json={"status_id": status_id}, headers=headers))


def _report(client, headers, ws: str, report_type: str, status: int = 200, **params) -> dict:
    response = client.get(f"/workspaces/{ws}/reports/{report_type}", params=params,
                          headers=headers)
    assert response.status_code == status, response.text
    return response.json()["data"]


def _kpis(data: dict) -> dict:
    return {k["key"]: k["value"] for k in data["kpis"]}


def _section(data: dict, key: str) -> list[dict]:
    return next(s["rows"] for s in data["sections"] if s["key"] == key)


def _board(client, db):
    """An owner, a space with the default workflow plus a Canceled stage, and one list."""
    owner, ws, space, board_list = pm.setup_board(client, db)
    canceled = _stage(db, ws, space["id"])
    return owner, ws, space, board_list, canceled


class TestTheFilter:
    def test_the_report_category_vocabulary_is_the_workflows(self):
        """`StatusCategory` is spelled out because a Literal must be; this is what stops it
        drifting from the categories a stage can actually have — the drift that made the
        filter refuse `canceled` with a 422 when the category was added."""
        assert get_args(StatusCategory) == workflow_rules.STATUS_CATEGORIES

    def test_a_report_can_be_narrowed_to_canceled_work(self, client, db):
        """Without `canceled` in the parameter model this is a 422, and the one category a
        reader most wants to audit — what was dropped — is the one no report can show."""
        owner, ws, space, board_list, canceled = _board(client, db)
        pm.issue(client, owner, ws, board_list["id"], title="Kept")
        dropped = pm.issue(client, owner, ws, board_list["id"], title="Dropped")
        _move(client, owner, ws, dropped["id"], canceled)
        data = _report(client, owner, ws, "project_overview", scope=f"space:{space['id']}",
                       status_categories=["canceled"])
        assert _kpis(data)["issues"] == 1
        assert [r["status"] for r in data["rows"]] == ["Canceled"]


class TestProjectReports:
    def test_overview_percentages_leave_canceled_out_and_overdue_is_open_work(self, client, db):
        """Four issues — done (5 points), canceled (3), to do (2), in progress (4). Done is
        one of the three that are still meant to happen (33.3%, not 25%) and 5 of their 11
        points (45.5%, not 35.7%). The canceled issue is past its due date and is NOT
        overdue: a dropped issue is not late. Each wrong answer is what the report gave
        when "not done" still meant "open"."""
        owner, ws, space, board_list, canceled = _board(client, db)
        done = pm.issue(client, owner, ws, board_list["id"], title="Done", story_points=5)
        dropped = pm.issue(client, owner, ws, board_list["id"], title="Dropped",
                           story_points=3, due_date=LONG_AGO)
        late = pm.issue(client, owner, ws, board_list["id"], title="Late", story_points=2,
                        due_date=LONG_AGO)
        busy = pm.issue(client, owner, ws, board_list["id"], title="Busy", story_points=4)
        _move(client, owner, ws, done["id"], pm.status_id(space, "Done"))
        _move(client, owner, ws, dropped["id"], canceled)
        _move(client, owner, ws, busy["id"], pm.status_id(space, "In Progress"))

        data = _report(client, owner, ws, "project_overview", scope=f"space:{space['id']}")
        kpis = _kpis(data)
        assert kpis["issues"] == 4 and kpis["canceled"] == 1
        assert kpis["done_pct"] == 33.3
        assert kpis["points_pct"] == 45.5
        assert [r["issue"] for r in _section(data, "overdue")] == [late["key"]]
        assert kpis["overdue"] == 1
        # Counted and shown, just not as progress: its stage's row, its own donut slice,
        # and a column of its own per assignee rather than "open".
        by_status = {r["status"]: r for r in data["rows"]}
        assert by_status["Canceled"]["count"] == 1
        assert by_status["Canceled"]["category"] == "Canceled"
        assert sum(r["share"] for r in data["rows"]) == pytest.approx(100.0)
        donut = dict(zip(data["chart"]["x"], data["chart"]["series"][0]["values"],
                         strict=True))
        assert donut == {"To do": 1, "In progress": 1, "Done": 1, "Canceled": 1}
        [unassigned] = _section(data, "by_assignee")
        assert (unassigned["open"], unassigned["done"], unassigned["canceled"]) == (2, 1, 1)
        assert unassigned["open_points"] == 6
        assert any("canceled" in note for note in data["meta"]["notes"])

    def test_epic_progress_is_of_the_issues_still_meant_to_happen(self, client, db):
        """An epic with a done story (8 points), a canceled story (2) and an open task (3)
        is half done (1 of 2) and 8 of 11 points — not a third, not 8 of 13. Canceling the
        epic itself closes it without it reading as shipped: `epic_done` (closed) and
        `epic_canceled` together say which."""
        owner, ws, space, board_list, canceled = _board(client, db)
        epic = pm.issue(client, owner, ws, board_list["id"], title="Epic", type="epic",
                        assignee_id="owner-1", completion_criteria="Refunds work end to end")
        shipped = pm.issue(client, owner, ws, board_list["id"], title="Shipped", type="story",
                           parent_id=epic["id"], story_points=8)
        dropped = pm.issue(client, owner, ws, board_list["id"], title="Dropped", type="story",
                           parent_id=epic["id"], story_points=2,
                           time_estimate_seconds=3600)
        pm.issue(client, owner, ws, board_list["id"], title="Open", type="task",
                 parent_id=epic["id"], story_points=3)
        _move(client, owner, ws, shipped["id"], pm.status_id(space, "Done"))
        _move(client, owner, ws, dropped["id"], canceled)
        pm.log_time(client, owner, ws, dropped["id"], 1800)

        row = next(r for r in _report(client, owner, ws, "epic_progress")["rows"]
                   if r["title"] == "Epic")
        assert (row["issues"], row["done"], row["canceled"]) == (2, 1, 1)
        assert row["done_pct"] == 50.0
        assert (row["points"], row["done_points"], row["points_pct"]) == (11, 8, 72.7)
        # Time really spent on dropped work stays in the totals, with its estimate.
        assert row["spent"] == 1800 and row["estimate"] == 3600
        assert row["epic_done"] is False and row["epic_canceled"] is False

        _move(client, owner, ws, epic["id"], canceled)
        data = _report(client, owner, ws, "epic_progress")
        row = next(r for r in data["rows"] if r["title"] == "Epic")
        assert row["epic_done"] is True and row["epic_canceled"] is True
        assert _kpis(data)["closed"] == 1 and _kpis(data)["canceled"] == 1

    def test_a_canceled_issue_is_nobodys_workload(self, client, db):
        """Open means not closed. Before, a canceled issue stayed on its assignee's load —
        2 open issues and 8 points here instead of 1 and 5 — for good."""
        owner, ws, _space, board_list, canceled = _board(client, db)
        pm.person(db, "member-1")
        pm.join(db, ws, "member-1", "member")
        pm.issue(client, owner, ws, board_list["id"], title="Open", story_points=5,
                 assignee_id="member-1", due_date=LONG_AGO)
        dropped = pm.issue(client, owner, ws, board_list["id"], title="Dropped",
                           story_points=3, assignee_id="member-1", due_date=LONG_AGO)
        _move(client, owner, ws, dropped["id"], canceled)
        by = {r["_user_id"]: r for r in _report(client, owner, ws, "workload")["rows"]}
        assert (by["member-1"]["open"], by["member-1"]["points"]) == (1, 5)
        assert by["member-1"]["overdue"] == 1

    def test_resolved_means_delivered(self, client, db):
        """Created vs resolved: two created, one done, one canceled — one resolved. Canceling
        is closing, not finishing, and a chart that counted it would show a team keeping up
        with demand by dropping it."""
        owner, ws, space, board_list, canceled = _board(client, db)
        done = pm.issue(client, owner, ws, board_list["id"], title="Done")
        dropped = pm.issue(client, owner, ws, board_list["id"], title="Dropped")
        _move(client, owner, ws, done["id"], pm.status_id(space, "Done"))
        _move(client, owner, ws, dropped["id"], canceled)
        kpis = _kpis(_report(client, owner, ws, "created_vs_resolved", bucket="day"))
        assert kpis == {"created": 2, "resolved": 1, "net": 1}

    def test_the_weekly_report_lists_no_canceled_issue_as_overdue(self, client, db):
        """Overdue now is open work only, in the weekly report as in the overview."""
        owner, ws, _space, board_list, canceled = _board(client, db)
        late = pm.issue(client, owner, ws, board_list["id"], title="Late", due_date=LONG_AGO)
        dropped = pm.issue(client, owner, ws, board_list["id"], title="Dropped",
                           due_date=LONG_AGO)
        _move(client, owner, ws, dropped["id"], canceled)
        data = _report(client, owner, ws, "weekly")
        assert [r["issue"] for r in _section(data, "overdue")] == [late["key"]]


class TestFlowReports:
    def test_cycle_time_measures_delivered_work_only(self, client, db):
        """Both issues went through In Progress; one reached Done, one was canceled. Only
        the first has a cycle time — how long giving up took is not how long delivering
        takes."""
        owner, ws, space, board_list, canceled = _board(client, db)
        done = pm.issue(client, owner, ws, board_list["id"], title="Done")
        dropped = pm.issue(client, owner, ws, board_list["id"], title="Dropped")
        for issue, end in ((done, pm.status_id(space, "Done")), (dropped, canceled)):
            _move(client, owner, ws, issue["id"], pm.status_id(space, "In Progress"))
            _move(client, owner, ws, issue["id"], end)
        data = pm.ok(client.get(f"/workspaces/{ws}/spaces/{space['id']}/cycle-time",
                                headers=owner))
        assert [r["issue"] for r in data["rows"]] == [done["key"]]
        assert _kpis(data)["resolved"] == 1

    def test_the_cfd_draws_a_canceled_band_directly_above_done(self, client, db):
        """One project: a band per stage, bottom first — Done, then Canceled, then the open
        stages (later stages lower), so the closed bands are at the bottom and everything
        above them is the work in flight. Across projects the bands are the four categories
        in the same order. Without the category the canceled issue fell into no band at
        all, and the category view put it nowhere either."""
        owner, ws, space, board_list, canceled = _board(client, db)
        issues = {name: pm.issue(client, owner, ws, board_list["id"], title=name)
                  for name in ("Done", "Dropped", "Review", "Busy", "Todo")}
        _move(client, owner, ws, issues["Done"]["id"], pm.status_id(space, "Done"))
        _move(client, owner, ws, issues["Dropped"]["id"], canceled)
        _move(client, owner, ws, issues["Review"]["id"], pm.status_id(space, "In Review"))
        _move(client, owner, ws, issues["Busy"]["id"], pm.status_id(space, "In Progress"))

        data = pm.ok(client.get(f"/workspaces/{ws}/spaces/{space['id']}/cfd",
                                params={"preset": "today"}, headers=owner))
        bands = [c["label"] for c in data["columns"][1:-1]]
        assert bands == ["Done", "Canceled", "In Review", "In Progress", "To Do"]
        assert [s["label"] for s in data["chart"]["series"]] == bands
        today = data["rows"][-1]
        assert today["s" + canceled] == 1 and today["total"] == 5

        wide = _report(client, owner, ws, "cfd", preset="today")
        assert [c["label"] for c in wide["columns"][1:-1]] == [
            "Done", "Canceled", "In progress", "To do"]
        assert wide["rows"][-1]["scanceled"] == 1
        assert wide["rows"][-1]["sdone"] == 1 and wide["rows"][-1]["sin_progress"] == 2

    def test_a_removed_stage_is_a_band_only_while_it_held_issues(self, client, db):
        """A removed stage keeps its id for history. Over a range in which it held an issue
        it is a band, labelled "(removed)" so nobody hunts the board for it; over a range in
        which it held nothing, and for a removed stage that never held anything, there is no
        band. Before, every removed stage was a band, unlabelled, in every range."""
        owner, ws, space, board_list = pm.setup_board(client, db)
        old = _stage(db, ws, space["id"], "Old QA", "in_progress")
        never = _stage(db, ws, space["id"], "Never used", "in_progress")
        issue = pm.issue(client, owner, ws, board_list["id"], title="A")
        _move(client, owner, ws, issue["id"], old)
        _move(client, owner, ws, issue["id"], pm.status_id(space, "To Do"))
        now = utc_now()
        for stage_id in (old, never):
            row = db.get(WorkflowStatus, stage_id)
            row.deleted, row.deleted_at = 1, now
        # Created four days ago, into Old QA three days ago, back out yesterday: the stage
        # held it at the end of two days of the range, and holds nothing today.
        db.get(Issue, issue["id"]).created_at = now - timedelta(days=4)
        into, out = (db.query(IssueEvent)
                     .filter(IssueEvent.issue_id == issue["id"],
                             IssueEvent.event == "status_changed")
                     .order_by(IssueEvent.created_at).all())
        into.created_at = now - timedelta(days=3)
        out.created_at = now - timedelta(days=1)
        db.commit()

        today = now.date()
        data = _report(client, owner, ws, "cfd", scope=f"space:{space['id']}",
                       date_from=(today - timedelta(days=4)).isoformat(),
                       date_to=today.isoformat())
        labels = {c["key"]: c["label"] for c in data["columns"]}
        assert labels["s" + old] == "Old QA (removed)"
        assert "s" + never not in labels
        assert [r["s" + old] for r in data["rows"]] == [0, 1, 1, 0, 0]
        # Every issue on every day is in some band, so the total is the sum of the bands.
        for row in data["rows"]:
            assert row["total"] == sum(v for k, v in row.items() if k.startswith("s"))

        only_today = _report(client, owner, ws, "cfd", scope=f"space:{space['id']}",
                             preset="today")
        assert "s" + old not in {c["key"] for c in only_today["columns"]}


class TestSnapshotsAndCache:
    def test_a_snapshot_records_canceled_as_itself(self, client, db):
        """The daily snapshot stores the category the stage has — never folded into `done`,
        which is what a stage named "Canceled" had to be before the category existed."""
        owner, ws, _space, board_list, canceled = _board(client, db)
        dropped = pm.issue(client, owner, ws, board_list["id"], title="Dropped")
        _move(client, owner, ws, dropped["id"], canceled)
        assert snapshot.write_day(db, ws, utc_now().date(), "UTC") == 1
        db.commit()
        [row] = db.query(IssueDailyStatus).all()
        assert (row.status_id, row.status_category) == (canceled, "canceled")

    def test_a_new_workflow_version_is_a_cache_miss(self, client, db):
        """The cache key's data version includes the spaces' workflow versions, which every
        publish increments. Without them nothing a report's version reads changes when a
        workflow is published, and the next minute of reports is answered from before it."""
        owner, ws, space, board_list, _canceled = _board(client, db)
        pm.issue(client, owner, ws, board_list["id"], title="A")
        params = {"scope": f"space:{space['id']}"}
        assert _report(client, owner, ws, "project_overview", **params)["meta"]["cached"] is False
        assert _report(client, owner, ws, "project_overview", **params)["meta"]["cached"] is True
        db.get(Space, space["id"]).workflow_version += 1
        db.commit()
        assert _report(client, owner, ws, "project_overview", **params)["meta"]["cached"] is False

    def test_recategorising_a_stage_is_not_answered_from_the_cache(self, client, db):
        """The case the version exists for: a legacy "Won't do" stage in the `done` category
        — the only way to close work before `canceled` — re-categorised by a publish. Its
        issues' history is re-labelled without a single event being written, so only the
        workflow version can tell the cache. Before it, done stays at 50% for another minute;
        after, it is 0% of the one issue still meant to happen."""
        owner, ws, space, board_list = pm.setup_board(client, db)
        wont = _stage(db, ws, space["id"], "Won't do", "done")
        pm.issue(client, owner, ws, board_list["id"], title="Open")
        dropped = pm.issue(client, owner, ws, board_list["id"], title="Dropped")
        _move(client, owner, ws, dropped["id"], wont)
        params = {"scope": f"space:{space['id']}"}
        assert _kpis(_report(client, owner, ws, "project_overview", **params))["done_pct"] == 50.0
        assert _report(client, owner, ws, "project_overview", **params)["meta"]["cached"] is True

        db.get(WorkflowStatus, wont).category = "canceled"
        db.get(Space, space["id"]).workflow_version += 1
        db.commit()
        data = _report(client, owner, ws, "project_overview", **params)
        assert data["meta"]["cached"] is False
        assert _kpis(data)["done_pct"] == 0.0 and _kpis(data)["canceled"] == 1
