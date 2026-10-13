"""Time tracking, through the HTTP API: logging, permissions, visibility, roll-ups, timers.

Read the tenancy and visibility tests first. Every other test here would still pass if one
tenant could read another's hours, or if a member could read a colleague's timesheet in a
workspace that switched that off; those would not.
"""
from datetime import timedelta

import pytest
from sqlalchemy import event

import db.session as db_session
from models.activity_log import ActivityLog
from models.issue import IssueEvent
from models.time_tracking import ActiveTimer, TimeEntry
from models.user import User
from models.workspace import WorkspaceMember
from tests import pm_fixtures as pm
from utils.time_tracking import MAX_ENTRY_SECONDS, utc_now

pytestmark = pytest.mark.integration


def _team(client, db):
    """An owner, a member and a guest in one workspace with one list and one issue."""
    owner, ws, _space, board_list = pm.setup_board(client, db)
    member = pm.person(db, "member-1")
    pm.join(db, ws, "member-1", "member")
    guest = pm.person(db, "guest-1")
    pm.join(db, ws, "guest-1", "guest")
    issue = pm.issue(client, owner, ws, board_list["id"], title="Fix refunds")
    return owner, member, guest, ws, board_list, issue


def _settings(client, headers, ws, **changes):
    return pm.ok(client.patch(f"/workspaces/{ws}/settings/time-tracking", json=changes,
                              headers=headers))


class TestTenancy:
    def test_a_non_member_gets_404_for_every_time_path(self, client, db):
        """404, not 403, exactly like the rest of the workspace (CLAUDE.md §8)."""
        owner, _member, _guest, ws, _list, issue = _team(client, db)
        entry = pm.log_time(client, owner, ws, issue["id"])
        stranger = pm.person(db, "stranger")

        for method, path, body in (
            ("get", f"/workspaces/{ws}/issues/{issue['id']}/time-tracking", None),
            ("post", f"/workspaces/{ws}/issues/{issue['id']}/time-entries",
             {"duration_seconds": 60, "work_date": "2026-10-01"}),
            ("patch", f"/workspaces/{ws}/time-entries/{entry['id']}", {"note": "x"}),
            ("delete", f"/workspaces/{ws}/time-entries/{entry['id']}", None),
            ("get", f"/workspaces/{ws}/time-entries", None),
            ("get", f"/workspaces/{ws}/time-report?date_from=2026-10-01&date_to=2026-10-07",
             None),
            ("post", f"/workspaces/{ws}/issues/{issue['id']}/timer", {}),
            ("get", f"/workspaces/{ws}/settings/time-tracking", None),
        ):
            kwargs = {"headers": stranger}
            if body is not None:
                kwargs["json"] = body
            response = getattr(client, method)(path, **kwargs)
            assert response.status_code == 404, (method, path)
            assert response.json()["message"] == "Workspace not found"

    def test_ids_from_another_workspace_are_not_found_inside_yours(self, client, db):
        """Membership of the path's workspace passes; the tenant filter on the entry or
        issue lookup must still refuse the other tenant's id."""
        headers_a, ws_a, _, _list_a = pm.setup_board(client, db, "alice", "alpha", "ALP")
        headers_b, ws_b, _, list_b = pm.setup_board(client, db, "bob", "bravo", "BRA")
        theirs = pm.issue(client, headers_b, ws_b, list_b["id"])
        their_entry = pm.log_time(client, headers_b, ws_b, theirs["id"], note="secret")

        assert client.post(f"/workspaces/{ws_a}/issues/{theirs['id']}/time-entries",
                           json={"duration_seconds": 60, "work_date": "2026-10-01"},
                           headers=headers_a).status_code == 404
        assert client.patch(f"/workspaces/{ws_a}/time-entries/{their_entry['id']}",
                            json={"note": "mine"}, headers=headers_a).status_code == 404
        assert client.delete(f"/workspaces/{ws_a}/time-entries/{their_entry['id']}",
                             headers=headers_a).status_code == 404
        assert client.post(f"/workspaces/{ws_a}/issues/{theirs['id']}/timer", json={},
                           headers=headers_a).status_code == 404
        listed = pm.ok(client.get(f"/workspaces/{ws_a}/time-entries", headers=headers_a))
        assert listed == {"records": [], "total": 0, "total_seconds": 0}


