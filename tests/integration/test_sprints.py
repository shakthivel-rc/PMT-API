"""Sprints, through the HTTP API: planning, the one-active rule, closing, permissions.

The numbers frozen here — `committed_points` at start and `completed_points` at close — are
what the velocity chart reads months later, so the tests check them exactly. The membership
rows (`sprint_issues`) and events are what the sprint report and burndown replay; the tests
check those are written alongside `issues.sprint_id`, never one without the other.
"""
import pytest

from models.activity_log import ActivityLog
from models.issue import IssueEvent
from models.sprint import SprintIssue
from tests import pm_fixtures as pm

pytestmark = pytest.mark.integration


def _sprint(client, headers, ws, space_id, name="Sprint 1", **fields):
    return pm.ok(client.post(f"/workspaces/{ws}/spaces/{space_id}/sprints",
                             json={"name": name, **fields}, headers=headers), 201)


def _add(client, headers, ws, sprint_id, *issue_ids, status=200):
    return pm.ok(client.post(f"/workspaces/{ws}/sprints/{sprint_id}/issues",
                             json={"issue_ids": list(issue_ids)}, headers=headers), status)


def _setup(client, db):
    owner, ws, space, board_list = pm.setup_board(client, db)
    member = pm.person(db, "member-1")
    pm.join(db, ws, "member-1", "member")
    a = pm.issue(client, owner, ws, board_list["id"], title="A", story_points=5)
    b = pm.issue(client, owner, ws, board_list["id"], title="B", story_points=3)
    return owner, member, ws, space, board_list, a, b


class TestPlanning:
    def test_create_add_and_list(self, client, db):
        _owner, member, ws, space, _list, a, b = _setup(client, db)
        sprint = _sprint(client, member, ws, space["id"], goal="Ship it",
                         starts_on="2026-10-05", ends_on="2026-10-16")
        assert sprint["state"] == "planned" and sprint["goal"] == "Ship it"

        change = _add(client, member, ws, sprint["id"], a["id"], b["id"], a["id"])
        assert change["changed"] == [a["id"], b["id"]]
        assert change["sprint"]["issue_count"] == 2 and change["sprint"]["points"] == 8

        again = _add(client, member, ws, sprint["id"], a["id"])
        assert again["changed"] == [] and again["unchanged"] == [a["id"]]

        cards = pm.ok(client.get(f"/workspaces/{ws}/sprints/{sprint['id']}/issues",
                                 headers=member))
        assert {c["id"] for c in cards} == {a["id"], b["id"]}
        assert all(c["sprint_id"] == sprint["id"] for c in cards)

        events = db.query(IssueEvent).filter(IssueEvent.event == "sprint_added").all()
        assert {e.issue_id for e in events} == {a["id"], b["id"]}
        assert db.query(SprintIssue).count() == 2

    def test_moving_an_issue_between_sprints_closes_its_old_membership(self, client, db):
        owner, _member, ws, space, _list, a, _b = _setup(client, db)
        one = _sprint(client, owner, ws, space["id"], "One")
        two = _sprint(client, owner, ws, space["id"], "Two")
        _add(client, owner, ws, one["id"], a["id"])
        _add(client, owner, ws, two["id"], a["id"])
        rows = db.query(SprintIssue).order_by(SprintIssue.added_at).all()
        assert [(r.sprint_id, r.removed_at is not None) for r in rows] == [
            (one["id"], True), (two["id"], False)]
        listed = pm.ok(client.get(f"/workspaces/{ws}/spaces/{space['id']}/sprints",
                                  headers=owner))
        assert {s["name"]: s["issue_count"] for s in listed} == {"One": 0, "Two": 1}

    def test_remove(self, client, db):
        owner, _member, ws, space, _list, a, _b = _setup(client, db)
        sprint = _sprint(client, owner, ws, space["id"])
        _add(client, owner, ws, sprint["id"], a["id"])
        out = pm.ok(client.request("DELETE", f"/workspaces/{ws}/sprints/{sprint['id']}/issues",
                                   json={"issue_ids": [a["id"]]}, headers=owner))
        assert out["changed"] == [a["id"]] and out["sprint"]["issue_count"] == 0

    def test_dates_must_be_in_order(self, client, db):
        owner, _member, ws, space, *_ = _setup(client, db)
        response = client.post(f"/workspaces/{ws}/spaces/{space['id']}/sprints",
                               json={"name": "X", "starts_on": "2026-10-10",
                                     "ends_on": "2026-10-01"}, headers=owner)
        assert response.status_code == 422

    def test_guests_read_but_do_not_plan(self, client, db):
        owner, _member, ws, space, *_ = _setup(client, db)
        guest = pm.person(db, "guest-1")
        pm.join(db, ws, "guest-1", "guest")
        _sprint(client, owner, ws, space["id"])
        assert client.get(f"/workspaces/{ws}/spaces/{space['id']}/sprints",
                          headers=guest).status_code == 200
        assert client.post(f"/workspaces/{ws}/spaces/{space['id']}/sprints",
                           json={"name": "Mine"}, headers=guest).status_code == 403


