"""Reports, through the HTTP API: the numbers, who may see them, exports, jobs, saved reports,
schedules and snapshots.

Read the tenancy and visibility tests first: every other test here would still pass if one
workspace could read another's hours through a report, or a member could read a colleague's
time in a workspace that switched that off.

The time data is fixed in the past (work dates in the week of 28 Sep 2026) and read with a
custom range, so no expectation depends on the day the suite runs. Issue history (created,
moved, sprinted) happens "now", and the reports that read it use their default range, which
always includes today.
"""
from datetime import timedelta
from io import BytesIO

import pytest
from sqlalchemy import event

import db.session as db_session
from models.activity_log import ActivityLog
from models.reporting import IssueDailyStatus, IssueSnapshotDay, ReportJob, ReportSchedule
from models.sprint import Sprint, SprintIssue
from services.reports import engine, jobs, scheduler, snapshot
from services.reports.registry import REPORTS
from tests import pm_fixtures as pm
from utils.time_tracking import utc_now
from workers import report_worker

pytestmark = pytest.mark.integration

WEEK = {"date_from": "2026-09-28", "date_to": "2026-10-04"}


def _report(client, headers, ws, report_type, status=200, **params):
    response = client.get(f"/workspaces/{ws}/reports/{report_type}", params=params,
                          headers=headers)
    assert response.status_code == status, response.text
    return response.json()["data"]


def _export(client, headers, ws, report_type, fmt="csv", **body):
    return client.post(f"/workspaces/{ws}/reports/{report_type}/export",
                       json={"format": fmt, **body}, headers=headers)


def _settings(client, headers, ws, path="reports", **changes):
    return pm.ok(client.patch(f"/workspaces/{ws}/settings/{path}", json=changes,
                              headers=headers))


def _team(client, db):
    """Owner and member in one workspace; two issues; time logged by both:

    | who    | issue | day        | seconds | billable |
    |--------|-------|------------|---------|----------|
    | owner  | A     | 2026-09-28 | 3600    | yes      |
    | member | A     | 2026-09-29 | 1800    | yes      |
    | member | B     | 2026-09-29 | 900     | no       |
    | member | B     | 2026-09-27 | 600     | yes      |  (the Sunday before)
    """
    owner, ws, space, board_list = pm.setup_board(client, db)
    member = pm.person(db, "member-1")
    pm.join(db, ws, "member-1", "member")
    a = pm.issue(client, owner, ws, board_list["id"], title="A", story_points=5,
                 time_estimate_seconds=7200, assignee_id="member-1")
    b = pm.issue(client, owner, ws, board_list["id"], title="B", story_points=3)
    pm.log_time(client, owner, ws, a["id"], 3600, work_date="2026-09-28")
    pm.log_time(client, member, ws, a["id"], 1800, work_date="2026-09-29")
    pm.log_time(client, member, ws, b["id"], 900, work_date="2026-09-29", billable=False)
    pm.log_time(client, member, ws, b["id"], 600, work_date="2026-09-27")
    return owner, member, ws, space, board_list, a, b


class TestTenancy:
    def test_a_non_member_gets_404_for_every_report_path(self, client, db):
        _owner, _member, ws, space, *_ = _team(client, db)
        stranger = pm.person(db, "stranger")
        for method, path, body in (
            ("get", f"/workspaces/{ws}/reports/catalog", None),
            ("get", f"/workspaces/{ws}/reports/timesheet", None),
            ("post", f"/workspaces/{ws}/reports/timesheet/export", {"format": "csv"}),
            ("get", f"/workspaces/{ws}/report-jobs", None),
            ("get", f"/workspaces/{ws}/saved-reports", None),
            ("get", f"/workspaces/{ws}/settings/reports", None),
            ("get", f"/workspaces/{ws}/spaces/{space['id']}/velocity", None),
        ):
            kwargs = {"headers": stranger}
            if body is not None:
                kwargs["json"] = body
            response = getattr(client, method)(path, **kwargs)
            assert response.status_code == 404, (method, path)

    def test_a_scope_in_another_workspace_is_not_found(self, client, db):
        owner, _member, ws, _space, _board_list, *_ = _team(client, db)
        headers_b, ws_b, space_b, list_b = pm.setup_board(client, db, "bob", "bravo", "BRA")
        theirs = pm.issue(client, headers_b, ws_b, list_b["id"], title="Secret")
        pm.log_time(client, headers_b, ws_b, theirs["id"], 7200, work_date="2026-09-28")
        for scope in (f"space:{space_b['id']}", f"list:{list_b['id']}"):
            _report(client, owner, ws, "timesheet", 404, scope=scope, **WEEK)
        # Their hours never appear in our workspace-wide report.
        mine = _report(client, owner, ws, "timesheet", **WEEK)
        assert mine["totals"]["total"] == 6300

    def test_unknown_report_type_is_404_and_bad_params_422(self, client, db):
        owner, _member, ws, *_ = _team(client, db)
        _report(client, owner, ws, "nope", 404)
        _report(client, owner, ws, "timesheet", 422, scope="issue:1")
        _report(client, owner, ws, "timesheet", 422, group_by="user,user")


