"""Editing and deleting workspaces, and changing and removing their members — through the API.

The four routes of 2026-10-06: `PATCH`/`DELETE /workspaces/{id}` and
`PATCH`/`DELETE /workspaces/{id}/members/{user_id}`. The role matrix itself is unit-tested
(tests/unit/test_membership_rules.py); what is here is everything that needs rows:

* **A deleted workspace is gone for everyone** — every route under it answers 404, nobody's
  list shows it, its slug stays taken, and nothing keeps running in it (a timer or a running
  migration plan refuses the delete; GitHub deliveries and report schedules stop on their
  own) — while its issues, time and history stay in the database.
* **A removal never leaves something broken behind it.** The last owner stays; a space owner,
  a running timer and a report schedule that runs as the person each refuse with a 409 that
  says what to change first; what they did stays as it was, and only their workflow-edit
  grants go with them.
"""
import dataclasses
import hashlib
import hmac
import json
import uuid
from datetime import timedelta

import pytest

import routes.integrations as webhook_route
from core.config import get_settings
from models.activity_log import ActivityLog
from models.hierarchy import Space, SpaceWorkflowEditor
from models.issue import Issue
from models.migration import MigrationPlan
from models.reporting import ReportSchedule
from models.time_tracking import ActiveTimer, TimeEntry
from models.user import User
from models.vcs import VcsLink
from models.workspace import Workspace, WorkspaceMember
from services.reports import scheduler
from tests import pm_fixtures as pm
from utils import membership_rules as rules
from utils.time_tracking import utc_now

pytestmark = pytest.mark.integration

LAST_OWNER = "A workspace needs at least one owner. Make someone else an owner first."


def _team(client, db):
    """owner-1's workspace `acme` (space PAY, one list), with an admin, a member, a guest and
    a signed-in stranger who belongs to none of it."""
    owner, ws, space, board_list = pm.setup_board(client, db)
    people = {"owner": owner}
    for role in ("admin", "member", "guest"):
        people[role] = pm.person(db, f"{role}-1")
        pm.join(db, ws, f"{role}-1", role)
    people["stranger"] = pm.person(db, "stranger")
    return people, ws, space, board_list


def _workspace(ws: str) -> str:
    return f"/workspaces/{ws}"


def _member(ws: str, user_id: str) -> str:
    return f"/workspaces/{ws}/members/{user_id}"


def _audit(db, entity_type: str, action: str | None = None) -> list[ActivityLog]:
    query = db.query(ActivityLog).filter(ActivityLog.entity_type == entity_type)
    if action is not None:
        query = query.filter(ActivityLog.action == action)
    return query.order_by(ActivityLog.created_at).all()


def _roles(client, headers, ws: str) -> dict[str, str]:
    return {m["user_id"]: m["role"]
            for m in pm.ok(client.get(f"{_workspace(ws)}/members", headers=headers))}


def _listed(client, headers) -> list[str]:
    return [w["id"] for w in pm.ok(client.get("/workspaces", headers=headers))]


def _start_timer(client, headers, ws: str, issue_id: str) -> None:
    pm.ok(client.post(f"/workspaces/{ws}/issues/{issue_id}/timer", json={}, headers=headers),
          201)


