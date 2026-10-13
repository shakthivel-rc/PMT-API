"""Containers after 2026-10-05: write states, renaming and reparenting, and the tree.

docs/design/09-restructuring.md §3.4 and §3.5, through the HTTP API.

* **Archive / unarchive** (spaces, folders, lists) and **freeze / unfreeze** (spaces). A state
  change that is not ready is a 409 that says what to do first — close the sprint, stop the
  timer, wait for the migration plan. A write *into* a read-only container is the write
  guard's 423.
* **Renaming and reparenting**: the space PATCH and its key aliases — the old key keeps
  resolving and stays reserved — and the folder and list PATCH, within one space only.
* **The tree** carries every container's write state and each space's workflow rules, at a
  number of queries that does not grow with the number of spaces.

Tenant isolation first, as everywhere: each of these would still pass if an id from another
workspace worked inside yours; the first class would not.
"""
from datetime import datetime

import pytest
from sqlalchemy import event

import db.session as db_session
from controllers import github_controller
from models.activity_log import ActivityLog
from models.hierarchy import ProjectList, Space, SpaceKeyAlias, SpaceWorkflowEditor, WorkflowStatus
from models.migration import MigrationPlan
from services import key_resolution, transitions
from tests import pm_fixtures as pm

pytestmark = pytest.mark.integration


def _setup(client, db):
    """An owner, a workspace, the PAY space with one list, and a member and a guest."""
    owner, ws, space, board_list = pm.setup_board(client, db)
    member = pm.person(db, "member-1")
    pm.join(db, ws, "member-1", "member")
    guest = pm.person(db, "guest-1")
    pm.join(db, ws, "guest-1", "guest")
    return owner, member, guest, ws, space, board_list


def _act(client, headers, ws: str, kind: str, container_id: str, action: str, body=None):
    """`POST /workspaces/{ws}/{kind}s/{id}/{action}` — archive, unarchive, freeze, unfreeze."""
    return client.post(f"/workspaces/{ws}/{kind}s/{container_id}/{action}", json=body or {},
                       headers=headers)


def _patch(client, headers, ws: str, kind: str, container_id: str, **body):
    return client.patch(f"/workspaces/{ws}/{kind}s/{container_id}", json=body, headers=headers)


def _folder(client, headers, ws: str, space_id: str, name: str = "Q4") -> dict:
    return pm.ok(client.post(f"/workspaces/{ws}/spaces/{space_id}/folders",
                             json={"name": name}, headers=headers), 201)


def _list(client, headers, ws: str, space_id: str, name: str, folder_id: str | None = None):
    return client.post(f"/workspaces/{ws}/lists",
                       json={"space_id": space_id, "folder_id": folder_id, "name": name},
                       headers=headers)


def _tree_space(client, headers, ws: str, space_id: str) -> dict:
    tree = pm.ok(client.get(f"/workspaces/{ws}", headers=headers))
    return next(space for space in tree["spaces"] if space["id"] == space_id)


def _detail(client, headers, ws: str, space_id: str) -> dict:
    return pm.ok(client.get(f"/workspaces/{ws}/spaces/{space_id}", headers=headers))


def _sprint(client, headers, ws: str, space_id: str, name: str = "Sprint 1") -> dict:
    return pm.ok(client.post(f"/workspaces/{ws}/spaces/{space_id}/sprints",
                             json={"name": name}, headers=headers), 201)


def _plan(db, ws: str, status: str, name: str = "Wave 1") -> str:
    plan = MigrationPlan(workspace_id=ws, name=name, status=status, operations=[],
                         created_by="owner-1")
    db.add(plan)
    db.commit()
    return str(plan.id)


def _frozen_by(db, space_id: str, plan_id: str) -> None:
    """Freeze a space the way the migration runner will: with its plan's id on it."""
    db.query(Space).filter(Space.id == space_id).update({
        "frozen_at": datetime(2026, 10, 5, 9), "frozen_by": "owner-1",
        "freeze_reason": "Wave 1: Payments into Delivery", "frozen_by_plan_id": plan_id})
    db.commit()


def _audit_details(db, entity_type: str) -> list[str]:
    """The audit trail's changes to one kind of container, oldest first. `UPDATE` only: the
    space every test starts with was audited as a `CREATE`, and that is not what is checked."""
    return [row.details for row in db.query(ActivityLog)
            .filter(ActivityLog.entity_type == entity_type, ActivityLog.action == "UPDATE")
            .order_by(ActivityLog.created_at)]