class TestLogging:
    def test_logging_time_returns_the_entry_and_records_an_event(self, client, db):
        owner, _member, _guest, ws, _list, issue = _team(client, db)
        entry = pm.log_time(client, owner, ws, issue["id"], 5400, note="  Reproduced  ")

        assert entry["duration_seconds"] == 5400
        assert entry["work_date"] == "2026-10-01"
        assert entry["note"] == "Reproduced"
        assert entry["source"] == "manual"
        assert entry["user"]["id"] == entry["created_by"]["id"] == "owner-1"
        assert entry["issue"]["key"] == issue["key"]
        assert entry["issue"]["list_id"] == issue["list_id"]
        assert entry["can_edit"] is True
        [event_row] = db.query(IssueEvent).filter(IssueEvent.event == "time_logged").all()
        assert (event_row.issue_id, event_row.to_value) == (issue["id"], "5400")

    def test_an_offset_start_is_stored_as_utc_and_a_naive_one_is_refused(self, client, db):
        """PyMySQL and SQLite drop an offset silently; without the conversion 10:00 IST
        would be stored as 10:00 UTC, five and a half hours wrong."""
        owner, _member, _guest, ws, _list, issue = _team(client, db)
        entry = pm.log_time(client, owner, ws, issue["id"],
                            started_at="2026-10-01T10:00:00+05:30")
        assert entry["started_at"].startswith("2026-10-01T04:30:00")

        naive = client.post(f"/workspaces/{ws}/issues/{issue['id']}/time-entries",
                            json={"duration_seconds": 60, "work_date": "2026-10-01",
                                  "started_at": "2026-10-01T10:00:00"}, headers=owner)
        assert naive.status_code == 422

    @pytest.mark.parametrize("body, status", [
        ({"duration_seconds": 0, "work_date": "2026-10-01"}, 422),
        ({"duration_seconds": MAX_ENTRY_SECONDS + 1, "work_date": "2026-10-01"}, 422),
        ({"duration_seconds": 60}, 422),
        ({"duration_seconds": 60, "work_date": "2026-10-01", "note": "x" * 2001}, 422),
    ])
    def test_bad_values_are_refused(self, client, db, body, status):
        owner, _member, _guest, ws, _list, issue = _team(client, db)
        response = client.post(f"/workspaces/{ws}/issues/{issue['id']}/time-entries",
                               json=body, headers=owner)
        assert response.status_code == status
        assert db.query(TimeEntry).count() == 0

    def test_time_in_the_future_is_refused(self, client, db):
        owner, _member, _guest, ws, _list, issue = _team(client, db)
        future_day = (utc_now() + timedelta(days=2)).date().isoformat()
        response = client.post(f"/workspaces/{ws}/issues/{issue['id']}/time-entries",
                               json={"duration_seconds": 60, "work_date": future_day},
                               headers=owner)
        assert response.status_code == 422
        future_start = (utc_now() + timedelta(hours=2)).isoformat() + "+00:00"
        response = client.post(f"/workspaces/{ws}/issues/{issue['id']}/time-entries",
                               json={"duration_seconds": 60, "work_date": "2026-10-01",
                                     "started_at": future_start}, headers=owner)
        assert response.status_code == 422

    def test_logging_time_does_not_bump_the_issue_version(self, client, db):
        """If it did, everyone editing the issue while a colleague logs time gets a 409."""
        owner, _member, _guest, ws, _list, issue = _team(client, db)
        pm.log_time(client, owner, ws, issue["id"])
        after = pm.ok(client.get(f"/workspaces/{ws}/issues/{issue['id']}", headers=owner))
        assert after["version"] == issue["version"]
        assert after["time_spent_seconds"] == 3600

    def test_totals_reach_the_card_and_the_estimate_is_a_patchable_field(self, client, db):
        owner, member, _guest, ws, board_list, issue = _team(client, db)
        pm.log_time(client, owner, ws, issue["id"], 1800)
        pm.log_time(client, member, ws, issue["id"], 3600)
        updated = pm.ok(client.patch(f"/workspaces/{ws}/issues/{issue['id']}",
                                     json={"version": issue["version"],
                                           "time_estimate_seconds": 7200}, headers=owner))
        assert updated["time_estimate_seconds"] == 7200

        board = pm.board(client, owner, ws, board_list["id"])
        [card] = [c for column in board["columns"] for c in column["cards"]]
        assert (card["time_spent_seconds"], card["time_estimate_seconds"]) == (5400, 7200)
        summary = pm.issue_time(client, owner, ws, issue["id"])["summary"]
        assert summary["remaining_seconds"] == 1800
        assert summary["entry_count"] == 2
        assert [(u["user"]["id"], u["seconds"]) for u in summary["by_user"]] == [
            ("member-1", 3600), ("owner-1", 1800)]

    def test_an_estimate_can_be_given_at_creation(self, client, db):
        owner, _member, _guest, ws, board_list, _issue = _team(client, db)
        created = pm.issue(client, owner, ws, board_list["id"], time_estimate_seconds=3600)
        assert created["time_estimate_seconds"] == 3600
        assert created["time_spent_seconds"] == 0