class TestUpdateWorkspace:
    def test_an_admin_renames_it_and_gives_it_another_address(self, client, db):
        people, ws, _space, _list = _team(client, db)
        updated = pm.ok(client.patch(_workspace(ws), json={"name": "Acme Platform",
                                                           "slug": "acme-platform"},
                                     headers=people["admin"]))
        assert updated == {"id": ws, "name": "Acme Platform", "slug": "acme-platform",
                           "role": "admin"}
        [listed] = pm.ok(client.get("/workspaces", headers=people["owner"]))
        assert (listed["name"], listed["slug"], listed["role"]) == ("Acme Platform",
                                                                    "acme-platform", "owner")
        [entry] = _audit(db, "workspace", "UPDATE")
        assert entry.user_id == "admin-1" and entry.entity_id == ws
        assert "name Acme -> Acme Platform" in entry.details
        assert "slug acme -> acme-platform" in entry.details

    def test_only_the_fields_sent_change_and_the_name_is_trimmed(self, client, db):
        people, ws, _space, _list = _team(client, db)
        updated = pm.ok(client.patch(_workspace(ws), json={"name": "  Renamed  "},
                                     headers=people["owner"]))
        assert (updated["name"], updated["slug"], updated["role"]) == ("Renamed", "acme",
                                                                       "owner")

    def test_members_and_guests_are_refused_and_strangers_are_not_told_it_exists(self, client,
                                                                                 db):
        people, ws, _space, _list = _team(client, db)
        for who in ("member", "guest"):
            response = client.patch(_workspace(ws), json={"name": "Mine"},
                                    headers=people[who])
            assert response.status_code == 403, who
        response = client.patch(_workspace(ws), json={"name": "Mine"},
                                headers=people["stranger"])
        assert response.status_code == 404
        assert response.json()["message"] == "Workspace not found"
        assert pm.ok(client.get(_workspace(ws), headers=people["owner"]))["name"] == "Acme"

    def test_a_slug_another_workspace_has_is_a_409_and_a_deleted_ones_too(self, client, db):
        """A deleted workspace keeps its address — the column is unique and a restore must
        not find it taken — and the message says so, since nothing visible holds it."""
        people, ws, _space, _list = _team(client, db)
        pm.workspace(client, people["owner"], "bravo")
        gone = pm.workspace(client, people["owner"], "charlie")
        pm.ok(client.delete(_workspace(gone), headers=people["owner"]))

        for slug in ("bravo", "charlie"):
            response = client.patch(_workspace(ws), json={"slug": slug},
                                    headers=people["owner"])
            assert response.status_code == 409, slug
            assert "A deleted workspace keeps its address" in response.json()["message"]
        # Creating one under the deleted workspace's address is refused the same way.
        response = client.post("/workspaces", json={"name": "C", "slug": "charlie"},
                               headers=people["owner"])
        assert response.status_code == 409
        assert "A deleted workspace keeps its address" in response.json()["message"]
        assert pm.ok(client.get(_workspace(ws), headers=people["owner"]))["slug"] == "acme"

    def test_its_own_values_are_not_a_conflict_and_not_audited(self, client, db):
        people, ws, _space, _list = _team(client, db)
        unchanged = pm.ok(client.patch(_workspace(ws), json={"name": "Acme", "slug": "acme"},
                                       headers=people["owner"]))
        assert (unchanged["name"], unchanged["slug"]) == ("Acme", "acme")
        assert _audit(db, "workspace", "UPDATE") == []

    @pytest.mark.parametrize("body", [{}, {"name": ""}, {"name": "   "}, {"name": None},
                                      {"slug": None}, {"slug": "Not A Slug"}, {"slug": "a"},
                                      {"name": "Acme", "colour": "teal"}])
    def test_a_body_that_cannot_be_applied_is_a_422(self, client, db, body):
        people, ws, _space, _list = _team(client, db)
        response = client.patch(_workspace(ws), json=body, headers=people["owner"])
        assert response.status_code == 422, body
        assert response.json()["data"]["errors"]
        tree = pm.ok(client.get(_workspace(ws), headers=people["owner"]))
        assert (tree["name"], tree["slug"]) == ("Acme", "acme")