class TestTenancyAndRoles:
    def test_another_workspaces_containers_are_not_found_inside_yours(self, client, db):
        """The path's workspace is yours, every id is someone else's. Membership passes; the
        tenant filter on each lookup must still answer 404 — and change nothing of theirs."""
        alice, ws_a, _space_a, list_a = pm.setup_board(client, db, "alice", "alpha", "ALP")
        bob, ws_b, space_b, list_b = pm.setup_board(client, db, "bob", "bravo", "BRA")
        folder_b = _folder(client, bob, ws_b, space_b["id"])

        def refused(response):
            return response.status_code == 404

        sid, fid, lid = space_b["id"], folder_b["id"], list_b["id"]
        assert refused(client.get(f"/workspaces/{ws_a}/spaces/{sid}", headers=alice))
        assert refused(_patch(client, alice, ws_a, "space", sid, name="Mine now"))
        for action, body in (("archive", None), ("unarchive", None),
                             ("freeze", {"reason": "x"}), ("unfreeze", None)):
            assert refused(_act(client, alice, ws_a, "space", sid, action, body)), action
        assert refused(_patch(client, alice, ws_a, "folder", fid, name="Mine now"))
        assert refused(_patch(client, alice, ws_a, "list", lid, name="Mine now"))
        for kind, cid in (("folder", fid), ("list", lid)):
            for action in ("archive", "unarchive"):
                assert refused(_act(client, alice, ws_a, kind, cid, action)), (kind, action)
        assert refused(client.post(f"/workspaces/{ws_a}/spaces/{sid}/folders",
                                   json={"name": "x"}, headers=alice))
        # Their folder as the target of a move of *your* list is just as unknown.
        assert refused(_patch(client, alice, ws_a, "list", list_a["id"], folder_id=fid))

        theirs = _tree_space(client, bob, ws_b, sid)
        assert theirs["name"] == "BRA space" and theirs["archived"] is False
        assert theirs["frozen"] is False and theirs["folders"][0]["name"] == "Q4"

    def test_members_and_guests_cannot_change_structure_but_can_read_it(self, client, db):
        """Every structural change is an admin's (§3.4, §3.5): a member — who can see all of
        it — gets 403, a guest too; both may read the space detail."""
        owner, member, guest, ws, space, board_list = _setup(client, db)
        folder = _folder(client, owner, ws, space["id"])
        for headers in (member, guest):
            assert _patch(client, headers, ws, "space", space["id"],
                          name="x").status_code == 403
            for action, body in (("archive", None), ("unarchive", None),
                                 ("freeze", {"reason": "x"}), ("unfreeze", None)):
                assert _act(client, headers, ws, "space", space["id"], action,
                            body).status_code == 403
            assert _patch(client, headers, ws, "folder", folder["id"],
                          name="x").status_code == 403
            assert _patch(client, headers, ws, "list", board_list["id"],
                          name="x").status_code == 403
            for kind, cid in (("folder", folder["id"]), ("list", board_list["id"])):
                assert _act(client, headers, ws, kind, cid, "archive").status_code == 403
            assert _detail(client, headers, ws, space["id"])["id"] == space["id"]
        assert client.get(f"/workspaces/{ws}/spaces/no-such-space",
                          headers=owner).status_code == 404