class TestTimeReports:
    def test_timesheet_per_person_per_day(self, client, db):
        owner, _member, ws, *_ = _team(client, db)
        data = _report(client, owner, ws, "timesheet", **WEEK)
        by_person = {row["_user_id"]: row for row in data["rows"]}
        assert by_person["owner-1"]["d2026-09-28"] == 3600
        assert by_person["owner-1"]["total"] == 3600
        assert by_person["member-1"]["d2026-09-29"] == 2700
        assert by_person["member-1"]["total"] == 2700
        assert data["totals"]["total"] == 6300
        kpis = {k["key"]: k["value"] for k in data["kpis"]}
        assert kpis["total"] == 6300 and kpis["billable"] == 5400
        assert kpis["non_billable"] == 900 and kpis["people"] == 2
        assert len(data["chart"]["x"]) == 7

    def test_billable_filter(self, client, db):
        owner, _member, ws, *_ = _team(client, db)
        data = _report(client, owner, ws, "timesheet", billable="non_billable", **WEEK)
        assert data["totals"]["total"] == 900

    def test_time_by_two_groupings_is_a_tree_with_drill_down(self, client, db):
        owner, _member, ws, _space, _list, _a, _b = _team(client, db)
        data = _report(client, owner, ws, "time_by_group", group_by=["user", "issue"], **WEEK)
        tops = [r for r in data["rows"] if r["_depth"] == 0]
        children = [r for r in data["rows"] if r["_depth"] == 1]
        assert {r["group"]: r["seconds"] for r in tops} == {"Test User": 3600 + 2700} or \
            sorted(r["seconds"] for r in tops) == [2700, 3600]
        assert sum(r["seconds"] for r in children) == 6300
        assert data["totals"]["seconds"] == 6300
        drill = tops[0]["_drill"]
        assert drill["date_from"] == "2026-09-28" and drill["user_ids"]

    def test_rounding_applies_per_entry_before_summing(self, client, db):
        """15-minute rounding up: 3600, 1800 stay; 900 stays. Add a 7-minute entry, which
        becomes 15 — so the total rises by 900, not by 420."""
        owner, _member, ws, _space, _list, a, _b = _team(client, db)
        pm.log_time(client, owner, ws, a["id"], 420, work_date="2026-09-30")
        plain = _report(client, owner, ws, "timesheet", **WEEK)
        rounded = _report(client, owner, ws, "timesheet", rounding_minutes=15,
                          rounding_mode="up", **WEEK)
        assert plain["totals"]["total"] == 6720
        assert rounded["totals"]["total"] == 7200

    def test_time_entries_lists_each_entry(self, client, db):
        owner, _member, ws, *_ = _team(client, db)
        data = _report(client, owner, ws, "time_entries", **WEEK)
        assert sorted(r["seconds"] for r in data["rows"]) == [900, 1800, 3600]
        assert {r["billable"] for r in data["rows"]} == {True, False}

    def test_estimate_vs_actual(self, client, db):
        owner, _member, ws, _space, _list, _a, _b = _team(client, db)
        data = _report(client, owner, ws, "estimate_vs_actual")
        row = next(r for r in data["rows"] if r["title"] == "A")
        assert row["estimate"] == 7200 and row["spent"] == 5400
        assert row["remaining"] == 1800 and row["variance"] == -1800
        assert row["accuracy"] == 75.0 and row["over"] is False

    def test_weekly_report_has_its_sections(self, client, db):
        owner, _member, ws, *_ = _team(client, db)
        data = _report(client, owner, ws, "weekly", date_from="2026-09-30")
        assert data["meta"]["date_from"] == "2026-09-28"
        assert data["meta"]["date_to"] == "2026-10-04"
        assert data["totals"]["seconds"] == 6300
        assert {s["key"] for s in data["sections"]} >= {"projects", "completed"}

    def test_time_ranges_are_bounded(self, client, db):
        owner, _member, ws, *_ = _team(client, db)
        _report(client, owner, ws, "timesheet", 400, date_from="2026-01-01",
                date_to="2026-12-31")
        _report(client, owner, ws, "timesheet", 400, date_from="2026-10-04",
                date_to="2026-10-01")