class TestDeleteWorkspace:
    def test_an_owner_deletes_it_and_it_disappears_for_everyone(self, client, db):
        people, ws, _space, board_list = _team(client, db)
        issue = pm.issue(client, people["owner"], ws, board_list["id"])
        deleted = client.delete(_workspace(ws), headers=people["owner"])
        assert deleted.status_code == 200, deleted.text
        assert deleted.json()["data"] == {"id": ws}

        for who in ("owner", "admin", "member", "guest"):
            assert ws not in _listed(client, people[who]), who
            for path in (_workspace(ws), f"{_workspace(ws)}/members",
                         f"{_workspace(ws)}/issues", f"{_workspace(ws)}/issues/{issue['id']}",
                         f"{_workspace(ws)}/time-entries"):
                response = client.get(path, headers=people[who])
                assert response.status_code == 404, (who, path)
                assert response.json()["message"] == "Workspace not found"
        # Nor can it be changed, deleted again, or have its members changed.
        owner = people["owner"]
        assert client.patch(_workspace(ws), json={"name": "Back"},
                            headers=owner).status_code == 404
        assert client.delete(_workspace(ws), headers=owner).status_code == 404
        assert client.patch(_member(ws, "member-1"), json={"role": "guest"},
                            headers=owner).status_code == 404
        assert client.delete(_member(ws, "member-1"), headers=owner).status_code == 404

    def test_its_work_and_history_stay_in_the_database(self, client, db):
        """A soft delete: the rows are where they were, so a restore is two columns."""
        people, ws, _space, board_list = _team(client, db)
        issue = pm.issue(client, people["member"], ws, board_list["id"])
        pm.log_time(client, people["member"], ws, issue["id"])
        pm.ok(client.delete(_workspace(ws), headers=people["owner"]))

        deleted, deleted_at = (db.query(Workspace.deleted, Workspace.deleted_at)
                               .filter(Workspace.id == ws).one())
        assert deleted == 1 and deleted_at is not None
        assert db.query(Issue.deleted).filter(Issue.id == issue["id"]).scalar() == 0
        assert db.query(TimeEntry).filter(TimeEntry.workspace_id == ws).count() == 1
        assert db.query(WorkspaceMember).filter(WorkspaceMember.workspace_id == ws).count() == 4
        [entry] = _audit(db, "workspace", "DELETE")
        assert entry.user_id == "owner-1" and entry.entity_id == ws
        assert "acme" in entry.details

    def test_only_an_owner_deletes_it(self, client, db):
        people, ws, _space, _list = _team(client, db)
        for who in ("admin", "member", "guest"):
            response = client.delete(_workspace(ws), headers=people[who])
            assert response.status_code == 403, who
            assert response.json()["message"] == "Only an owner can delete this workspace."
        response = client.delete(_workspace(ws), headers=people["stranger"])
        assert response.status_code == 404
        assert ws in _listed(client, people["owner"])
        assert _audit(db, "workspace", "DELETE") == []

    def test_a_running_timer_refuses_it_naming_the_issue(self, client, db):
        """Deleted, the timer's owner could not see it and stopping it would discard the
        time — the reason archiving a space refuses too."""
        people, ws, _space, board_list = _team(client, db)
        issue = pm.issue(client, people["member"], ws, board_list["id"])
        _start_timer(client, people["member"], ws, issue["id"])

        refused = client.delete(_workspace(ws), headers=people["owner"])
        assert refused.status_code == 409
        assert refused.json()["data"]["issue_keys"] == [issue["key"]]
        assert issue["key"] in refused.json()["message"]
        assert ws in _listed(client, people["owner"])

        pm.ok(client.post("/timer/stop", json={}, headers=people["member"]))
        pm.ok(client.delete(_workspace(ws), headers=people["owner"]))

    def test_a_running_migration_plan_refuses_it_and_a_failed_one_does_not(self, client, db):
        """A running plan's runner writes into the workspace wave by wave; a failed one runs
        nothing and only waits to be repaired or rolled back."""
        people, ws, _space, _list = _team(client, db)
        plan = MigrationPlan(workspace_id=ws, name="Wave 1", status="running", operations=[],
                             created_by="owner-1")
        db.add(plan)
        db.commit()
        refused = client.delete(_workspace(ws), headers=people["owner"])
        assert refused.status_code == 409
        assert refused.json()["data"]["plan_id"] == plan.id
        assert "Wave 1" in refused.json()["message"]

        plan.status = "failed"
        db.commit()
        pm.ok(client.delete(_workspace(ws), headers=people["owner"]))

    def test_github_deliveries_for_its_repository_are_no_longer_applied(self, client, db,
                                                                          monkeypatch):
        """The repository stays linked to it — one repository feeds one workspace, and a
        restore must find the link — but a merge must not keep moving its issues unseen."""
        configured = dataclasses.replace(get_settings(), github_webhook_secret="whsec-test")
        monkeypatch.setattr(webhook_route, "get_settings", lambda: configured)
        people, ws, _space, board_list = _team(client, db)
        issue = pm.issue(client, people["owner"], ws, board_list["id"], title="Refunds")
        pm.ok(client.post(f"{_workspace(ws)}/repositories", json={"full_name": "acme/pay"},
                          headers=people["owner"]), 201)
        pm.ok(client.delete(_workspace(ws), headers=people["owner"]))

        payload = {"action": "closed", "repository": {"full_name": "acme/pay"},
                   "pull_request": {"number": 7, "title": f"{issue['key']} refunds",
                                    "body": "", "state": "closed", "merged": True,
                                    "html_url": "https://github.com/acme/pay/pull/7",
                                    "head": {"ref": "feature/refunds"},
                                    "user": {"login": "octocat"}}}
        body = json.dumps(payload).encode()
        digest = hmac.new(b"whsec-test", body, hashlib.sha256).hexdigest()
        response = client.post("/integrations/github/webhook", content=body, headers={
            "Content-Type": "application/json", "X-GitHub-Event": "pull_request",
            "X-GitHub-Delivery": str(uuid.uuid4()), "X-Hub-Signature-256": f"sha256={digest}"})
        result = pm.ok(response)
        assert result["handled"] is False and result["linked"] == []
        assert "deleted" in result["note"]
        assert db.query(VcsLink).count() == 0
        status_id = db.query(Issue.status_id).filter(Issue.id == issue["id"]).scalar()
        assert status_id == issue["status_id"]

    def test_its_report_schedules_send_nothing(self, client, db):
        """Why a schedule does not refuse the delete: the scheduler sends only to members of
        a live workspace, so a deleted one's schedules fall silent by themselves."""
        people, ws, _space, _list = _team(client, db)
        saved = pm.ok(client.post(f"{_workspace(ws)}/saved-reports", json={
            "name": "Weekly hours", "report_type": "timesheet", "visibility": "workspace",
            "params": {"date_from": "2026-09-28", "date_to": "2026-10-04"}},
            headers=people["owner"]), 201)
        schedule = pm.ok(client.post(
            f"{_workspace(ws)}/saved-reports/{saved['id']}/schedules",
            json={"recipients": ["owner-1", "member-1"]}, headers=people["owner"]), 201)
        pm.ok(client.delete(_workspace(ws), headers=people["owner"]))

        row = db.get(ReportSchedule, schedule["id"])
        row.next_run_at = utc_now() - timedelta(minutes=1)
        db.commit()
        assert scheduler.run_due(db) == 0