class TestArchive:
    def test_an_archived_space_stays_in_the_tree_flagged_and_unarchive_restores_it(
            self, client, db):
        """Archived is a flag, not a deletion: the space, its lists and its issues stay
        where they were, the tree marks it, and unarchiving clears exactly what archiving
        set. Both are audited."""
        owner, _member, guest, ws, space, board_list = _setup(client, db)
        pm.issue(client, owner, ws, board_list["id"])

        archived = pm.ok(_act(client, owner, ws, "space", space["id"], "archive"))
        assert archived["archived"] is True and archived["archived_at"]
        assert archived["lists"][0]["id"] == board_list["id"]
        in_tree = _tree_space(client, guest, ws, space["id"])
        assert in_tree["archived"] is True
        row = db.query(Space).filter(Space.id == space["id"]).one()
        assert row.archived_by == "owner-1" and row.archived_at is not None
        assert len(pm.ok(client.get(f"/workspaces/{ws}/issues", headers=guest))["records"]) == 1

        restored = pm.ok(_act(client, owner, ws, "space", space["id"], "unarchive"))
        assert restored["archived"] is False and restored["archived_at"] is None
        db.expire_all()
        row = db.query(Space).filter(Space.id == space["id"]).one()
        assert row.archived_by is None and row.archived_at is None
        assert _audit_details(db, "space")[-2:] == ["Archived space PAY (PAY space)",
                                                    "Unarchived space PAY (PAY space)"]

    def test_a_folder_and_a_list_archive_on_their_own_and_stay_in_the_tree(self, client, db):
        """Archiving a folder or a list sets its own flag; the tree keeps it, flagged, so the
        SPA can draw an Archived section."""
        owner, _member, _guest, ws, space, board_list = _setup(client, db)
        folder = _folder(client, owner, ws, space["id"])
        inner = pm.ok(_list(client, owner, ws, space["id"], "Inner", folder["id"]), 201)

        assert pm.ok(_act(client, owner, ws, "list", board_list["id"], "archive"))[
            "archived"] is True
        folder_node = pm.ok(_act(client, owner, ws, "folder", folder["id"], "archive"))
        assert folder_node["archived"] is True
        assert [item["id"] for item in folder_node["lists"]] == [inner["id"]]
        tree = _tree_space(client, owner, ws, space["id"])
        assert tree["archived"] is False
        assert [(item["name"], item["archived"]) for item in tree["lists"]] == [
            ("Backlog", True)]
        assert tree["folders"][0]["archived"] is True
        assert tree["folders"][0]["lists"][0]["archived"] is False
        assert _audit_details(db, "list") == ["Archived list Backlog"]
        assert _audit_details(db, "folder") == ["Archived folder Q4"]

    def test_a_space_with_an_active_sprint_is_archived_only_after_it_closes(self, client, db):
        """An archived space could never close its sprint, so the archive waits for it —
        and the refusal says exactly that, naming both."""
        owner, _member, _guest, ws, space, _board_list = _setup(client, db)
        sprint = _sprint(client, owner, ws, space["id"])
        pm.ok(client.post(f"/workspaces/{ws}/sprints/{sprint['id']}/start", json={},
                          headers=owner))

        refused = _act(client, owner, ws, "space", space["id"], "archive")
        assert refused.status_code == 409
        assert refused.json()["message"] == "Close Sprint 1 before archiving PAY space."
        assert refused.json()["data"]["sprint_id"] == sprint["id"]
        assert db.query(Space.archived_at).filter(Space.id == space["id"]).scalar() is None

        pm.ok(client.post(f"/workspaces/{ws}/sprints/{sprint['id']}/close", json={},
                          headers=owner))
        assert pm.ok(_act(client, owner, ws, "space", space["id"], "archive"))["archived"]

    def test_a_running_timer_blocks_archiving_every_container_it_is_in(self, client, db):
        """Stopping a timer writes a time entry; inside an archived container that write
        would be refused and the time lost. So the space, the folder and the list holding
        the issue each refuse while it runs, naming the issue — and all three archive once
        it is stopped."""
        owner, _member, _guest, ws, space, _board_list = _setup(client, db)
        folder = _folder(client, owner, ws, space["id"])
        inner = pm.ok(_list(client, owner, ws, space["id"], "Inner", folder["id"]), 201)
        timed = pm.issue(client, owner, ws, inner["id"])
        pm.ok(client.post(f"/workspaces/{ws}/issues/{timed['id']}/timer", json={},
                          headers=owner), 201)

        for kind, cid in (("list", inner["id"]), ("folder", folder["id"]),
                          ("space", space["id"])):
            refused = _act(client, owner, ws, kind, cid, "archive")
            assert refused.status_code == 409, kind
            assert timed["key"] in refused.json()["message"]
            assert refused.json()["data"]["issue_keys"] == [timed["key"]]

        pm.ok(client.post("/timer/stop", json={}, headers=owner))
        for kind, cid in (("list", inner["id"]), ("folder", folder["id"]),
                          ("space", space["id"])):
            assert pm.ok(_act(client, owner, ws, kind, cid, "archive"))["archived"], kind

    def test_a_folder_or_list_whose_issues_are_in_an_open_sprint_waits(self, client, db):
        """Closing a sprint carries its unfinished issues over, and deleting a planned one
        sends them to the backlog: both write issues an archived list must not let change.
        So a list or folder with an issue in a sprint that is not closed refuses, naming the
        issue and the sprint, until the issue is out of it."""
        owner, _member, _guest, ws, space, board_list = _setup(client, db)
        planned = _sprint(client, owner, ws, space["id"], "Sprint 7")
        held = pm.issue(client, owner, ws, board_list["id"])
        pm.issue(client, owner, ws, board_list["id"], title="Not in a sprint")
        pm.ok(client.post(f"/workspaces/{ws}/sprints/{planned['id']}/issues",
                          json={"issue_ids": [held["id"]]}, headers=owner))

        refused = _act(client, owner, ws, "list", board_list["id"], "archive")
        assert refused.status_code == 409
        message = refused.json()["message"]
        assert held["key"] in message and "Sprint 7" in message
        assert refused.json()["data"] == {"issue_keys": [held["key"]], "sprints": ["Sprint 7"]}

        pm.ok(client.request("DELETE", f"/workspaces/{ws}/sprints/{planned['id']}/issues",
                             json={"issue_ids": [held["id"]]}, headers=owner))
        assert pm.ok(_act(client, owner, ws, "list", board_list["id"], "archive"))["archived"]

    def test_archive_and_unarchive_are_409_when_the_state_already_holds(self, client, db):
        """Archiving twice, or unarchiving something live, changes nothing and says so —
        one answer for all three kinds, as freezing twice does."""
        owner, _member, _guest, ws, space, board_list = _setup(client, db)
        folder = _folder(client, owner, ws, space["id"])
        for kind, cid in (("list", board_list["id"]), ("folder", folder["id"]),
                          ("space", space["id"])):
            assert _act(client, owner, ws, kind, cid, "unarchive").status_code == 409, kind
            pm.ok(_act(client, owner, ws, kind, cid, "archive"))
            again = _act(client, owner, ws, kind, cid, "archive")
            assert again.status_code == 409 and "already archived" in again.json()["message"]

    def test_inside_an_archived_container_state_changes_go_top_down(self, client, db):
        """A list in an archived folder or space is read-only, its own flag included: archiving
        or unarchiving it is a 423 that says what holds it. Unarchive the top first."""
        owner, _member, _guest, ws, space, board_list = _setup(client, db)
        folder = _folder(client, owner, ws, space["id"])
        inner = pm.ok(_list(client, owner, ws, space["id"], "Inner", folder["id"]), 201)
        pm.ok(_act(client, owner, ws, "list", inner["id"], "archive"))
        pm.ok(_act(client, owner, ws, "folder", folder["id"], "archive"))

        locked = _act(client, owner, ws, "list", inner["id"], "unarchive")
        assert locked.status_code == 423 and "folder Q4" in locked.json()["message"]

        pm.ok(_act(client, owner, ws, "space", space["id"], "archive"))
        for kind, cid, action in (("folder", folder["id"], "unarchive"),
                                  ("list", board_list["id"], "archive")):
            locked = _act(client, owner, ws, kind, cid, action)
            assert locked.status_code == 423, (kind, action)
            assert "PAY space is archived" in locked.json()["message"]

        pm.ok(_act(client, owner, ws, "space", space["id"], "unarchive"))
        pm.ok(_act(client, owner, ws, "folder", folder["id"], "unarchive"))
        assert pm.ok(_act(client, owner, ws, "list", inner["id"], "unarchive"))[
            "archived"] is False