class TestVisibility:
    def test_under_own_a_member_sees_only_their_time_and_the_team_total(self, client, db):
        owner, member, ws, *_ = _team(client, db)
        _settings(client, owner, ws, "time-tracking", visibility="own")
        data = _report(client, member, ws, "timesheet", **WEEK)
        assert [r["_user_id"] for r in data["rows"]] == ["member-1"]
        assert data["totals"]["total"] == 2700
        assert data["meta"]["restricted_to_own"] is True
        assert data["meta"]["team_total_seconds"] == 6300
        entries = _report(client, member, ws, "time_entries", **WEEK)
        assert {r["user"] for r in entries["rows"]} == {"Test User"}
        assert len(entries["rows"]) == 2
        # Admins still see everyone.
        assert _report(client, owner, ws, "timesheet", **WEEK)["totals"]["total"] == 6300

    def test_the_cache_does_not_leak_one_persons_view_to_another(self, client, db):
        owner, member, ws, *_ = _team(client, db)
        _settings(client, owner, ws, "time-tracking", visibility="own")
        assert _report(client, owner, ws, "timesheet", **WEEK)["totals"]["total"] == 6300
        assert _report(client, member, ws, "timesheet", **WEEK)["totals"]["total"] == 2700

    def test_guests_run_reports_unless_switched_off(self, client, db):
        owner, _member, ws, *_ = _team(client, db)
        guest = pm.person(db, "guest-1")
        pm.join(db, ws, "guest-1", "guest")
        _report(client, guest, ws, "project_overview")
        _settings(client, owner, ws, guest_access=False)
        _report(client, guest, ws, "project_overview", 403)
        catalog = pm.ok(client.get(f"/workspaces/{ws}/reports/catalog", headers=guest))
        assert catalog["can_view"] is False and catalog["export_formats"] == []


class TestProjectReports:
    def test_overview_counts_by_status(self, client, db):
        owner, _member, ws, space, _list, a, _b = _team(client, db)
        pm.ok(client.post(f"/workspaces/{ws}/issues/{a['id']}/move",
                          json={"status_id": pm.status_id(space, "Done")}, headers=owner))
        data = _report(client, owner, ws, "project_overview", scope=f"space:{space['id']}")
        counts = {r["status"]: r["count"] for r in data["rows"]}
        assert counts == {"To Do": 1, "Done": 1}
        kpis = {k["key"]: k["value"] for k in data["kpis"]}
        assert kpis["done_pct"] == 50.0 and kpis["points_pct"] == 62.5

    def test_created_vs_resolved(self, client, db):
        owner, _member, ws, space, _list, a, _b = _team(client, db)
        pm.ok(client.post(f"/workspaces/{ws}/issues/{a['id']}/move",
                          json={"status_id": pm.status_id(space, "Done")}, headers=owner))
        data = _report(client, owner, ws, "created_vs_resolved", bucket="day")
        kpis = {k["key"]: k["value"] for k in data["kpis"]}
        assert kpis == {"created": 2, "resolved": 1, "net": 1}
        assert data["rows"][-1]["created"] == 2

    def test_workload_by_assignee(self, client, db):
        owner, _member, ws, *_ = _team(client, db)
        data = _report(client, owner, ws, "workload")
        by = {r["_user_id"]: r for r in data["rows"]}
        assert by["member-1"]["open"] == 1 and by["member-1"]["points"] == 5
        assert by[None]["open"] == 1 and by[None]["points"] == 3

    def test_epic_progress_rolls_up_children(self, client, db):
        owner, ws, space, board_list = pm.setup_board(client, db)
        epic = pm.issue(client, owner, ws, board_list["id"], title="Epic", type="epic",
                        assignee_id="owner-1", completion_criteria="Both stories are done.")
        child = pm.issue(client, owner, ws, board_list["id"], title="Story", type="story",
                         parent_id=epic["id"], story_points=8)
        pm.issue(client, owner, ws, board_list["id"], title="Story 2", type="story",
                 parent_id=epic["id"], story_points=2)
        pm.ok(client.post(f"/workspaces/{ws}/issues/{child['id']}/move",
                          json={"status_id": pm.status_id(space, "Done")}, headers=owner))
        data = _report(client, owner, ws, "epic_progress")
        row = next(r for r in data["rows"] if r["title"] == "Epic")
        assert row["issues"] == 2 and row["done"] == 1 and row["done_pct"] == 50.0
        assert row["done_points"] == 8 and row["points"] == 10 and row["points_pct"] == 80.0