class TestPermissions:
    def test_a_guest_reads_time_but_cannot_log_or_run_a_timer(self, client, db):
        owner, _member, guest, ws, _list, issue = _team(client, db)
        pm.log_time(client, owner, ws, issue["id"])
        panel = pm.issue_time(client, guest, ws, issue["id"])
        assert panel["summary"]["spent_seconds"] == 3600
        assert (panel["can_log"], panel["can_log_for_others"]) == (False, False)
        assert panel["entries"][0]["can_edit"] is False
        assert client.post(f"/workspaces/{ws}/issues/{issue['id']}/time-entries",
                           json={"duration_seconds": 60, "work_date": "2026-10-01"},
                           headers=guest).status_code == 403
        assert client.post(f"/workspaces/{ws}/issues/{issue['id']}/timer", json={},
                           headers=guest).status_code == 403

    def test_a_member_changes_only_their_own_time_and_an_admin_anyones(self, client, db):
        owner, member, _guest, ws, _list, issue = _team(client, db)
        mine = pm.log_time(client, member, ws, issue["id"], 600)
        theirs = pm.log_time(client, owner, ws, issue["id"], 900)

        assert client.patch(f"/workspaces/{ws}/time-entries/{theirs['id']}",
                            json={"duration_seconds": 60}, headers=member).status_code == 403
        assert client.delete(f"/workspaces/{ws}/time-entries/{theirs['id']}",
                             headers=member).status_code == 403
        edited = pm.ok(client.patch(f"/workspaces/{ws}/time-entries/{mine['id']}",
                                    json={"duration_seconds": 1200, "note": "longer"},
                                    headers=member))
        assert (edited["duration_seconds"], edited["note"]) == (1200, "longer")

        # The owner corrects the member's entry: allowed, and audited as an admin act.
        pm.ok(client.patch(f"/workspaces/{ws}/time-entries/{mine['id']}",
                           json={"note": None}, headers=owner))
        audit = db.query(ActivityLog).filter(ActivityLog.entity_type == "time_entry").all()
        assert [(a.action, a.entity_id) for a in audit] == [("UPDATE", mine["id"])]
        [updated] = db.query(IssueEvent).filter(IssueEvent.event == "time_updated",
                                                IssueEvent.actor_id == "member-1").all()
        assert (updated.from_value, updated.to_value) == ("600", "1200")

    def test_only_an_admin_logs_on_someone_elses_behalf(self, client, db):
        owner, member, _guest, ws, _list, issue = _team(client, db)
        for_member = pm.log_time(client, owner, ws, issue["id"], user_id="member-1")
        assert for_member["user"]["id"] == "member-1"
        assert for_member["created_by"]["id"] == "owner-1"
        assert db.query(ActivityLog).filter(ActivityLog.entity_type == "time_entry",
                                            ActivityLog.action == "CREATE").count() == 1

        refused = client.post(f"/workspaces/{ws}/issues/{issue['id']}/time-entries",
                              json={"duration_seconds": 60, "work_date": "2026-10-01",
                                    "user_id": "owner-1"}, headers=member)
        assert refused.status_code == 403
        for target in ("guest-1", "stranger", "nobody"):
            if target == "stranger":
                pm.person(db, "stranger")
            response = client.post(f"/workspaces/{ws}/issues/{issue['id']}/time-entries",
                                   json={"duration_seconds": 60, "work_date": "2026-10-01",
                                         "user_id": target}, headers=owner)
            assert response.status_code == 422, target

    def test_time_cannot_be_logged_for_a_deleted_account(self, client, db):
        """`services/workspace.is_member` accepts a soft-deleted account; this must not."""
        owner, _member, _guest, ws, _list, issue = _team(client, db)
        db.query(User).filter(User.id == "member-1").update({User.deleted: 1})
        db.commit()
        response = client.post(f"/workspaces/{ws}/issues/{issue['id']}/time-entries",
                               json={"duration_seconds": 60, "work_date": "2026-10-01",
                                     "user_id": "member-1"}, headers=owner)
        assert response.status_code == 422

    def test_deleting_an_entry_removes_it_from_every_total(self, client, db):
        owner, member, _guest, ws, _list, issue = _team(client, db)
        kept = pm.log_time(client, member, ws, issue["id"], 600)
        gone = pm.log_time(client, member, ws, issue["id"], 900)
        pm.ok(client.delete(f"/workspaces/{ws}/time-entries/{gone['id']}", headers=member))

        panel = pm.issue_time(client, owner, ws, issue["id"])
        assert panel["summary"]["spent_seconds"] == 600
        assert [e["id"] for e in panel["entries"]] == [kept["id"]]
        assert client.delete(f"/workspaces/{ws}/time-entries/{gone['id']}",
                             headers=member).status_code == 404
        [deleted] = db.query(IssueEvent).filter(IssueEvent.event == "time_deleted").all()
        assert deleted.from_value == "900"