class TestWriteGuard:
    def test_no_list_or_folder_in_an_archived_space_and_no_list_in_an_archived_folder(
            self, client, db):
        """Creating a container inside a read-only one is a write there (§3.4): 423, and
        nothing created."""
        owner, _member, _guest, ws, space, _board_list = _setup(client, db)
        folder = _folder(client, owner, ws, space["id"])
        pm.ok(_act(client, owner, ws, "folder", folder["id"], "archive"))
        in_folder = _list(client, owner, ws, space["id"], "Late", folder["id"])
        assert in_folder.status_code == 423 and "folder Q4" in in_folder.json()["message"]

        pm.ok(_act(client, owner, ws, "space", space["id"], "archive"))
        at_root = _list(client, owner, ws, space["id"], "Late")
        assert at_root.status_code == 423
        assert "PAY space is archived" in at_root.json()["message"]
        assert client.post(f"/workspaces/{ws}/spaces/{space['id']}/folders",
                           json={"name": "Late"}, headers=owner).status_code == 423
        assert db.query(ProjectList).filter(ProjectList.name == "Late").count() == 0

    def test_no_folder_or_list_in_a_frozen_space_and_the_reason_is_quoted(self, client, db):
        """A frozen space refuses new containers with the freeze reason in the message, so
        the person refused knows why and for how long."""
        owner, _member, _guest, ws, space, _board_list = _setup(client, db)
        pm.ok(_act(client, owner, ws, "space", space["id"], "freeze",
                   {"reason": "Wave 1: lists move to Delivery"}))
        folder = client.post(f"/workspaces/{ws}/spaces/{space['id']}/folders",
                             json={"name": "Late"}, headers=owner)
        assert folder.status_code == 423
        assert "Wave 1: lists move to Delivery" in folder.json()["message"]
        assert _list(client, owner, ws, space["id"], "Late").status_code == 423

    def test_renaming_or_moving_an_archived_container_is_locked_unarchive_is_not(
            self, client, db):
        """Archived means read-only for renames and reparenting too; the way out — unarchive
        — is the one change it accepts."""
        owner, _member, _guest, ws, space, board_list = _setup(client, db)
        folder = _folder(client, owner, ws, space["id"])
        spare = pm.ok(_list(client, owner, ws, space["id"], "Spare"), 201)
        pm.ok(_act(client, owner, ws, "list", board_list["id"], "archive"))
        pm.ok(_act(client, owner, ws, "folder", folder["id"], "archive"))

        assert _patch(client, owner, ws, "list", board_list["id"],
                      name="Renamed").status_code == 423
        assert _patch(client, owner, ws, "list", board_list["id"],
                      position=5).status_code == 423
        assert _patch(client, owner, ws, "folder", folder["id"],
                      name="Renamed").status_code == 423
        into_archived = _patch(client, owner, ws, "list", spare["id"], folder_id=folder["id"])
        assert into_archived.status_code == 423
        assert db.query(ProjectList.folder_id).filter(
            ProjectList.id == spare["id"]).scalar() is None

        pm.ok(_act(client, owner, ws, "space", space["id"], "archive"))
        assert _patch(client, owner, ws, "space", space["id"],
                      name="Renamed").status_code == 423
        assert _patch(client, owner, ws, "space", space["id"], key="NEW").status_code == 423
        pm.ok(_act(client, owner, ws, "space", space["id"], "unarchive"))
        pm.ok(_act(client, owner, ws, "list", board_list["id"], "unarchive"))
        assert pm.ok(_patch(client, owner, ws, "list", board_list["id"],
                            name="Renamed"))["name"] == "Renamed"

    def test_a_frozen_space_refuses_edits_to_itself_and_its_containers(self, client, db):
        """Mid-wave, a key change would invalidate the keys the migration recorded and a
        rename would move what its dry-run checked: frozen is read-only for those too."""
        owner, _member, _guest, ws, space, board_list = _setup(client, db)
        folder = _folder(client, owner, ws, space["id"])
        pm.ok(_act(client, owner, ws, "space", space["id"], "freeze", {"reason": "Wave 1"}))
        for kind, cid, body in (("space", space["id"], {"key": "NEW"}),
                                ("folder", folder["id"], {"name": "x"}),
                                ("list", board_list["id"], {"folder_id": folder["id"]})):
            assert _patch(client, owner, ws, kind, cid, **body).status_code == 423, kind
        assert db.query(Space.key).filter(Space.id == space["id"]).scalar() == "PAY"