class TestSprintReports:
    def _sprint(self, client, owner, ws, space, *issues):
        sprint = pm.ok(client.post(f"/workspaces/{ws}/spaces/{space['id']}/sprints",
                                   json={"name": "S1"}, headers=owner), 201)
        pm.ok(client.post(f"/workspaces/{ws}/sprints/{sprint['id']}/issues",
                          json={"issue_ids": [i["id"] for i in issues]}, headers=owner))
        pm.ok(client.post(f"/workspaces/{ws}/sprints/{sprint['id']}/start", json={},
                          headers=owner))
        return sprint

    @staticmethod
    def _started_an_hour_ago(db, sprint):
        """Changes made within a moment of the start count as part of it, so move the start
        (and the memberships it began with) back to make the later changes real."""
        row = db.get(Sprint, sprint["id"])
        row.started_at -= timedelta(hours=1)
        for member in db.query(SprintIssue).filter(SprintIssue.sprint_id == row.id):
            member.added_at -= timedelta(hours=1)
        db.commit()

    def test_sprint_report_sections(self, client, db):
        owner, ws, space, board_list = pm.setup_board(client, db)
        a = pm.issue(client, owner, ws, board_list["id"], title="A", story_points=5)
        b = pm.issue(client, owner, ws, board_list["id"], title="B", story_points=3)
        c = pm.issue(client, owner, ws, board_list["id"], title="C", story_points=2)
        late = pm.issue(client, owner, ws, board_list["id"], title="Late", story_points=1)
        sprint = self._sprint(client, owner, ws, space, a, b, c)
        self._started_an_hour_ago(db, sprint)
        base = f"/workspaces/{ws}/sprints/{sprint['id']}"
        pm.ok(client.post(f"/workspaces/{ws}/issues/{a['id']}/move",
                          json={"status_id": pm.status_id(space, "Done")}, headers=owner))
        pm.ok(client.request("DELETE", f"{base}/issues", json={"issue_ids": [c["id"]]},
                             headers=owner))
        pm.ok(client.post(f"{base}/issues", json={"issue_ids": [late["id"]]}, headers=owner))
        current = pm.ok(client.get(f"/workspaces/{ws}/issues/{b['id']}", headers=owner))
        pm.ok(client.patch(f"/workspaces/{ws}/issues/{b['id']}",
                           json={"version": current["version"], "story_points": 8},
                           headers=owner))

        data = pm.ok(client.get(f"{base}/report", headers=owner))
        sections = {s["key"]: [r["title"] for r in s["rows"]] for s in data["sections"]}
        assert sections["completed"] == ["A"]
        assert sorted(sections["not_completed"]) == ["B", "Late"]
        assert sections["removed"] == ["C"]
        assert sections["added"] == ["Late"]
        b_row = next(s for s in data["sections"] if s["key"] == "not_completed")["rows"]
        b_row = next(r for r in b_row if r["title"] == "B")
        assert b_row["points_change"] == "3 → 8"
        kpis = {k["key"]: k["value"] for k in data["kpis"]}
        assert kpis["committed"] == 10 and kpis["completed_points"] == 5

    def test_burndown_and_burnup(self, client, db):
        owner, ws, space, board_list = pm.setup_board(client, db)
        a = pm.issue(client, owner, ws, board_list["id"], title="A", story_points=5)
        b = pm.issue(client, owner, ws, board_list["id"], title="B", story_points=3)
        sprint = self._sprint(client, owner, ws, space, a, b)
        pm.ok(client.post(f"/workspaces/{ws}/issues/{a['id']}/move",
                          json={"status_id": pm.status_id(space, "Done")}, headers=owner))
        down = pm.ok(client.get(f"/workspaces/{ws}/sprints/{sprint['id']}/burndown",
                                headers=owner))
        kpis = {k["key"]: k["value"] for k in down["kpis"]}
        assert kpis["start"] == 8 and kpis["remaining"] == 3
        assert down["rows"][0]["remaining"] == 3
        assert down["rows"][-1]["guideline"] == 0
        count = pm.ok(client.get(f"/workspaces/{ws}/sprints/{sprint['id']}/burndown",
                                 params={"unit": "count"}, headers=owner))
        assert {k["key"]: k["value"] for k in count["kpis"]}["remaining"] == 1
        up = _report(client, owner, ws, "burnup", scope=f"sprint:{sprint['id']}")
        first = up["rows"][0]
        assert list(first.values())[1:4] == [8, 5, 3]

    def test_velocity_reads_frozen_numbers_of_closed_sprints(self, client, db):
        owner, ws, space, board_list = pm.setup_board(client, db)
        done = pm.status_id(space, "Done")
        for name, points in (("S1", (5, 3)), ("S2", (2, 2))):
            issues = [pm.issue(client, owner, ws, board_list["id"], title=f"{name}-{p}",
                               story_points=p) for p in points]
            sprint = pm.ok(client.post(f"/workspaces/{ws}/spaces/{space['id']}/sprints",
                                       json={"name": name}, headers=owner), 201)
            pm.ok(client.post(f"/workspaces/{ws}/sprints/{sprint['id']}/issues",
                              json={"issue_ids": [i["id"] for i in issues]}, headers=owner))
            pm.ok(client.post(f"/workspaces/{ws}/sprints/{sprint['id']}/start", json={},
                              headers=owner))
            pm.ok(client.post(f"/workspaces/{ws}/issues/{issues[0]['id']}/move",
                              json={"status_id": done}, headers=owner))
            pm.ok(client.post(f"/workspaces/{ws}/sprints/{sprint['id']}/close", json={},
                              headers=owner))
        data = pm.ok(client.get(f"/workspaces/{ws}/spaces/{space['id']}/velocity",
                                headers=owner))
        rows = {r["sprint"]: (r["committed"], r["completed"]) for r in data["rows"]}
        assert rows == {"S1": (8, 5), "S2": (4, 2)}
        kpis = {k["key"]: k["value"] for k in data["kpis"]}
        assert kpis["average"] == 3.5

    def test_sprint_reports_need_a_sprint(self, client, db):
        owner, _member, ws, *_ = _team(client, db)
        _report(client, owner, ws, "burndown", 422)