class TestVisibility:
    def test_under_own_visibility_a_member_sees_totals_but_only_their_own_entries(
            self, client, db):
        """The privacy setting: issue totals stay public, people's lines do not."""
        owner, member, _guest, ws, _list, issue = _team(client, db)
        theirs = pm.log_time(client, owner, ws, issue["id"], 900)
        mine = pm.log_time(client, member, ws, issue["id"], 600)
        _settings(client, owner, ws, visibility="own")

        panel = pm.issue_time(client, member, ws, issue["id"])
        assert panel["summary"]["spent_seconds"] == 1500
        assert [u["user"]["id"] for u in panel["summary"]["by_user"]] == ["member-1"]
        assert [e["id"] for e in panel["entries"]] == [mine["id"]]
        assert panel["entries_total"] == 1

        listed = pm.ok(client.get(f"/workspaces/{ws}/time-entries", headers=member))
        assert [r["id"] for r in listed["records"]] == [mine["id"]]
        assert client.get(f"/workspaces/{ws}/time-entries?user_id=owner-1",
                          headers=member).status_code == 403
        assert client.get(f"/workspaces/{ws}/time-report?date_from=2026-10-01"
                          "&date_to=2026-10-01&user_id=owner-1",
                          headers=member).status_code == 403
        # Someone else's entry is not "yours to see", so it is not found at all.
        assert client.patch(f"/workspaces/{ws}/time-entries/{theirs['id']}",
                            json={"note": "x"}, headers=member).status_code == 404

        # Admins see everyone whatever the setting.
        everyone = pm.ok(client.get(f"/workspaces/{ws}/time-entries", headers=owner))
        assert everyone["total"] == 2

    def test_under_everyone_visibility_members_see_each_others_time(self, client, db):
        owner, member, _guest, ws, _list, issue = _team(client, db)
        pm.log_time(client, owner, ws, issue["id"], 900)
        listed = pm.ok(client.get(f"/workspaces/{ws}/time-entries?user_id=owner-1",
                                  headers=member))
        assert listed["total"] == 1
        assert listed["records"][0]["can_edit"] is False