class TestFreeze:
    def test_freezing_needs_a_reason_and_shows_in_the_tree(self, client, db):
        """The reason is what every refused writer reads, so it is required and cannot be
        blank; the tree shows the freeze and why; the detail says who set it (not a plan)."""
        owner, _member, guest, ws, space, _board_list = _setup(client, db)
        for body in ({}, {"reason": "   "}, {"reason": "x" * 501}):
            assert _act(client, owner, ws, "space", space["id"], "freeze",
                        body).status_code == 422, body
        frozen = pm.ok(_act(client, owner, ws, "space", space["id"], "freeze",
                            {"reason": "  Wave 1  "}))
        assert frozen["frozen"] is True and frozen["freeze_reason"] == "Wave 1"
        assert _tree_space(client, guest, ws, space["id"])["freeze_reason"] == "Wave 1"
        detail = _detail(client, guest, ws, space["id"])
        assert detail["frozen_at"] and detail["frozen_by_plan_id"] is None
        row = db.query(Space).filter(Space.id == space["id"]).one()
        assert row.frozen_by == "owner-1"
        assert _audit_details(db, "space") == ["Froze space PAY: Wave 1"]

    def test_freeze_refuses_an_archived_or_frozen_space_and_archive_a_frozen_one(self, client,
                                                                                  db):
        """The two read-only states do not stack by hand: an archived space is already
        read-only for good, a frozen one is mid-wave and archiving it would pull the space
        out from under the migration."""
        owner, _member, _guest, ws, space, _board_list = _setup(client, db)
        pm.ok(_act(client, owner, ws, "space", space["id"], "freeze", {"reason": "Wave 1"}))
        again = _act(client, owner, ws, "space", space["id"], "freeze", {"reason": "Wave 2"})
        assert again.status_code == 409 and "already frozen" in again.json()["message"]
        archive = _act(client, owner, ws, "space", space["id"], "archive")
        assert archive.status_code == 409 and "Unfreeze" in archive.json()["message"]

        other = pm.space(client, owner, ws, "OPS")
        pm.ok(_act(client, owner, ws, "space", other["id"], "archive"))
        refused = _act(client, owner, ws, "space", other["id"], "freeze", {"reason": "x"})
        assert refused.status_code == 409 and "archived" in refused.json()["message"]

    def test_unfreeze_clears_the_freeze_and_reports_the_replay(self, client, db,
                                                               monkeypatch):
        """Unfreezing lifts every part of the freeze, is audited, and then replays the
        workspace's deferred GitHub deliveries — after the commit, for this workspace — and
        answers with what the replay did."""
        owner, _member, _guest, ws, space, _board_list = _setup(client, db)
        calls = []

        def replay(session, workspace_id):
            calls.append((workspace_id, session.query(Space.frozen_at)
                          .filter(Space.id == space["id"]).scalar()))
            return {"replayed": 2, "still_deferred": 1, "failed": 0}

        monkeypatch.setattr(github_controller, "replay_deferred", replay)
        pm.ok(_act(client, owner, ws, "space", space["id"], "freeze", {"reason": "Wave 1"}))
        result = pm.ok(_act(client, owner, ws, "space", space["id"], "unfreeze"))

        assert calls == [(ws, None)], "the replay must run once, after the unfreeze is saved"
        assert result["replay"] == {"replayed": 2, "still_deferred": 1, "failed": 0}
        assert result["space"]["frozen"] is False and result["space"]["freeze_reason"] is None
        db.expire_all()
        row = db.query(Space).filter(Space.id == space["id"]).one()
        assert (row.frozen_at, row.frozen_by, row.freeze_reason, row.frozen_by_plan_id) == (
            None, None, None, None)
        assert _audit_details(db, "space")[-1] == "Unfroze space PAY"

    def test_the_real_replay_answers_in_the_contract_shape(self, client, db):
        """Without a stand-in: whatever the integrations module replays, the answer carries
        three counts."""
        owner, _member, _guest, ws, space, _board_list = _setup(client, db)
        pm.ok(_act(client, owner, ws, "space", space["id"], "freeze", {"reason": "Wave 1"}))
        replay = pm.ok(_act(client, owner, ws, "space", space["id"], "unfreeze"))["replay"]
        assert set(replay) == {"replayed", "still_deferred", "failed"}
        assert all(isinstance(count, int) for count in replay.values())

    @pytest.mark.parametrize("status, hint", [("running", "Let it finish"),
                                              ("failed", "execute the plan again")])
    def test_a_space_frozen_by_a_running_or_failed_plan_stays_frozen(self, client, db,
                                                                     status, hint):
        """The plan says to keep writes paused while a wave runs and while a failed one is
        repaired or reversed (§8.5): unfreezing by hand is refused, naming the plan, and the
        freeze is untouched."""
        owner, _member, _guest, ws, space, _board_list = _setup(client, db)
        plan_id = _plan(db, ws, status)
        _frozen_by(db, space["id"], plan_id)

        refused = _act(client, owner, ws, "space", space["id"], "unfreeze")
        assert refused.status_code == 409
        assert "Wave 1" in refused.json()["message"] and hint in refused.json()["message"]
        assert refused.json()["data"] == {"plan_id": plan_id, "plan_status": status}
        db.expire_all()
        assert db.query(Space.frozen_by_plan_id).filter(
            Space.id == space["id"]).scalar() == plan_id

    def test_once_the_plan_is_done_an_admin_may_unfreeze(self, client, db):
        """A completed (or rolled back, or canceled) plan no longer holds the freeze; the
        admin's unfreeze clears the plan id with the rest."""
        owner, _member, _guest, ws, space, _board_list = _setup(client, db)
        plan_id = _plan(db, ws, "completed")
        _frozen_by(db, space["id"], plan_id)
        assert _detail(client, owner, ws, space["id"])["frozen_by_plan_id"] == plan_id
        result = pm.ok(_act(client, owner, ws, "space", space["id"], "unfreeze"))
        assert result["space"]["frozen"] is False
        assert db.query(Space.frozen_by_plan_id).filter(
            Space.id == space["id"]).scalar() is None
        assert plan_id in _audit_details(db, "space")[-1]

    def test_unfreezing_a_space_that_is_not_frozen_is_a_409(self, client, db):
        """Nothing to lift is a conflict, not a silent success."""
        owner, _member, _guest, ws, space, _board_list = _setup(client, db)
        refused = _act(client, owner, ws, "space", space["id"], "unfreeze")
        assert refused.status_code == 409 and "not frozen" in refused.json()["message"]

    def test_a_replay_that_raises_does_not_hide_the_unfreeze(self, client, db, monkeypatch):
        """The unfreeze is committed before the replay runs. A replay that raises must not
        turn that into a 500 the admin would retry against a space that is no longer
        frozen: the answer is 200, the space unfrozen, `replay` null, and the message says
        the deliveries are still waiting."""
        owner, _member, _guest, ws, space, _board_list = _setup(client, db)

        def explode(_session, _workspace_id):
            raise RuntimeError("deliveries table unavailable")

        monkeypatch.setattr(github_controller, "replay_deferred", explode)
        pm.ok(_act(client, owner, ws, "space", space["id"], "freeze", {"reason": "Wave 1"}))
        response = _act(client, owner, ws, "space", space["id"], "unfreeze")
        assert response.status_code == 200, response.text
        assert response.json()["data"]["replay"] is None
        assert "still waiting" in response.json()["message"]
        assert response.json()["data"]["space"]["frozen"] is False