class TestUpdateMember:
    def test_an_admin_changes_a_members_role(self, client, db):
        people, ws, _space, board_list = _team(client, db)
        updated = pm.ok(client.patch(_member(ws, "member-1"), json={"role": "guest"},
                                     headers=people["admin"]))
        assert updated == {"user_id": "member-1", "email": "member-1@example.com",
                           "first_name": "Test", "last_name": "User", "role": "guest"}
        assert _roles(client, people["owner"], ws)["member-1"] == "guest"
        [entry] = _audit(db, "workspace_member", "UPDATE")
        assert entry.user_id == "admin-1" and entry.entity_id == "member-1"
        assert "member -> guest" in entry.details
        # The new role is the one the tenancy gate now applies.
        assert client.post(f"{_workspace(ws)}/issues", json={"list_id": board_list["id"],
                                                              "title": "x"},
                           headers=people["member"]).status_code == 403

    def test_the_role_they_already_have_is_a_200_with_no_audit_row(self, client, db):
        people, ws, _space, _list = _team(client, db)
        same = pm.ok(client.patch(_member(ws, "member-1"), json={"role": "member"},
                                  headers=people["admin"]))
        assert same["role"] == "member"
        # An admin naming an owner's own role neither makes nor changes an owner.
        assert pm.ok(client.patch(_member(ws, "owner-1"), json={"role": "owner"},
                                  headers=people["admin"]))["role"] == "owner"
        assert _audit(db, "workspace_member", "UPDATE") == []

    def test_only_an_owner_makes_owners_or_changes_an_owners_role(self, client, db):
        people, ws, _space, _list = _team(client, db)
        for target in ("member-1", "admin-1"):  # admin-1 includes promoting themselves
            refused = client.patch(_member(ws, target), json={"role": "owner"},
                                   headers=people["admin"])
            assert refused.status_code == 403, target
            assert refused.json()["message"] == rules.MAKE_OWNER
        refused = client.patch(_member(ws, "owner-1"), json={"role": "admin"},
                               headers=people["admin"])
        assert refused.status_code == 403
        assert refused.json()["message"] == rules.CHANGE_OWNER

        assert pm.ok(client.patch(_member(ws, "admin-1"), json={"role": "owner"},
                                  headers=people["owner"]))["role"] == "owner"
        assert pm.ok(client.patch(_member(ws, "admin-1"), json={"role": "member"},
                                  headers=people["owner"]))["role"] == "member"
        assert _roles(client, people["owner"], ws) == {
            "owner-1": "owner", "admin-1": "member", "member-1": "member", "guest-1": "guest"}

    def test_the_last_owner_cannot_be_demoted(self, client, db):
        people, ws, _space, _list = _team(client, db)
        refused = client.patch(_member(ws, "owner-1"), json={"role": "admin"},
                               headers=people["owner"])
        assert refused.status_code == 409
        assert refused.json()["message"] == LAST_OWNER
        assert _roles(client, people["owner"], ws)["owner-1"] == "owner"

        pm.ok(client.patch(_member(ws, "admin-1"), json={"role": "owner"},
                           headers=people["owner"]))
        assert pm.ok(client.patch(_member(ws, "owner-1"), json={"role": "admin"},
                                  headers=people["owner"]))["role"] == "admin"

    def test_an_owner_whose_account_was_deleted_does_not_count(self, client, db):
        """A deleted account cannot sign in, so it cannot use the role: with it as the only
        other owner, the live one is still the last. And it is not a member to change — the
        member list does not show it either."""
        people, ws, _space, _list = _team(client, db)
        pm.ok(client.patch(_member(ws, "admin-1"), json={"role": "owner"},
                           headers=people["owner"]))
        db.query(User).filter(User.id == "admin-1").update({"deleted": 1})
        db.commit()

        refused = client.patch(_member(ws, "owner-1"), json={"role": "admin"},
                               headers=people["owner"])
        assert refused.status_code == 409
        assert refused.json()["message"] == LAST_OWNER
        assert client.patch(_member(ws, "admin-1"), json={"role": "member"},
                            headers=people["owner"]).status_code == 404

    def test_members_and_guests_cannot_change_roles(self, client, db):
        people, ws, _space, _list = _team(client, db)
        for who, target in (("member", "guest-1"), ("guest", "member-1"),
                            ("member", "member-1")):
            response = client.patch(_member(ws, target), json={"role": "admin"},
                                    headers=people[who])
            assert response.status_code == 403, (who, target)
        assert _audit(db, "workspace_member", "UPDATE") == []

    def test_a_non_member_is_404(self, client, db):
        people, ws, _space, _list = _team(client, db)
        for target in ("stranger", "no-such-account"):
            response = client.patch(_member(ws, target), json={"role": "member"},
                                    headers=people["owner"])
            assert response.status_code == 404, target
            assert response.json()["message"] == "That person is not a member of this workspace."
        # A stranger is told nothing about the workspace, as on every route under it.
        response = client.patch(_member(ws, "member-1"), json={"role": "guest"},
                                headers=people["stranger"])
        assert response.status_code == 404
        assert response.json()["message"] == "Workspace not found"

    @pytest.mark.parametrize("body", [{}, {"role": "boss"}, {"role": None},
                                      {"role": "admin", "until": "2027-01-01"}])
    def test_a_body_without_one_known_role_is_a_422(self, client, db, body):
        people, ws, _space, _list = _team(client, db)
        response = client.patch(_member(ws, "member-1"), json=body, headers=people["owner"])
        assert response.status_code == 422, body
        assert _roles(client, people["owner"], ws)["member-1"] == "member"