class TestFlowReports:
    def test_cfd_reads_snapshots_and_today_live(self, client, db):
        owner, ws, space, board_list = pm.setup_board(client, db)
        a = pm.issue(client, owner, ws, board_list["id"], title="A")
        pm.issue(client, owner, ws, board_list["id"], title="B")
        pm.ok(client.post(f"/workspaces/{ws}/issues/{a['id']}/move",
                          json={"status_id": pm.status_id(space, "Done")}, headers=owner))
        data = pm.ok(client.get(f"/workspaces/{ws}/spaces/{space['id']}/cfd",
                                params={"preset": "today"}, headers=owner))
        last = data["rows"][-1]
        assert last["total"] == 2
        done_key = "s" + pm.status_id(space, "Done")
        assert last[done_key] == 1

    def test_cycle_time(self, client, db):
        owner, ws, space, board_list = pm.setup_board(client, db)
        a = pm.issue(client, owner, ws, board_list["id"], title="A")
        pm.ok(client.post(f"/workspaces/{ws}/issues/{a['id']}/move",
                          json={"status_id": pm.status_id(space, "In Progress")},
                          headers=owner))
        pm.ok(client.post(f"/workspaces/{ws}/issues/{a['id']}/move",
                          json={"status_id": pm.status_id(space, "Done")}, headers=owner))
        data = pm.ok(client.get(f"/workspaces/{ws}/spaces/{space['id']}/cycle-time",
                                headers=owner))
        assert len(data["rows"]) == 1
        assert data["rows"][0]["cycle_days"] is not None
        assert data["chart"]["kind"] == "scatter"

    def test_snapshots_are_written_once_a_day_and_idempotent(self, client, db):
        owner, ws, _space, board_list = pm.setup_board(client, db)
        pm.issue(client, owner, ws, board_list["id"], title="A")
        today = utc_now().date()
        assert snapshot.write_day(db, ws, today, "UTC") == 1
        db.commit()
        assert snapshot.write_day(db, ws, today, "UTC") == 1
        db.commit()
        assert db.query(IssueDailyStatus).count() == 1
        assert snapshot.has_day(db, ws, today)
        written = report_worker.snapshot_yesterday()
        assert written == 1
        assert report_worker.snapshot_yesterday() == 0
        assert db.query(IssueSnapshotDay).count() == 2


class TestEveryReportRuns:
    @pytest.mark.parametrize("report_type", sorted(REPORTS))
    def test_runs_and_exports_in_every_format(self, client, db, report_type):
        owner, _member, ws, space, _list, a, b = _team(client, db)
        sprint = pm.ok(client.post(f"/workspaces/{ws}/spaces/{space['id']}/sprints",
                                   json={"name": "S1"}, headers=owner), 201)
        pm.ok(client.post(f"/workspaces/{ws}/sprints/{sprint['id']}/issues",
                          json={"issue_ids": [a["id"], b["id"]]}, headers=owner))
        pm.ok(client.post(f"/workspaces/{ws}/sprints/{sprint['id']}/start", json={},
                          headers=owner))
        params = {"scope": f"sprint:{sprint['id']}"}
        data = _report(client, owner, ws, report_type, **params)
        assert data["meta"]["type"] == report_type
        for fmt in ("csv", "xlsx", "pdf"):
            response = _export(client, owner, ws, report_type, fmt, params=params)
            assert response.status_code == 200, (fmt, response.text[:300])
            assert "attachment" in response.headers["content-disposition"]
            assert response.content


class TestCache:
    def test_a_repeat_is_cached_until_something_changes(self, client, db):
        owner, _member, ws, _space, _list, a, _b = _team(client, db)
        assert _report(client, owner, ws, "timesheet", **WEEK)["meta"]["cached"] is False
        assert _report(client, owner, ws, "timesheet", **WEEK)["meta"]["cached"] is True
        pm.log_time(client, owner, ws, a["id"], 60, work_date="2026-09-30")
        again = _report(client, owner, ws, "timesheet", **WEEK)
        assert again["meta"]["cached"] is False and again["totals"]["total"] == 6360


class TestLimits:
    def test_too_many_rows_says_export(self, client, db, monkeypatch):
        owner, _member, ws, *_ = _team(client, db)
        monkeypatch.setattr(engine, "MAX_SYNC_ROWS", 2)
        response = client.get(f"/workspaces/{ws}/reports/time_entries", params=WEEK,
                              headers=owner)
        assert response.status_code == 413
        assert response.json()["data"]["hint"] == "export"