class TestSpaceDetailAndPatch:
    def test_patch_changes_only_what_is_present_and_null_clears(self, client, db):
        """`model_fields_set` semantics: an omitted field is untouched, null clears a
        nullable one, and the two that cannot be empty — name and key — refuse null."""
        owner, _member, _guest, ws, space, _board_list = _setup(client, db)
        pm.person(db, "lead-1")
        pm.join(db, ws, "lead-1", "member")
        path_kind, sid = "space", space["id"]

        detail = pm.ok(_patch(client, owner, ws, path_kind, sid, name="Payments",
                              description="Cards.", owner_id="lead-1",
                              delivery_method="kanban",
                              charter={"objective": "Refunds.", "notes": "n"}))
        assert (detail["name"], detail["description"], detail["owner"]["id"],
                detail["delivery_method"]) == ("Payments", "Cards.", "lead-1", "kanban")
        assert detail["charter"]["objective"] == "Refunds." and detail["has_charter"] is True

        detail = pm.ok(_patch(client, owner, ws, path_kind, sid, charter={"scope": "Cards"}))
        assert detail["charter"]["objective"] is None, "the charter is replaced, not merged"
        assert detail["charter"]["scope"] == "Cards" and detail["name"] == "Payments"

        detail = pm.ok(_patch(client, owner, ws, path_kind, sid, description=None,
                              owner_id=None, delivery_method=None, charter=None))
        assert (detail["description"], detail["owner"], detail["delivery_method"],
                detail["charter"], detail["has_charter"]) == (None, None, None, None, False)
        assert detail["name"] == "Payments" and detail["key"] == "PAY"

        for field in ("name", "key"):
            assert _patch(client, owner, ws, path_kind, sid,
                          **{field: None}).status_code == 422, field
        pm.person(db, "outsider")
        assert _patch(client, owner, ws, path_kind, sid,
                      owner_id="outsider").status_code == 422
        assert _patch(client, owner, ws, path_kind, sid, colour="red").status_code == 422
        assert _audit_details(db, "space")[0].startswith("Updated space PAY: name PAY space")

    def test_a_key_change_keeps_the_old_key_resolving_and_reserved(self, client, db):
        """Plan Step 2 §2 rule 1, enforced: renaming PAY to PAYM re-keys every issue, PAY-1
        still finds the same issue, no other space can take PAY, and the space itself can
        take PAY back — after which PAYM is the alias."""
        owner, _member, _guest, ws, space, board_list = _setup(client, db)
        first = pm.issue(client, owner, ws, board_list["id"])
        assert first["key"] == "PAY-1"

        detail = pm.ok(_patch(client, owner, ws, "space", space["id"], key="PAYM"))
        assert detail["key"] == "PAYM" and detail["key_aliases"] == ["PAY"]
        assert pm.ok(client.get(f"/workspaces/{ws}/issues/{first['id']}",
                                headers=owner))["key"] == "PAYM-1"
        found, how = key_resolution.resolve_key(db, ws, "PAY", 1)
        assert (found.id, how) == (first["id"], key_resolution.RESOLVED_SPACE_ALIAS)
        found, how = key_resolution.resolve_key(db, ws, "PAYM", 1)
        assert (found.id, how) == (first["id"], key_resolution.RESOLVED_CURRENT)

        other = pm.space(client, owner, ws, "OPS")
        assert client.post(f"/workspaces/{ws}/spaces", json={"name": "Again", "key": "PAY"},
                           headers=owner).status_code == 409
        taken = _patch(client, owner, ws, "space", other["id"], key="PAY")
        assert taken.status_code == 409 and "used to belong" in taken.json()["message"]
        assert _patch(client, owner, ws, "space", other["id"],
                      key="PAYM").status_code == 409

        back = pm.ok(_patch(client, owner, ws, "space", space["id"], key="PAY"))
        assert back["key"] == "PAY" and back["key_aliases"] == ["PAYM"]
        found, how = key_resolution.resolve_key(db, ws, "PAYM", 1)
        assert (found.id, how) == (first["id"], key_resolution.RESOLVED_SPACE_ALIAS)
        assert db.query(SpaceKeyAlias).count() == 1
        assert "key PAY -> PAYM" in _audit_details(db, "space")[0]

    def test_patching_the_same_key_or_nothing_records_nothing(self, client, db):
        """No change is no alias and no audit line: an alias equal to the live key would
        make the key reserved against its own space."""
        owner, _member, _guest, ws, space, _board_list = _setup(client, db)
        detail = pm.ok(_patch(client, owner, ws, "space", space["id"], key="PAY"))
        assert detail["key_aliases"] == []
        pm.ok(_patch(client, owner, ws, "space", space["id"]))
        assert db.query(SpaceKeyAlias).count() == 0
        assert _audit_details(db, "space") == []

    def test_with_the_checklist_required_a_filled_item_cannot_be_cleared(self, client, db):
        """The epic rule of §2.3 applied to the space checklist: while the workspace requires
        it, what is filled in stays filled in — but a space that predates the setting is
        still editable without completing it first."""
        owner, _member, _guest, ws, old_space, _board_list = _setup(client, db)
        pm.ok(client.patch(f"/workspaces/{ws}/settings/governance",
                           json={"require_space_charter": True}, headers=owner))
        full = {"objective": "Refunds.", "success_measure": "Tickets halve.",
                "reuse_justification": "Own cadence."}
        space = pm.ok(client.post(f"/workspaces/{ws}/spaces", json={
            "name": "Payments", "key": "PAYS", "owner_id": "owner-1",
            "delivery_method": "scrum", "charter": full}, headers=owner), 201)

        cleared = _patch(client, owner, ws, "space", space["id"], owner_id=None,
                         charter={"objective": "Refunds."})
        assert cleared.status_code == 422
        assert cleared.json()["data"]["missing"] == [
            "owner_id", "charter.success_measure", "charter.reuse_justification"]
        assert pm.ok(_patch(client, owner, ws, "space", space["id"],
                            description="Still fine."))["description"] == "Still fine."
        assert pm.ok(_patch(client, owner, ws, "space", old_space["id"],
                            name="Legacy"))["name"] == "Legacy"