class TestRollups:
    def test_an_epic_sums_time_and_estimates_from_every_level_beneath_it(self, client, db):
        owner, ws, _space, board_list = pm.setup_board(client, db)
        lid = board_list["id"]
        epic = pm.issue(client, owner, ws, lid, "Epic", type="epic", assignee_id="owner-1",
                        completion_criteria="Every level is logged.")
        story = pm.issue(client, owner, ws, lid, "Story", type="story", parent_id=epic["id"],
                         time_estimate_seconds=7200)
        task = pm.issue(client, owner, ws, lid, "Task", type="subtask", parent_id=story["id"],
                        time_estimate_seconds=3600)
        deleted = pm.issue(client, owner, ws, lid, "Gone", type="subtask",
                           parent_id=story["id"])
        pm.log_time(client, owner, ws, epic["id"], 60)
        pm.log_time(client, owner, ws, story["id"], 600)
        pm.log_time(client, owner, ws, task["id"], 1200)
        pm.log_time(client, owner, ws, deleted["id"], 5000)
        pm.ok(client.delete(f"/workspaces/{ws}/issues/{deleted['id']}", headers=owner))

        summary = pm.issue_time(client, owner, ws, epic["id"])["summary"]
        assert summary["spent_seconds"] == 60
        assert summary["rollup_spent_seconds"] == 60 + 600 + 1200
        assert summary["estimate_seconds"] is None
        assert summary["remaining_seconds"] is None
        assert summary["rollup_estimate_seconds"] == 7200 + 3600

        story_summary = pm.issue_time(client, owner, ws, story["id"])["summary"]
        assert story_summary["rollup_spent_seconds"] == 1800
        assert story_summary["remaining_seconds"] == 7200 - 600


class TestTimesheet:
    def test_the_list_pages_newest_first_and_totals_across_pages(self, client, db):
        owner, _member, _guest, ws, board_list, issue = _team(client, db)
        other = pm.issue(client, owner, ws, board_list["id"], title="Other")
        pm.log_time(client, owner, ws, issue["id"], 100, work_date="2026-09-29")
        pm.log_time(client, owner, ws, other["id"], 200, work_date="2026-09-30")
        pm.log_time(client, owner, ws, issue["id"], 300, work_date="2026-10-01")

        page = pm.ok(client.get(f"/workspaces/{ws}/time-entries?user_id=me&limit=2",
                                headers=owner))
        assert [r["work_date"] for r in page["records"]] == ["2026-10-01", "2026-09-30"]
        assert (page["total"], page["total_seconds"]) == (3, 600)
        ranged = pm.ok(client.get(f"/workspaces/{ws}/time-entries?date_from=2026-09-30"
                                  f"&date_to=2026-09-30", headers=owner))
        assert [r["duration_seconds"] for r in ranged["records"]] == [200]
        by_issue = pm.ok(client.get(f"/workspaces/{ws}/time-entries?issue_id={issue['id']}",
                                    headers=owner))
        assert by_issue["total_seconds"] == 400
        assert client.get(f"/workspaces/{ws}/time-entries?date_from=2026-10-02"
                          "&date_to=2026-10-01", headers=owner).status_code == 400

    def test_the_report_sums_by_person_issue_and_day_and_skips_deleted_issues(
            self, client, db):
        owner, member, _guest, ws, board_list, issue = _team(client, db)
        doomed = pm.issue(client, owner, ws, board_list["id"], title="Doomed")
        pm.log_time(client, owner, ws, issue["id"], 100, work_date="2026-10-01")
        pm.log_time(client, owner, ws, issue["id"], 200, work_date="2026-10-01")
        pm.log_time(client, member, ws, issue["id"], 400, work_date="2026-10-02")
        pm.log_time(client, owner, ws, doomed["id"], 999, work_date="2026-10-01")
        pm.log_time(client, owner, ws, issue["id"], 50, work_date="2026-09-01")
        pm.ok(client.delete(f"/workspaces/{ws}/issues/{doomed['id']}", headers=owner))

        report = pm.ok(client.get(f"/workspaces/{ws}/time-report?date_from=2026-10-01"
                                  "&date_to=2026-10-07", headers=owner))
        assert report["total_seconds"] == 700
        assert report["truncated"] is False
        assert [(r["user"]["id"], r["work_date"], r["seconds"]) for r in report["rows"]] == [
            ("owner-1", "2026-10-01", 300), ("member-1", "2026-10-02", 400)]
        mine = pm.ok(client.get(f"/workspaces/{ws}/time-report?date_from=2026-10-01"
                                "&date_to=2026-10-07&user_id=me", headers=member))
        assert mine["total_seconds"] == 400
        listed = pm.ok(client.get(f"/workspaces/{ws}/time-entries", headers=owner))
        assert all(r["issue"]["id"] != doomed["id"] for r in listed["records"])

    @pytest.mark.parametrize("query", ["date_from=2026-10-02&date_to=2026-10-01",
                                       "date_from=2026-01-01&date_to=2026-12-31"])
    def test_a_report_range_must_be_forwards_and_at_most_a_quarter(self, client, db, query):
        owner, _member, _guest, ws, _list, _issue = _team(client, db)
        assert client.get(f"/workspaces/{ws}/time-report?{query}",
                          headers=owner).status_code == 400