class TestExports:
    def test_csv_content_and_audit(self, client, db):
        owner, _member, ws, _space, board_list, *_ = _team(client, db)
        pm.issue(client, owner, ws, board_list["id"], title="=HYPERLINK(\"http://x\")")
        response = _export(client, owner, ws, "time_entries", "csv", params=WEEK,
                           duration_format="hhmm")
        text = response.content.decode("utf-8-sig")
        assert response.headers["content-type"].startswith("text/csv")
        assert "1:00" in text and "0:30" in text and "0:15" in text
        assert "Time zone,UTC" in text
        assert db.query(ActivityLog).filter(ActivityLog.action == "EXPORT").count() == 1

    def test_formula_text_is_neutralised(self, client, db):
        owner, ws, _space, board_list = pm.setup_board(client, db)
        bad = pm.issue(client, owner, ws, board_list["id"], title="=HYPERLINK(\"http://x\")")
        pm.log_time(client, owner, ws, bad["id"], 600)
        text = _export(client, owner, ws, "workload", "csv").content.decode("utf-8-sig")
        assert "HYPERLINK" not in text  # workload lists people, not titles
        text = _export(client, owner, ws, "time_entries", "csv",
                       params=WEEK).content.decode("utf-8-sig")
        assert "'=HYPERLINK" in text

    def test_xlsx_and_pdf(self, client, db):
        import openpyxl

        owner, _member, ws, *_ = _team(client, db)
        xlsx = _export(client, owner, ws, "timesheet", "xlsx", params=WEEK)
        book = openpyxl.load_workbook(BytesIO(xlsx.content))
        assert book.sheetnames[0] == "Summary"
        pdf = _export(client, owner, ws, "timesheet", "pdf", params=WEEK)
        assert pdf.content.startswith(b"%PDF") and pdf.headers["content-type"] == \
            "application/pdf"

    def test_export_needs_the_minimum_role(self, client, db):
        owner, member, ws, *_ = _team(client, db)
        assert _export(client, member, ws, "timesheet", params=WEEK).status_code == 200
        _settings(client, owner, ws, export_min_role="admin")
        response = _export(client, member, ws, "timesheet", params=WEEK)
        assert response.status_code == 403
        catalog = pm.ok(client.get(f"/workspaces/{ws}/reports/catalog", headers=member))
        assert catalog["export_formats"] == []

    def test_background_export_is_a_job_the_worker_finishes(self, client, db):
        owner, member, ws, *_ = _team(client, db)
        queued = _export(client, owner, ws, "timesheet", "xlsx", params=WEEK, background=True)
        assert queued.status_code == 202, queued.text
        job = queued.json()["data"]
        assert job["status"] == "queued" and job["download_path"] is None

        assert report_worker.run_once() is True
        assert report_worker.run_once() is False
        done = pm.ok(client.get(f"/workspaces/{ws}/report-jobs/{job['id']}", headers=owner))
        assert done["status"] == "done" and done["row_count"] == 2
        assert done["download_path"].startswith(f"/workspaces/{ws}/report-jobs/{job['id']}")

        file = client.get(done["download_path"], headers=owner)
        assert file.status_code == 200 and file.content[:2] == b"PK"
        listed = pm.ok(client.get(f"/workspaces/{ws}/report-jobs", headers=owner))
        assert [j["id"] for j in listed] == [job["id"]]

        # Someone else: not their job, so not found — even holding the owner's link.
        assert client.get(done["download_path"], headers=member).status_code == 404
        assert pm.ok(client.get(f"/workspaces/{ws}/report-jobs", headers=member)) == []
        bad = client.get(f"/workspaces/{ws}/report-jobs/{job['id']}/download?token=1.x",
                         headers=owner)
        assert bad.status_code == 403

    def test_large_exports_become_jobs(self, client, db, monkeypatch):
        owner, _member, ws, *_ = _team(client, db)
        monkeypatch.setattr(jobs, "SYNC_EXPORT_ROWS", 1)
        response = _export(client, owner, ws, "time_entries", params=WEEK)
        assert response.status_code == 202

    def test_expired_files_are_gone(self, client, db):
        owner, _member, ws, *_ = _team(client, db)
        job = _export(client, owner, ws, "timesheet", params=WEEK,
                      background=True).json()["data"]
        report_worker.run_once()
        path = pm.ok(client.get(f"/workspaces/{ws}/report-jobs/{job['id']}",
                                headers=owner))["download_path"]
        row = db.get(ReportJob, job["id"])
        row.expires_at = utc_now() - timedelta(minutes=1)
        db.commit()
        assert jobs.cleanup(db) == (1, 0)
        assert client.get(path, headers=owner).status_code == 410

    def test_a_failing_job_retries_then_fails(self, client, db, monkeypatch):
        owner, _member, ws, *_ = _team(client, db)
        job = _export(client, owner, ws, "timesheet", params=WEEK,
                      background=True).json()["data"]

        def boom(*_args, **_kwargs):
            raise RuntimeError("disk on fire")

        monkeypatch.setattr(jobs, "render", boom)
        report_worker.run_once()
        row = db.get(ReportJob, job["id"])
        db.refresh(row)
        assert row.status == "queued" and row.attempts == 1
        assert row.available_at > utc_now()