class TestFoldersAndLists:
    def test_positions_are_set_as_given_and_ties_order_by_name(self, client, db):
        """Nothing is renumbered behind the caller's back; equal positions are ordered by
        name, so the tree is deterministic whatever the SPA sends."""
        owner, _member, _guest, ws, space, board_list = _setup(client, db)
        beta = _folder(client, owner, ws, space["id"], "Beta")
        alpha = _folder(client, owner, ws, space["id"], "Alpha")
        assert (beta["position"], alpha["position"]) == (0, 1)
        moved = pm.ok(_patch(client, owner, ws, "folder", alpha["id"], position=0,
                             name=" Alpha team "))
        assert (moved["name"], moved["position"]) == ("Alpha team", 0)

        second = pm.ok(_list(client, owner, ws, space["id"], "Another"), 201)
        pm.ok(_patch(client, owner, ws, "list", second["id"], position=0))
        tree = _tree_space(client, owner, ws, space["id"])
        assert [folder["name"] for folder in tree["folders"]] == ["Alpha team", "Beta"]
        assert [item["name"] for item in tree["lists"]] == ["Another", "Backlog"]
        assert [item["id"] for item in tree["lists"]] == [second["id"], board_list["id"]]

        for body in ({"name": None}, {"name": "  "}, {"position": None}, {"position": -1},
                     {"position": 1_000_001}, {"lists": []}):
            assert _patch(client, owner, ws, "folder", alpha["id"],
                          **body).status_code == 422, body

    def test_a_list_moves_between_folders_of_its_own_space_only(self, client, db):
        """Reparenting is within the space (§3.5): into a folder, back to the root, never into
        another space's folder — that would re-key its issues, which is a transfer. A moved
        list without a position goes to the bottom of its new place."""
        owner, _member, _guest, ws, space, board_list = _setup(client, db)
        folder = _folder(client, owner, ws, space["id"])
        pm.ok(_list(client, owner, ws, space["id"], "Already here", folder["id"]), 201)
        issue = pm.issue(client, owner, ws, board_list["id"])

        moved = pm.ok(_patch(client, owner, ws, "list", board_list["id"],
                             folder_id=folder["id"]))
        assert (moved["folder_id"], moved["position"]) == (folder["id"], 1)
        tree = _tree_space(client, owner, ws, space["id"])
        assert tree["lists"] == []
        assert [item["name"] for item in tree["folders"][0]["lists"]] == [
            "Already here", "Backlog"]
        assert pm.ok(client.get(f"/workspaces/{ws}/issues/{issue['id']}",
                                headers=owner))["key"] == issue["key"]

        back = pm.ok(_patch(client, owner, ws, "list", board_list["id"], folder_id=None,
                            position=4))
        assert (back["folder_id"], back["position"]) == (None, 4)

        other = pm.space(client, owner, ws, "OPS")
        their_folder = _folder(client, owner, ws, other["id"], "Elsewhere")
        across = _patch(client, owner, ws, "list", board_list["id"],
                        folder_id=their_folder["id"])
        assert across.status_code == 422 and "transfer" in across.json()["message"]
        assert _patch(client, owner, ws, "list", board_list["id"],
                      folder_id="no-such-folder").status_code == 404
        assert db.query(ProjectList.folder_id).filter(
            ProjectList.id == board_list["id"]).scalar() is None
        assert _audit_details(db, "list")[0] == "Updated list Backlog: moved to folder Q4"