class TestTimer:
    def test_start_shows_in_the_header_and_stop_logs_the_time(self, client, db):
        _owner, member, _guest, ws, _list, issue = _team(client, db)
        assert pm.ok(client.get("/timer", headers=member)) is None

        started = pm.ok(client.post(f"/workspaces/{ws}/issues/{issue['id']}/timer",
                                    json={"work_date": "2026-10-01", "note": "pairing"},
                                    headers=member), 201)
        assert started["stopped_entry"] is None
        assert started["timer"]["issue"]["key"] == issue["key"]
        running = pm.ok(client.get("/timer", headers=member))
        assert running["issue"]["workspace_id"] == ws
        assert running["elapsed_seconds"] >= 0

        stopped = pm.ok(client.post("/timer/stop", json={}, headers=member))
        assert stopped["capped"] is False
        entry = stopped["entry"]
        assert (entry["source"], entry["note"], entry["work_date"]) == (
            "timer", "pairing", "2026-10-01")
        # A timer stopped at once still records the second it ran, rather than nothing.
        assert entry["duration_seconds"] >= 1
        assert pm.ok(client.get("/timer", headers=member)) is None
        # Stopping twice is harmless: a retried request must not be an error to explain.
        assert pm.ok(client.post("/timer/stop", json={}, headers=member))["entry"] is None

    def test_starting_another_timer_saves_the_first_and_restarting_one_is_a_no_op(
            self, client, db):
        owner, _member, _guest, ws, board_list, issue = _team(client, db)
        other = pm.issue(client, owner, ws, board_list["id"], title="Other")
        first = pm.ok(client.post(f"/workspaces/{ws}/issues/{issue['id']}/timer", json={},
                                  headers=owner), 201)
        again = pm.ok(client.post(f"/workspaces/{ws}/issues/{issue['id']}/timer", json={},
                                  headers=owner), 201)
        assert again["timer"]["started_at"] == first["timer"]["started_at"]
        assert again["stopped_entry"] is None
        assert db.query(TimeEntry).count() == 0

        switched = pm.ok(client.post(f"/workspaces/{ws}/issues/{other['id']}/timer",
                                     json={}, headers=owner), 201)
        assert switched["stopped_entry"]["issue"]["id"] == issue["id"]
        assert switched["timer"]["issue"]["id"] == other["id"]
        assert db.query(ActiveTimer).count() == 1

    def test_a_forgotten_timer_is_cut_to_one_day(self, client, db):
        owner, _member, _guest, ws, _list, issue = _team(client, db)
        pm.ok(client.post(f"/workspaces/{ws}/issues/{issue['id']}/timer", json={},
                          headers=owner), 201)
        db.query(ActiveTimer).update({ActiveTimer.started_at: utc_now() - timedelta(hours=30)})
        db.commit()
        stopped = pm.ok(client.post("/timer/stop", json={"note": "weekend"}, headers=owner))
        assert stopped["capped"] is True
        assert stopped["entry"]["duration_seconds"] == MAX_ENTRY_SECONDS
        assert stopped["entry"]["note"] == "weekend"

    def test_discard_logs_nothing(self, client, db):
        owner, _member, _guest, ws, _list, issue = _team(client, db)
        pm.ok(client.post(f"/workspaces/{ws}/issues/{issue['id']}/timer", json={},
                          headers=owner), 201)
        assert pm.ok(client.delete("/timer", headers=owner)) == {"discarded": True}
        assert pm.ok(client.delete("/timer", headers=owner)) == {"discarded": False}
        assert db.query(TimeEntry).count() == 0

    def test_leaving_the_workspace_takes_the_timer_with_it(self, client, db):
        _owner, member, _guest, ws, _list, issue = _team(client, db)
        pm.ok(client.post(f"/workspaces/{ws}/issues/{issue['id']}/timer", json={},
                          headers=member), 201)
        db.query(WorkspaceMember).filter(WorkspaceMember.user_id == "member-1").delete()
        db.commit()
        assert pm.ok(client.get("/timer", headers=member)) is None
        assert pm.ok(client.post("/timer/stop", json={}, headers=member))["entry"] is None
        assert db.query(TimeEntry).count() == 0
        assert db.query(ActiveTimer).count() == 0

    def test_a_timer_on_a_deleted_issue_is_discarded_on_stop(self, client, db):
        owner, _member, _guest, ws, _list, issue = _team(client, db)
        pm.ok(client.post(f"/workspaces/{ws}/issues/{issue['id']}/timer", json={},
                          headers=owner), 201)
        pm.ok(client.delete(f"/workspaces/{ws}/issues/{issue['id']}", headers=owner))
        assert pm.ok(client.post("/timer/stop", json={}, headers=owner))["entry"] is None