class TestStartAndClose:
    def test_full_cycle_freezes_commitment_and_completion(self, client, db):
        owner, _member, ws, space, _list, a, b = _setup(client, db)
        done = pm.status_id(space, "Done")
        sprint = _sprint(client, owner, ws, space["id"])
        _add(client, owner, ws, sprint["id"], a["id"], b["id"])
        started = pm.ok(client.post(f"/workspaces/{ws}/sprints/{sprint['id']}/start",
                                    json={}, headers=owner))
        assert started["state"] == "active" and started["committed_points"] == 8
        assert started["starts_on"] and started["ends_on"]

        pm.ok(client.post(f"/workspaces/{ws}/issues/{a['id']}/move",
                          json={"status_id": done}, headers=owner))
        nxt = _sprint(client, owner, ws, space["id"], "Sprint 2")
        closed = pm.ok(client.post(f"/workspaces/{ws}/sprints/{sprint['id']}/close",
                                   json={"carry_over": "sprint", "target_sprint_id": nxt["id"]},
                                   headers=owner))
        assert closed["completed_issues"] == 1 and closed["carried_over"] == 1
        assert closed["completed_points"] == 5
        assert closed["sprint"]["state"] == "closed"
        assert closed["sprint"]["committed_points"] == 8

        moved = pm.ok(client.get(f"/workspaces/{ws}/sprints/{nxt['id']}/issues",
                                 headers=owner))
        assert [c["id"] for c in moved] == [b["id"]]
        kept = pm.ok(client.get(f"/workspaces/{ws}/sprints/{sprint['id']}/issues",
                                headers=owner))
        assert [c["id"] for c in kept] == [a["id"]]
        audited = {row.details.split(" ")[0] for row in db.query(ActivityLog).filter(
            ActivityLog.entity_type == "sprint")}
        assert audited == {"Started", "Closed"}

    def test_one_active_sprint_per_project(self, client, db):
        owner, _member, ws, space, *_ = _setup(client, db)
        one = _sprint(client, owner, ws, space["id"], "One")
        two = _sprint(client, owner, ws, space["id"], "Two")
        pm.ok(client.post(f"/workspaces/{ws}/sprints/{one['id']}/start", json={},
                          headers=owner))
        refused = client.post(f"/workspaces/{ws}/sprints/{two['id']}/start", json={},
                              headers=owner)
        assert refused.status_code == 409
        assert refused.json()["data"]["active_sprint_id"] == one["id"]

    def test_state_rules(self, client, db):
        owner, _member, ws, space, *_ = _setup(client, db)
        sprint = _sprint(client, owner, ws, space["id"])
        base = f"/workspaces/{ws}/sprints/{sprint['id']}"
        assert client.post(f"{base}/close", json={}, headers=owner).status_code == 409
        pm.ok(client.post(f"{base}/start", json={}, headers=owner))
        assert client.post(f"{base}/start", json={}, headers=owner).status_code == 409
        assert client.delete(base, headers=owner).status_code == 409
        pm.ok(client.post(f"{base}/close", json={}, headers=owner))
        assert client.patch(base, json={"name": "Renamed"}, headers=owner).status_code == 409
        assert client.post(f"{base}/issues", json={"issue_ids": ["x"]},
                           headers=owner).status_code == 409

    def test_members_plan_but_only_admins_start_close_and_delete_by_default(self, client, db):
        owner, member, ws, space, *_ = _setup(client, db)
        sprint = _sprint(client, member, ws, space["id"])
        base = f"/workspaces/{ws}/sprints/{sprint['id']}"
        assert client.post(f"{base}/start", json={}, headers=member).status_code == 403
        assert client.delete(base, headers=member).status_code == 403
        pm.ok(client.patch(f"/workspaces/{ws}/settings/reports",
                           json={"sprint_manage_role": "member"}, headers=owner))
        pm.ok(client.post(f"{base}/start", json={}, headers=member))

    def test_delete_returns_issues_to_the_backlog(self, client, db):
        owner, _member, ws, space, _list, a, _b = _setup(client, db)
        sprint = _sprint(client, owner, ws, space["id"])
        _add(client, owner, ws, sprint["id"], a["id"])
        out = pm.ok(client.delete(f"/workspaces/{ws}/sprints/{sprint['id']}", headers=owner))
        assert out["issues_moved"] == 1
        assert client.get(f"/workspaces/{ws}/sprints/{sprint['id']}",
                          headers=owner).status_code == 404

    def test_carry_target_must_be_a_planned_sprint_of_the_same_project(self, client, db):
        owner, _member, ws, space, *_ = _setup(client, db)
        other_space = pm.space(client, owner, ws, "OPS")
        sprint = _sprint(client, owner, ws, space["id"])
        elsewhere = _sprint(client, owner, ws, other_space["id"])
        pm.ok(client.post(f"/workspaces/{ws}/sprints/{sprint['id']}/start", json={},
                          headers=owner))
        response = client.post(f"/workspaces/{ws}/sprints/{sprint['id']}/close",
                               json={"carry_over": "sprint",
                                     "target_sprint_id": elsewhere["id"]}, headers=owner)
        assert response.status_code == 404
        assert client.post(f"/workspaces/{ws}/sprints/{sprint['id']}/close",
                           json={"carry_over": "sprint"}, headers=owner).status_code == 422