class TestRemoveMember:
    def test_a_member_leaves_and_loses_access(self, client, db):
        people, ws, _space, _list = _team(client, db)
        left = client.delete(_member(ws, "member-1"), headers=people["member"])
        assert left.status_code == 200, left.text
        assert left.json()["data"] == {"user_id": "member-1"}
        assert left.json()["message"] == "You left the workspace"

        assert client.get(_workspace(ws), headers=people["member"]).status_code == 404
        assert _listed(client, people["member"]) == []
        assert "member-1" not in _roles(client, people["owner"], ws)
        [entry] = _audit(db, "workspace_member", "DELETE")
        assert entry.user_id == "member-1" and entry.entity_id == "member-1"
        assert entry.details.startswith(f"Left workspace {ws}")

    @pytest.mark.parametrize("who", ["guest", "admin"])
    def test_a_guest_and_an_admin_may_leave_too(self, client, db, who):
        people, ws, _space, _list = _team(client, db)
        pm.ok(client.delete(_member(ws, f"{who}-1"), headers=people[who]))
        assert f"{who}-1" not in _roles(client, people["owner"], ws)

    def test_an_admin_removes_someone(self, client, db):
        people, ws, _space, _list = _team(client, db)
        removed = pm.ok(client.delete(_member(ws, "member-1"), headers=people["admin"]))
        assert removed == {"user_id": "member-1"}
        assert client.get(_workspace(ws), headers=people["member"]).status_code == 404
        [entry] = _audit(db, "workspace_member", "DELETE")
        assert entry.user_id == "admin-1" and entry.entity_id == "member-1"
        assert entry.details.startswith(f"Removed from workspace {ws} (was member)")

    def test_removing_someone_else_needs_admin_or_owner(self, client, db):
        people, ws, _space, _list = _team(client, db)
        for who, target in (("member", "guest-1"), ("guest", "member-1")):
            refused = client.delete(_member(ws, target), headers=people[who])
            assert refused.status_code == 403, (who, target)
            assert refused.json()["message"] == rules.REMOVE_SOMEONE_ELSE
        assert set(_roles(client, people["owner"], ws)) == {"owner-1", "admin-1", "member-1",
                                                            "guest-1"}

    def test_only_an_owner_removes_an_owner(self, client, db):
        people, ws, _space, _list = _team(client, db)
        refused = client.delete(_member(ws, "owner-1"), headers=people["admin"])
        assert refused.status_code == 403
        assert refused.json()["message"] == rules.REMOVE_OWNER

        pm.ok(client.patch(_member(ws, "admin-1"), json={"role": "owner"},
                           headers=people["owner"]))
        pm.ok(client.delete(_member(ws, "admin-1"), headers=people["owner"]))
        assert _roles(client, people["owner"], ws) == {"owner-1": "owner",
                                                       "member-1": "member",
                                                       "guest-1": "guest"}

    def test_the_last_owner_cannot_leave(self, client, db):
        people, ws, _space, _list = _team(client, db)
        refused = client.delete(_member(ws, "owner-1"), headers=people["owner"])
        assert refused.status_code == 409
        assert refused.json()["message"] == LAST_OWNER
        assert ws in _listed(client, people["owner"])

        pm.ok(client.patch(_member(ws, "admin-1"), json={"role": "owner"},
                           headers=people["owner"]))
        pm.ok(client.delete(_member(ws, "owner-1"), headers=people["owner"]))
        assert _listed(client, people["owner"]) == []
        assert _roles(client, people["admin"], ws)["admin-1"] == "owner"

    def test_a_non_member_is_404(self, client, db):
        people, ws, _space, _list = _team(client, db)
        for target in ("stranger", "no-such-account"):
            response = client.delete(_member(ws, target), headers=people["owner"])
            assert response.status_code == 404, target
            assert response.json()["message"] == "That person is not a member of this workspace."
        for target in ("member-1", "stranger"):  # removing someone, or "leaving"
            response = client.delete(_member(ws, target), headers=people["stranger"])
            assert response.status_code == 404, target
            assert response.json()["message"] == "Workspace not found"
        assert _audit(db, "workspace_member", "DELETE") == []

    def test_a_space_owner_stays_until_the_space_has_another_owner(self, client, db):
        """A space's owner must be a member of its workspace, and the charter may require
        one, so the space needs another owner rather than none."""
        people, ws, space, _list = _team(client, db)
        pm.ok(client.patch(f"{_workspace(ws)}/spaces/{space['id']}",
                           json={"owner_id": "member-1"}, headers=people["owner"]))

        refused = client.delete(_member(ws, "member-1"), headers=people["admin"])
        assert refused.status_code == 409
        assert refused.json()["data"]["spaces"] == [{"id": space["id"], "key": "PAY",
                                                     "name": space["name"]}]
        message = refused.json()["message"]
        assert message.startswith("Test User cannot be removed from this workspace yet.")
        assert "PAY" in message and "another owner" in message
        leaving = client.delete(_member(ws, "member-1"), headers=people["member"])
        assert leaving.status_code == 409
        assert leaving.json()["message"].startswith("You cannot leave this workspace yet. "
                                                    "You own the space PAY")

        pm.ok(client.patch(f"{_workspace(ws)}/spaces/{space['id']}",
                           json={"owner_id": "admin-1"}, headers=people["owner"]))
        pm.ok(client.delete(_member(ws, "member-1"), headers=people["admin"]))

    def test_an_archived_space_they_own_keeps_them_as_its_record(self, client, db):
        """Archived is read-only for good: its owner is the record of who was accountable,
        and changing it would mean unarchiving and archiving again."""
        people, ws, space, _list = _team(client, db)
        pm.ok(client.patch(f"{_workspace(ws)}/spaces/{space['id']}",
                           json={"owner_id": "member-1"}, headers=people["owner"]))
        pm.ok(client.post(f"{_workspace(ws)}/spaces/{space['id']}/archive",
                          headers=people["owner"]))
        pm.ok(client.delete(_member(ws, "member-1"), headers=people["admin"]))
        assert db.query(Space.owner_id).filter(Space.id == space["id"]).scalar() == "member-1"

    def test_a_running_timer_holds_them_until_it_is_stopped_or_discarded(self, client, db):
        """Afterwards their timer would be hidden from them and its time discarded."""
        people, ws, _space, board_list = _team(client, db)
        issue = pm.issue(client, people["member"], ws, board_list["id"])
        _start_timer(client, people["member"], ws, issue["id"])

        refused = client.delete(_member(ws, "member-1"), headers=people["admin"])
        assert refused.status_code == 409
        assert refused.json()["data"]["issue_keys"] == [issue["key"]]
        assert f"Their timer is running on {issue['key']}" in refused.json()["message"]
        leaving = client.delete(_member(ws, "member-1"), headers=people["member"])
        assert f"Your timer is running on {issue['key']}" in leaving.json()["message"]

        pm.ok(client.delete("/timer", headers=people["member"]))
        pm.ok(client.delete(_member(ws, "member-1"), headers=people["admin"]))

    def test_only_their_own_timer_in_this_workspace_holds_them(self, client, db):
        """Someone else's timer here is not theirs to stop, and theirs in another workspace
        is not affected by leaving this one."""
        people, ws, _space, board_list = _team(client, db)
        here = pm.issue(client, people["owner"], ws, board_list["id"])
        _start_timer(client, people["owner"], ws, here["id"])
        other = pm.workspace(client, people["member"], "other")
        other_space = pm.space(client, people["member"], other, "OTH")
        other_list = pm.task_list(client, people["member"], other, other_space["id"])
        elsewhere = pm.issue(client, people["member"], other, other_list["id"])
        _start_timer(client, people["member"], other, elsewhere["id"])

        pm.ok(client.delete(_member(ws, "member-1"), headers=people["admin"]))
        assert db.query(ActiveTimer).count() == 2

    def test_report_schedules_that_need_them_hold_them_until_disabled(self, client, db):
        """A schedule renders as whoever set it up, and the worker fails every job whose
        requester has left — so it would stop reaching anyone. A private report's name is
        its owner's business: someone else is told how many, not what they are called."""
        people, ws, _space, _list = _team(client, db)
        base = f"{_workspace(ws)}/saved-reports"
        made = {}
        for name, visibility in (("Weekly hours", "workspace"), ("My hours", "private")):
            saved = pm.ok(client.post(base, json={
                "name": name, "report_type": "timesheet", "visibility": visibility,
                "params": {"date_from": "2026-09-28", "date_to": "2026-10-04"}},
                headers=people["admin"]), 201)
            made[name] = (saved["id"], pm.ok(client.post(
                f"{base}/{saved['id']}/schedules", json={"recipients": ["owner-1"]},
                headers=people["admin"]), 201)["id"])

        refused = client.delete(_member(ws, "admin-1"), headers=people["owner"])
        assert refused.status_code == 409
        message = refused.json()["message"]
        assert "Weekly hours and 1 on a private report" in message
        assert "My hours" not in message
        named = {s["saved_report_name"] for s in refused.json()["data"]["schedules"]}
        assert named == {"Weekly hours", None}
        # Leaving, they see their own report's name.
        leaving = client.delete(_member(ws, "admin-1"), headers=people["admin"])
        assert leaving.status_code == 409
        assert "My hours and Weekly hours" in leaving.json()["message"]

        for saved_id, schedule_id in made.values():
            pm.ok(client.patch(f"{base}/{saved_id}/schedules/{schedule_id}",
                               json={"enabled": False}, headers=people["admin"]))
        pm.ok(client.delete(_member(ws, "admin-1"), headers=people["owner"]))

    def test_a_schedule_on_their_private_report_holds_them_whoever_set_it_up(self, client,
                                                                             db):
        """Set up by someone else on a report that was shared then, and is private now: once
        its owner has left, nobody could open the report to stop the schedule."""
        people, ws, _space, _list = _team(client, db)
        base = f"{_workspace(ws)}/saved-reports"
        saved = pm.ok(client.post(base, json={
            "name": "Team hours", "report_type": "timesheet", "visibility": "workspace",
            "params": {"date_from": "2026-09-28", "date_to": "2026-10-04"}},
            headers=people["admin"]), 201)
        schedule = pm.ok(client.post(f"{base}/{saved['id']}/schedules",
                                     json={"recipients": ["member-1"]},
                                     headers=people["owner"]), 201)
        pm.ok(client.patch(f"{base}/{saved['id']}", json={"visibility": "private"},
                           headers=people["admin"]))

        refused = client.delete(_member(ws, "admin-1"), headers=people["owner"])
        assert refused.status_code == 409
        assert "1 on a private report" in refused.json()["message"]
        assert refused.json()["data"]["schedules"] == [
            {"id": schedule["id"], "saved_report_id": saved["id"], "saved_report_name": None}]

        pm.ok(client.delete(f"{base}/{saved['id']}/schedules/{schedule['id']}",
                            headers=people["admin"]))
        pm.ok(client.delete(_member(ws, "admin-1"), headers=people["owner"]))

    def test_every_reason_is_given_at_once(self, client, db):
        people, ws, space, board_list = _team(client, db)
        pm.ok(client.patch(f"{_workspace(ws)}/spaces/{space['id']}",
                           json={"owner_id": "member-1"}, headers=people["owner"]))
        issue = pm.issue(client, people["member"], ws, board_list["id"])
        _start_timer(client, people["member"], ws, issue["id"])
        refused = client.delete(_member(ws, "member-1"), headers=people["owner"])
        assert refused.status_code == 409
        assert set(refused.json()["data"]) == {"spaces", "issue_keys"}

    def test_what_they_did_stays_and_assigned_issues_stay_assigned(self, client, db):
        people, ws, _space, board_list = _team(client, db)
        issue = pm.issue(client, people["member"], ws, board_list["id"],
                         assignee_id="member-1")
        pm.log_time(client, people["member"], ws, issue["id"])
        pm.ok(client.delete(_member(ws, "member-1"), headers=people["admin"]))

        detail = pm.ok(client.get(f"{_workspace(ws)}/issues/{issue['id']}",
                                  headers=people["owner"]))
        assert detail["assignee"]["id"] == "member-1"
        assert detail["reporter"]["id"] == "member-1"
        assert db.query(TimeEntry).filter(TimeEntry.user_id == "member-1").count() == 1

    def test_workflow_edit_grants_go_with_the_membership(self, client, db):
        """A grant is a permission, not history. Kept, it would come back unasked the day
        they were added again."""
        people, ws, space, _list = _team(client, db)
        editors = f"{_workspace(ws)}/spaces/{space['id']}/workflow/editors"
        pm.ok(client.put(editors, json={"user_ids": ["member-1"]}, headers=people["owner"]))

        pm.ok(client.delete(_member(ws, "member-1"), headers=people["admin"]))
        assert db.query(SpaceWorkflowEditor).filter(
            SpaceWorkflowEditor.user_id == "member-1").count() == 0
        [entry] = _audit(db, "workspace_member", "DELETE")
        assert "workflow-edit grants revoked on 1 space" in entry.details

        pm.ok(client.post(f"{_workspace(ws)}/members", json={"email": "member-1@example.com"},
                          headers=people["owner"]), 201)
        assert pm.ok(client.get(editors, headers=people["owner"])) == []