class TestSettings:
    def test_defaults_then_only_an_admin_changes_them(self, client, db):
        owner, member, guest, ws, _list, _issue = _team(client, db)
        defaults = pm.ok(client.get(f"/workspaces/{ws}/settings/time-tracking",
                                    headers=guest))
        assert defaults == {"enabled": True, "hours_per_day": 8.0, "days_per_week": 5,
                            "visibility": "everyone", "week_start": "monday",
                            "billable_default": True}
        assert client.patch(f"/workspaces/{ws}/settings/time-tracking",
                            json={"visibility": "own"}, headers=member).status_code == 403
        saved = _settings(client, owner, ws, hours_per_day=7.5, week_start="sunday")
        assert (saved["hours_per_day"], saved["week_start"], saved["visibility"]) == (
            7.5, "sunday", "everyone")
        assert client.patch(f"/workspaces/{ws}/settings/time-tracking",
                            json={"enabled": None}, headers=owner).status_code == 422
        assert client.patch(f"/workspaces/{ws}/settings/time-tracking",
                            json={"hours_per_day": 30}, headers=owner).status_code == 422
        assert db.query(ActivityLog).filter(
            ActivityLog.entity_type == "workspace_settings").count() == 1

    def test_switched_off_refuses_new_time_but_keeps_the_old_readable(self, client, db):
        owner, member, _guest, ws, _list, issue = _team(client, db)
        entry = pm.log_time(client, member, ws, issue["id"])
        _settings(client, owner, ws, enabled=False)

        assert client.post(f"/workspaces/{ws}/issues/{issue['id']}/time-entries",
                           json={"duration_seconds": 60, "work_date": "2026-10-01"},
                           headers=member).status_code == 422
        assert client.post(f"/workspaces/{ws}/issues/{issue['id']}/timer", json={},
                           headers=member).status_code == 422
        assert client.patch(f"/workspaces/{ws}/time-entries/{entry['id']}",
                            json={"note": "x"}, headers=member).status_code == 422
        panel = pm.issue_time(client, member, ws, issue["id"])
        assert panel["summary"]["spent_seconds"] == 3600
        assert panel["can_log"] is False


class _QueryCounter:
    def __init__(self):
        self.statements = []

    def __enter__(self):
        self._listener = lambda conn, cursor, statement, *a: self.statements.append(statement)
        event.listen(db_session.engine, "before_cursor_execute", self._listener)
        return self

    def __exit__(self, *exc):
        event.remove(db_session.engine, "before_cursor_execute", self._listener)


def test_time_totals_do_not_cost_a_query_per_card_or_per_entry(client, db):
    """The N+1 guard for the board's new SUM and for the work log's payloads."""
    owner, ws, _space, board_list = pm.setup_board(client, db)
    lid = board_list["id"]

    def measure():
        with _QueryCounter() as board_count:
            pm.board(client, owner, ws, lid)
        with _QueryCounter() as list_count:
            pm.ok(client.get(f"/workspaces/{ws}/time-entries", headers=owner))
        return len(board_count.statements), len(list_count.statements)

    for title in ("a", "b"):
        created = pm.issue(client, owner, ws, lid, title)
        pm.log_time(client, owner, ws, created["id"])
    small = measure()
    for title in ("c", "d", "e", "f", "g", "h"):
        created = pm.issue(client, owner, ws, lid, title)
        pm.log_time(client, owner, ws, created["id"])
        pm.log_time(client, owner, ws, created["id"], 60)
    large = measure()
    assert large[0] <= small[0], f"board queries grew with cards: {small[0]} -> {large[0]}"
    assert large[1] <= small[1], f"list queries grew with entries: {small[1]} -> {large[1]}"