class TestTenancy:
    def test_another_workspaces_sprint_and_issues_are_not_found(self, client, db):
        headers_a, ws_a, space_a, _list_a = pm.setup_board(client, db, "alice", "alpha", "ALP")
        headers_b, ws_b, space_b, list_b = pm.setup_board(client, db, "bob", "bravo", "BRA")
        theirs = _sprint(client, headers_b, ws_b, space_b["id"])
        their_issue = pm.issue(client, headers_b, ws_b, list_b["id"])
        mine = _sprint(client, headers_a, ws_a, space_a["id"])

        assert client.get(f"/workspaces/{ws_a}/sprints/{theirs['id']}",
                          headers=headers_a).status_code == 404
        assert client.post(f"/workspaces/{ws_a}/sprints/{theirs['id']}/start", json={},
                           headers=headers_a).status_code == 404
        assert client.get(f"/workspaces/{ws_a}/spaces/{space_b['id']}/sprints",
                          headers=headers_a).status_code == 404
        refused = client.post(f"/workspaces/{ws_a}/sprints/{mine['id']}/issues",
                              json={"issue_ids": [their_issue["id"]]}, headers=headers_a)
        assert refused.status_code == 404
        assert client.get(f"/workspaces/{ws_b}/sprints/{theirs['id']}",
                          headers=headers_a).status_code == 404

    def test_an_issue_of_another_project_cannot_join(self, client, db):
        owner, _member, ws, space, *_ = _setup(client, db)
        other_space = pm.space(client, owner, ws, "OPS")
        other_list = pm.task_list(client, owner, ws, other_space["id"])
        stranger = pm.issue(client, owner, ws, other_list["id"])
        sprint = _sprint(client, owner, ws, space["id"])
        response = client.post(f"/workspaces/{ws}/sprints/{sprint['id']}/issues",
                               json={"issue_ids": [stranger["id"]]}, headers=owner)
        assert response.status_code == 404
        assert response.json()["data"]["issue_id"] == stranger["id"]