class TestSavedReports:
    def test_crud_and_visibility(self, client, db):
        owner, member, ws, *_ = _team(client, db)
        mine = pm.ok(client.post(f"/workspaces/{ws}/saved-reports", json={
            "name": "Team week", "report_type": "timesheet",
            "params": {"preset": "last_week"}}, headers=member), 201)
        assert mine["params"] == {"preset": "last_week"} and mine["can_edit"] is True
        assert pm.ok(client.get(f"/workspaces/{ws}/saved-reports", headers=owner)) == []

        pm.ok(client.patch(f"/workspaces/{ws}/saved-reports/{mine['id']}",
                           json={"visibility": "workspace", "pinned": True}, headers=member))
        shared = pm.ok(client.get(f"/workspaces/{ws}/saved-reports", headers=owner))
        assert [s["name"] for s in shared] == ["Team week"]
        assert shared[0]["can_edit"] is True  # owner of the workspace is an admin

        other = pm.person(db, "member-2")
        pm.join(db, ws, "member-2", "member")
        listed = pm.ok(client.get(f"/workspaces/{ws}/saved-reports", headers=other))
        assert listed[0]["can_edit"] is False
        assert client.patch(f"/workspaces/{ws}/saved-reports/{mine['id']}",
                            json={"name": "Mine now"}, headers=other).status_code == 403
        assert client.delete(f"/workspaces/{ws}/saved-reports/{mine['id']}",
                             headers=other).status_code == 403
        pm.ok(client.delete(f"/workspaces/{ws}/saved-reports/{mine['id']}", headers=owner))
        assert client.get(f"/workspaces/{ws}/saved-reports/{mine['id']}",
                          headers=member).status_code == 404

    def test_unknown_type_and_bad_params_are_refused(self, client, db):
        owner, _member, ws, *_ = _team(client, db)
        assert client.post(f"/workspaces/{ws}/saved-reports", json={
            "name": "X", "report_type": "nope"}, headers=owner).status_code == 400
        assert client.post(f"/workspaces/{ws}/saved-reports", json={
            "name": "X", "report_type": "timesheet", "params": {"bogus": 1}},
            headers=owner).status_code == 422


class TestSchedules:
    def _saved(self, client, owner, ws):
        return pm.ok(client.post(f"/workspaces/{ws}/saved-reports", json={
            "name": "Weekly hours", "report_type": "timesheet",
            "params": {"date_from": "2026-09-28", "date_to": "2026-10-04"},
            "visibility": "workspace"}, headers=owner), 201)

    def test_members_cannot_schedule_by_default(self, client, db):
        owner, member, ws, *_ = _team(client, db)
        saved = self._saved(client, owner, ws)
        response = client.post(f"/workspaces/{ws}/saved-reports/{saved['id']}/schedules",
                               json={"recipients": ["member-1"]}, headers=member)
        assert response.status_code == 403

    def test_recipients_must_be_members(self, client, db):
        owner, _member, ws, *_ = _team(client, db)
        saved = self._saved(client, owner, ws)
        pm.person(db, "outsider")
        response = client.post(f"/workspaces/{ws}/saved-reports/{saved['id']}/schedules",
                               json={"recipients": ["outsider"]}, headers=owner)
        assert response.status_code == 400

    def test_a_due_schedule_is_rendered_and_emailed(self, client, db, monkeypatch):
        owner, member, ws, *_ = _team(client, db)
        sent = []
        monkeypatch.setattr(report_worker, "send_email",
                            lambda to, template, attachments=None: sent.append(
                                (to, template.subject, attachments)) or {"status": True})
        saved = self._saved(client, owner, ws)
        schedule = pm.ok(client.post(
            f"/workspaces/{ws}/saved-reports/{saved['id']}/schedules",
            json={"cadence": "weekly", "weekday": 0, "hour_local": 9,
                  "timezone": "Europe/London", "format": "csv",
                  "recipients": ["owner-1", "member-1"]}, headers=owner), 201)
        assert schedule["next_run_at"] and len(schedule["recipients"]) == 2

        row = db.get(ReportSchedule, schedule["id"])
        row.next_run_at = utc_now() - timedelta(minutes=1)
        db.commit()
        assert scheduler.run_due(db) == 1  # one job: visibility is `everyone`
        db.refresh(row)
        assert row.next_run_at > utc_now() and row.last_run_at is not None
        assert scheduler.run_due(db) == 0

        assert report_worker.run_once() is True
        assert len(sent) == 2
        assert all(attachments and attachments[0][0].endswith(".csv")
                   for _to, _subject, attachments in sent)
        assert "Weekly hours" in sent[0][1]
        # A recipient can open the job from the email link.
        listed = pm.ok(client.get(f"/workspaces/{ws}/report-jobs", headers=member))
        assert len(listed) == 1 and listed[0]["kind"] == "schedule"

    def test_under_own_visibility_each_recipient_gets_their_own_rendering(self, client, db):
        owner, _member, ws, *_ = _team(client, db)
        _settings(client, owner, ws, "time-tracking", visibility="own")
        saved = self._saved(client, owner, ws)
        schedule = pm.ok(client.post(
            f"/workspaces/{ws}/saved-reports/{saved['id']}/schedules",
            json={"recipients": ["owner-1", "member-1"]}, headers=owner), 201)
        queued = pm.ok(client.post(
            f"/workspaces/{ws}/saved-reports/{saved['id']}/schedules/{schedule['id']}/run",
            headers=owner), 202)
        assert len(queued["job_ids"]) == 2
        requested = {db.get(ReportJob, j).requested_by for j in queued["job_ids"]}
        assert requested == {"owner-1", "member-1"}

    def test_update_and_delete(self, client, db):
        owner, _member, ws, *_ = _team(client, db)
        saved = self._saved(client, owner, ws)
        base = f"/workspaces/{ws}/saved-reports/{saved['id']}/schedules"
        schedule = pm.ok(client.post(base, json={"recipients": ["owner-1"]},
                                     headers=owner), 201)
        changed = pm.ok(client.patch(f"{base}/{schedule['id']}",
                                     json={"cadence": "monthly", "day_of_month": 15},
                                     headers=owner))
        assert changed["cadence"] == "monthly" and changed["day_of_month"] == 15
        assert pm.ok(client.get(base, headers=owner))[0]["id"] == schedule["id"]
        pm.ok(client.delete(f"{base}/{schedule['id']}", headers=owner))
        assert pm.ok(client.get(base, headers=owner)) == []