class TestTree:
    def test_every_space_carries_its_write_state_owner_and_workflow(self, client, db):
        """The fields the sidebar and the board read, for a space just created: present,
        typed, and true of the space."""
        owner, _member, guest, ws, space, board_list = _setup(client, db)
        node = _tree_space(client, guest, ws, space["id"])
        for field in ("description", "owner", "delivery_method", "archived_at",
                      "freeze_reason"):
            assert node[field] is None, field
        assert (node["has_charter"], node["archived"], node["frozen"]) == (False, False, False)
        assert node["workflow"]["version"] == 1
        assert node["workflow"]["wip_policy"] == "warn"
        assert node["can_edit_workflow"] is False, "a guest never edits a workflow"
        assert node["lists"] == [{"id": board_list["id"], "name": "Backlog",
                                  "folder_id": None, "position": 0, "archived": False}]
        assert _tree_space(client, owner, ws, space["id"])["can_edit_workflow"] is True

    def test_can_edit_workflow_follows_the_per_space_grant(self, client, db):
        """§4.6: admins edit every workflow, a member only where an admin granted it, a guest
        never — even with a grant row."""
        owner, member, guest, ws, space, _board_list = _setup(client, db)
        other = pm.space(client, owner, ws, "OPS")
        for user_id in ("member-1", "guest-1"):
            db.add(SpaceWorkflowEditor(workspace_id=ws, space_id=space["id"], user_id=user_id,
                                       granted_by="owner-1"))
        db.commit()
        assert _tree_space(client, member, ws, space["id"])["can_edit_workflow"] is True
        assert _tree_space(client, member, ws, other["id"])["can_edit_workflow"] is False
        assert _tree_space(client, guest, ws, space["id"])["can_edit_workflow"] is False
        assert _tree_space(client, owner, ws, other["id"])["can_edit_workflow"] is True

    def test_the_initial_stage_is_services_transitions_answer(self, client, db):
        """The tree computes the initial stage over stages it already loaded; it must give
        the answer `services/transitions.initial_status` gives, including when the chosen
        stage has been removed and the rule falls back."""
        owner, _member, _guest, ws, space, _board_list = _setup(client, db)
        row = db.query(Space).filter(Space.id == space["id"]).one()
        assert (_tree_space(client, owner, ws, space["id"])["workflow"]["initial_status_id"]
                == transitions.initial_status(db, ws, row).id)

        db.query(WorkflowStatus).filter(WorkflowStatus.id == pm.status_id(space, "To Do")
                                        ).update({"deleted": 1})
        db.commit()
        db.expire_all()
        row = db.query(Space).filter(Space.id == space["id"]).one()
        expected = transitions.initial_status(db, ws, row)
        assert expected.name == "In Progress"
        assert (_tree_space(client, owner, ws, space["id"])["workflow"]["initial_status_id"]
                == expected.id)

    def test_transitions_and_a_merge_target_into_a_removed_stage_are_left_out(self, client,
                                                                              db):
        """No move can enter a removed stage and no merge can land in one, so the tree offers
        neither: the transitions touching it go, and the merge target reads as null."""
        owner, _member, _guest, ws, _space, _board_list = _setup(client, db)
        space = pm.ok(client.post(f"/workspaces/{ws}/spaces", json={
            "name": "Delivery", "key": "DEL", "workflow_template": "delivery"},
            headers=owner), 201)
        qa = pm.status_id(space, "QA")
        db.query(WorkflowStatus).filter(WorkflowStatus.id == qa).update({"deleted": 1})
        db.commit()

        node = _tree_space(client, owner, ws, space["id"])
        assert qa not in {status["id"] for status in node["statuses"]}
        ends = {end for t in node["workflow"]["transitions"]
                for end in (t["from_status_id"], t["to_status_id"])}
        assert qa not in ends and len(node["workflow"]["transitions"]) == 6
        assert node["workflow"]["merge_action"] == {"mode": "status", "target_status_id": None}

    def test_the_tree_costs_the_same_number_of_queries_for_one_space_or_many(self, client,
                                                                             db):
        """The sidebar of a workspace with many spaces must cost what one with a single space
        does: one query per level, never one per space (`services/workspace.space_nodes`).
        Each space here has an owner, a folder, lists and restricted transitions, so every
        branch of the payload is exercised."""
        owner, _member, _guest, ws, space, _board_list = _setup(client, db)

        def furnish(space_id):
            pm.ok(_patch(client, owner, ws, "space", space_id, owner_id="member-1"))
            folder = _folder(client, owner, ws, space_id)
            pm.ok(_list(client, owner, ws, space_id, "Inner", folder["id"]), 201)

        def measure() -> int:
            statements: list[str] = []

            def listener(_conn, _cursor, statement, *_args):
                statements.append(statement)

            event.listen(db_session.engine, "before_cursor_execute", listener)
            try:
                pm.ok(client.get(f"/workspaces/{ws}", headers=owner))
            finally:
                event.remove(db_session.engine, "before_cursor_execute", listener)
            return len(statements)

        furnish(space["id"])
        one = measure()
        for key in ("OPS", "WEB", "APP"):
            created = pm.ok(client.post(f"/workspaces/{ws}/spaces", json={
                "name": f"{key} space", "key": key, "workflow_template": "delivery",
                "initial_lists": ["A", "B"]}, headers=owner), 201)
            furnish(created["id"])
        many = measure()
        assert many == one, f"the tree's queries grew with its spaces: {one} -> {many}"