class TestSettings:
    def test_admins_change_them_members_do_not(self, client, db):
        owner, member, ws, *_ = _team(client, db)
        defaults = pm.ok(client.get(f"/workspaces/{ws}/settings/reports", headers=member))
        assert defaults["timezone"] == "UTC" and defaults["guest_access"] is True
        assert client.patch(f"/workspaces/{ws}/settings/reports", json={"timezone": "UTC"},
                            headers=member).status_code == 403
        saved = _settings(client, owner, ws, timezone="Asia/Kolkata", working_days=[0, 1, 2])
        assert saved["timezone"] == "Asia/Kolkata" and saved["working_days"] == [0, 1, 2]
        assert client.patch(f"/workspaces/{ws}/settings/reports",
                            json={"timezone": "Mars/Base"}, headers=owner).status_code == 422
        assert client.patch(f"/workspaces/{ws}/settings/reports",
                            json={"working_days": [9]}, headers=owner).status_code == 422
        # The time zone moves day boundaries in reports.
        data = _report(client, owner, ws, "timesheet", **WEEK)
        assert data["meta"]["timezone"] == "Asia/Kolkata"


class TestQueryCounts:
    """The N+1 guard: a report costs the same number of queries for 2 issues as for 10, so its
    cost grows with the size of the answer in SQL, not in round trips. Measured through the
    export route, which never reads the cache."""

    TYPES = ("timesheet", "time_by_group", "time_entries", "estimate_vs_actual", "weekly",
             "project_overview", "created_vs_resolved", "workload", "epic_progress", "cfd",
             "cycle_time", "sprint_report", "burndown", "burnup", "velocity")

    def _count(self, client, headers, ws, report_type, scope):
        statements = []

        def listener(_conn, _cursor, statement, *_args):
            statements.append(statement)

        params = {"scope": scope, **WEEK}
        event.listen(db_session.engine, "before_cursor_execute", listener)
        try:
            response = _export(client, headers, ws, report_type, "csv", params=params)
        finally:
            event.remove(db_session.engine, "before_cursor_execute", listener)
        assert response.status_code == 200, (report_type, response.text)
        return len(statements)

    def test_queries_do_not_grow_with_issues_or_entries(self, client, db):
        owner, ws, space, board_list = pm.setup_board(client, db)
        epic = pm.issue(client, owner, ws, board_list["id"], title="Epic", type="epic",
                        assignee_id="owner-1", completion_criteria="Every issue is done.")

        def add(count):
            made = []
            for n in range(count):
                issue = pm.issue(client, owner, ws, board_list["id"], title=f"I{n}",
                                 parent_id=epic["id"], story_points=n + 1)
                pm.log_time(client, owner, ws, issue["id"], 600 * (n + 1))
                made.append(issue)
            done = pm.status_id(space, "Done")
            pm.ok(client.post(f"/workspaces/{ws}/issues/{made[0]['id']}/move",
                              json={"status_id": done}, headers=owner))
            return made

        first = add(2)
        sprint = pm.ok(client.post(f"/workspaces/{ws}/spaces/{space['id']}/sprints",
                                   json={"name": "S1"}, headers=owner), 201)
        pm.ok(client.post(f"/workspaces/{ws}/sprints/{sprint['id']}/issues",
                          json={"issue_ids": [i["id"] for i in first]}, headers=owner))
        pm.ok(client.post(f"/workspaces/{ws}/sprints/{sprint['id']}/start", json={},
                          headers=owner))
        scope = f"space:{space['id']}"
        small = {t: self._count(client, owner, ws, t, scope) for t in self.TYPES}
        more = add(8)
        pm.ok(client.post(f"/workspaces/{ws}/sprints/{sprint['id']}/issues",
                          json={"issue_ids": [i["id"] for i in more]}, headers=owner))
        large = {t: self._count(client, owner, ws, t, scope) for t in self.TYPES}
        grew = {t: (small[t], large[t]) for t in self.TYPES if large[t] > small[t]}
        assert not grew, f"queries grew with data: {grew}"
