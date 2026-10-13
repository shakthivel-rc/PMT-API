"""Customisable workflows through the HTTP API (docs/design/09-restructuring.md §4).

The rules of a draft are unit-tested in tests/unit/test_workflow_validation.py. What only a
database can show is here: that publishing keeps every issue's identity and history while
stages are renamed, reordered, added and removed; that a removed stage's issues really move,
each with a `status_changed` event by the person who published; that the refusals (stale
draft, changed counts, unacknowledged reporting change, archived or frozen space) leave the
workflow exactly as it was; and that what is published is what services/transitions.py — the
module the board, the webhook and issue creation obey — then answers.

Every epic created here has an owner and completion criteria, and every relationship is a
standard one (§2), so these tests keep passing under the hierarchy rules.
"""
from dataclasses import replace
from datetime import UTC, datetime

import pytest

from models.activity_log import ActivityLog
from models.hierarchy import (
    Space,
    SpaceWorkflowEditor,
    WorkflowRevision,
    WorkflowStatus,
    WorkflowTemplate,
)
from models.issue import Issue, IssueEvent
from models.workspace import Workspace
from services import governance, transitions
from services import workflow as wf
from tests import pm_fixtures as pm

pytestmark = pytest.mark.integration


# ---- helpers ---------------------------------------------------------------------------------


def _url(ws: str, space_id: str, tail: str = "") -> str:
    return f"/workspaces/{ws}/spaces/{space_id}/workflow{tail}"


def _workflow(client, headers, ws: str, space_id: str) -> dict:
    return pm.ok(client.get(_url(ws, space_id), headers=headers))


def _draft(workflow: dict) -> dict:
    """The workflow loaded into a draft, the way the editor does before anyone edits."""
    return {
        "base_version": workflow["version"],
        "transition_mode": workflow["transition_mode"],
        "wip_policy": workflow["wip_policy"],
        "stages": [{"ref": s["id"], "name": s["name"], "category": s["category"],
                    "color": s["color"], "counts_toward_wip": s["counts_toward_wip"],
                    "wip_limit": s["wip_limit"]} for s in workflow["stages"]],
        "initial": workflow["initial_status_id"],
        "transitions": [{"from": t["from_status_id"], "to": t["to_status_id"]}
                        for t in workflow["transitions"]],
        "replacements": {},
        "merge_action": {"mode": workflow["merge_action"]["mode"],
                         "target": workflow["merge_action"]["target_status_id"]},
    }


def _stage(draft: dict, name: str) -> dict:
    return next(s for s in draft["stages"] if s["name"] == name)


def _drop(draft: dict, name: str) -> str:
    """Remove the stage called `name` from the draft; returns its id."""
    removed = _stage(draft, name)
    draft["stages"].remove(removed)
    return removed["ref"]


def _preview(client, headers, ws: str, space_id: str, draft: dict):
    return client.post(_url(ws, space_id, "/preview"), json=draft, headers=headers)


def _publish(client, headers, ws: str, space_id: str, draft: dict, confirmed: int = 0,
             ack: bool = False):
    return client.put(_url(ws, space_id), headers=headers,
                      json={**draft, "confirmed_affected_issues": confirmed,
                            "acknowledge_reporting_changes": ack})


def _codes(payload: dict, kind: str = "errors") -> list[str]:
    return [problem["code"] for problem in payload[kind]]


def _setup(client, db):
    owner, ws, space, board_list = pm.setup_board(client, db)
    return owner, ws, space, board_list


def _person_in(db, ws: str, user_id: str, role: str) -> dict:
    headers = pm.person(db, user_id)
    pm.join(db, ws, user_id, role)
    return headers


def _grant(db, ws: str, space_id: str, user_id: str, by: str = "owner-1") -> None:
    db.add(SpaceWorkflowEditor(workspace_id=ws, space_id=space_id, user_id=user_id,
                               granted_by=by))
    db.commit()


def _events(db, issue_id: str, event: str = "status_changed") -> list[IssueEvent]:
    db.expire_all()
    return (db.query(IssueEvent)
            .filter(IssueEvent.issue_id == issue_id, IssueEvent.event == event)
            .order_by(IssueEvent.created_at, IssueEvent.id)
            .all())


def _space_row(db, space_id: str) -> Space:
    db.expire_all()
    return db.query(Space).filter(Space.id == space_id).one()


# ---- reading --------------------------------------------------------------------------------


class TestRead:
    def test_a_new_space_has_the_default_workflow_with_its_counts(self, client, db):
        """Counts by stage, and WIP counts only story, task and bug: an epic and a subtask
        in the column are not work items of their own (§4.7)."""
        owner, ws, space, board_list = _setup(client, db)
        doing = pm.status_id(space, "In Progress")
        pm.issue(client, owner, ws, board_list["id"], "First")
        pm.issue(client, owner, ws, board_list["id"], "Second")
        story = pm.issue(client, owner, ws, board_list["id"], "Story", type="story",
                         status_id=doing)
        pm.issue(client, owner, ws, board_list["id"], "Epic", type="epic", status_id=doing,
                 assignee_id="owner-1", completion_criteria="Shipped and measured")
        pm.issue(client, owner, ws, board_list["id"], "Part", type="subtask", status_id=doing,
                 parent_id=story["id"])

        workflow = _workflow(client, owner, ws, space["id"])
        assert [s["name"] for s in workflow["stages"]] == ["To Do", "In Progress",
                                                           "In Review", "Done"]
        assert [s["position"] for s in workflow["stages"]] == [0, 1, 2, 3]
        counts = {s["name"]: (s["issue_count"], s["wip_count"]) for s in workflow["stages"]}
        assert counts == {"To Do": (2, 2), "In Progress": (3, 1), "In Review": (0, 0),
                          "Done": (0, 0)}
        assert workflow["version"] == 1 and workflow["transition_mode"] == "any"
        assert workflow["initial_status_id"] == pm.status_id(space, "To Do")
        assert workflow["merge_action"] == {"mode": "status",
                                            "target_status_id": pm.status_id(space, "Done")}
        assert workflow["transitions"] == [] and workflow["editors"] == []
        assert workflow["can_edit"] is True

    def test_who_can_edit_is_answered_for_the_caller(self, client, db):
        _owner, ws, space, _list = _setup(client, db)
        guest = _person_in(db, ws, "guest-1", "guest")
        member = _person_in(db, ws, "member-1", "member")
        admin = _person_in(db, ws, "admin-1", "admin")
        assert _workflow(client, guest, ws, space["id"])["can_edit"] is False
        assert _workflow(client, member, ws, space["id"])["can_edit"] is False
        assert _workflow(client, admin, ws, space["id"])["can_edit"] is True
        _grant(db, ws, space["id"], "member-1")
        assert _workflow(client, member, ws, space["id"])["can_edit"] is True

    def test_another_workspaces_space_is_not_found(self, client, db):
        """404, not 403, for an id of another tenant; and a stranger cannot tell the
        workspace exists."""
        headers_a, ws_a, _space_a, _ = pm.setup_board(client, db, "alice", "alpha", "ALP")
        headers_b, _ws_b, space_b, _ = pm.setup_board(client, db, "bob", "bravo", "BRA")
        assert client.get(_url(ws_a, space_b["id"]), headers=headers_a).status_code == 404
        stranger = pm.person(db, "stranger")
        assert client.get(_url(ws_a, space_b["id"]), headers=stranger).status_code == 404
        assert client.get(f"/workspaces/{ws_a}/spaces/{space_b['id']}/workflow/editors",
                          headers=headers_a).status_code == 404
        draft = _draft(_workflow(client, headers_b, _ws_b, space_b["id"]))
        assert _preview(client, headers_a, ws_a, space_b["id"], draft).status_code == 404
        assert _publish(client, headers_a, ws_a, space_b["id"], draft).status_code == 404


# ---- permissions ------------------------------------------------------------------------------


class TestPermissions:
    def test_a_member_without_a_grant_is_refused(self, client, db):
        owner, ws, space, _list = _setup(client, db)
        member = _person_in(db, ws, "member-1", "member")
        draft = _draft(_workflow(client, owner, ws, space["id"]))
        assert _preview(client, member, ws, space["id"], draft).status_code == 403
        assert _publish(client, member, ws, space["id"], draft).status_code == 403
        assert _space_row(db, space["id"]).workflow_version == 1

    def test_a_granted_member_previews_and_publishes(self, client, db):
        """The plan's "without requiring administrator intervention for each change"."""
        owner, ws, space, _list = _setup(client, db)
        member = _person_in(db, ws, "member-1", "member")
        _grant(db, ws, space["id"], "member-1")
        draft = _draft(_workflow(client, owner, ws, space["id"]))
        _stage(draft, "In Review")["name"] = "Review"
        assert pm.ok(_preview(client, member, ws, space["id"], draft))["valid"] is True
        assert pm.ok(_publish(client, member, ws, space["id"], draft))["version"] == 2

    def test_a_guest_is_refused_even_when_granted(self, client, db):
        """A publish moves other people's issues; a guest is read-only everywhere."""
        owner, ws, space, _list = _setup(client, db)
        guest = _person_in(db, ws, "guest-1", "guest")
        _grant(db, ws, space["id"], "guest-1")
        draft = _draft(_workflow(client, owner, ws, space["id"]))
        assert _preview(client, guest, ws, space["id"], draft).status_code == 403
        assert _publish(client, guest, ws, space["id"], draft).status_code == 403
        assert _workflow(client, guest, ws, space["id"])["can_edit"] is False

    def test_an_admin_needs_no_grant(self, client, db):
        owner, ws, space, _list = _setup(client, db)
        admin = _person_in(db, ws, "admin-1", "admin")
        draft = _draft(_workflow(client, owner, ws, space["id"]))
        assert pm.ok(_publish(client, admin, ws, space["id"], draft))["version"] == 2

    def test_a_grant_on_one_space_is_not_a_grant_on_another(self, client, db):
        owner, ws, space, _list = _setup(client, db)
        other = pm.space(client, owner, ws, "OPS")
        member = _person_in(db, ws, "member-1", "member")
        _grant(db, ws, space["id"], "member-1")
        draft = _draft(_workflow(client, owner, ws, other["id"]))
        assert _publish(client, member, ws, other["id"], draft).status_code == 403


# ---- publishing ---------------------------------------------------------------------------------


class TestPublish:
    def test_rename_and_reorder_keep_ids_issue_identity_and_history(self, client, db):
        """Stage ids are what issues, events and reports point at; a rename or a drag that
        minted new ones would orphan all three."""
        owner, ws, space, board_list = _setup(client, db)
        review = pm.status_id(space, "In Review")
        card = pm.issue(client, owner, ws, board_list["id"], "Reviewed", status_id=review)
        events_before = len(_events(db, card["id"], "created"))

        draft = _draft(_workflow(client, owner, ws, space["id"]))
        _stage(draft, "In Review")["name"] = "Code review"
        to_do, doing, code_review, done = draft["stages"]
        draft["stages"] = [to_do, code_review, doing, done]
        preview = pm.ok(_preview(client, owner, ws, space["id"], draft))
        assert {s["name"]: s["change"] for s in preview["stages"]}["Code review"] == "renamed"
        result = pm.ok(_publish(client, owner, ws, space["id"], draft))

        assert result["version"] == 2 and result["moved_issues"] == 0
        stages = result["workflow"]["stages"]
        assert [(s["id"], s["name"], s["position"]) for s in stages] == [
            (to_do["ref"], "To Do", 0), (review, "Code review", 1), (doing["ref"],
                                                                     "In Progress", 2),
            (done["ref"], "Done", 3)]
        after = pm.ok(client.get(f"/workspaces/{ws}/issues/{card['id']}", headers=owner))
        assert (after["status_id"], after["key"], after["version"]) == (review, card["key"],
                                                                         card["version"])
        assert _events(db, card["id"]) == []
        assert len(_events(db, card["id"], "created")) == events_before == 1
        board = pm.board(client, owner, ws, board_list["id"])
        assert [c["status"]["name"] for c in board["columns"]] == ["To Do", "Code review",
                                                                   "In Progress", "Done"]

    def test_add_a_stage(self, client, db):
        owner, ws, space, _list = _setup(client, db)
        draft = _draft(_workflow(client, owner, ws, space["id"]))
        draft["stages"].insert(3, {"ref": "new:qa", "name": "QA", "category": "in_progress",
                                   "color": "warning", "wip_limit": 2})
        result = pm.ok(_publish(client, owner, ws, space["id"], draft))
        stages = result["workflow"]["stages"]
        assert [s["name"] for s in stages] == ["To Do", "In Progress", "In Review", "QA",
                                               "Done"]
        qa = stages[3]
        assert qa["id"] != "new:qa" and len(qa["id"]) == 36
        assert (qa["color"], qa["wip_limit"], qa["counts_toward_wip"], qa["position"]) == (
            "warning", 2, True, 3)
        assert pm.ok(client.get(f"/workspaces/{ws}", headers=owner))["spaces"][0][
            "statuses"][3]["name"] == "QA"

    def test_removing_a_populated_stage_needs_a_replacement_then_moves_its_issues(self,
                                                                                  client, db):
        """Plan §4: removal requires a replacement and a confirmed count; identities and
        history stay; each move is a `status_changed` event by the publisher."""
        owner, ws, space, board_list = _setup(client, db)
        member = _person_in(db, ws, "member-1", "member")
        _grant(db, ws, space["id"], "member-1")
        review, done = pm.status_id(space, "In Review"), pm.status_id(space, "Done")
        shipped = pm.issue(client, owner, ws, board_list["id"], "Shipped", status_id=done)
        first = pm.issue(client, owner, ws, board_list["id"], "First", status_id=review)
        second = pm.issue(client, owner, ws, board_list["id"], "Second", status_id=review)

        draft = _draft(_workflow(client, member, ws, space["id"]))
        _drop(draft, "In Review")
        preview = pm.ok(_preview(client, member, ws, space["id"], draft))
        assert _codes(preview) == ["replacement_required"]
        assert preview["affected_issue_count"] == 2
        refused = _publish(client, member, ws, space["id"], draft, confirmed=2)
        assert refused.status_code == 422
        assert "replacement_required" in _codes(refused.json()["data"])

        draft["replacements"] = {review: done}
        preview = pm.ok(_preview(client, member, ws, space["id"], draft))
        assert preview["valid"] and preview["moves"]["into_closed"] == 2
        assert "moves_into_closed" in _codes(preview, "warnings")
        result = pm.ok(_publish(client, member, ws, space["id"], draft, confirmed=2))
        assert result["moved_issues"] == 2
        assert review not in {s["id"] for s in result["workflow"]["stages"]}

        db.expire_all()
        rows = {row.id: row for row in db.query(Issue).filter(
            Issue.id.in_([first["id"], second["id"], shipped["id"]]))}
        assert {rows[first["id"]].status_id, rows[second["id"]].status_id} == {done}
        # Appended below what Done already held, in the order they had.
        assert rows[shipped["id"]].rank < rows[first["id"]].rank < rows[second["id"]].rank
        assert rows[first["id"]].version == first["version"] + 1
        assert rows[first["id"]].status_entered_at is not None
        for card in (first, second):
            [event] = _events(db, card["id"])
            assert (event.from_value, event.to_value) == (review, done)
            assert (event.actor_id, event.actor_kind) == ("member-1", "user")
            after = pm.ok(client.get(f"/workspaces/{ws}/issues/{card['id']}", headers=owner))
            assert after["key"] == card["key"]
        # The removed stage is kept for history, and still answers its category.
        removed = db.query(WorkflowStatus).filter(WorkflowStatus.id == review).one()
        assert removed.deleted == 1 and removed.deleted_at is not None
        assert transitions.category_of(db, ws, review) == "in_progress"
        board = pm.board(client, owner, ws, board_list["id"])
        assert pm.column_titles(board, "Done") == ["Shipped", "First", "Second"]

    def test_issues_can_move_into_a_stage_the_same_publish_creates(self, client, db):
        """The replacement exists only once the publish has inserted it; the moves must
        follow the insert, or they would point at a stage id that is not there yet."""
        owner, ws, space, board_list = _setup(client, db)
        review = pm.status_id(space, "In Review")
        card = pm.issue(client, owner, ws, board_list["id"], "Reviewed", status_id=review)
        draft = _draft(_workflow(client, owner, ws, space["id"]))
        _drop(draft, "In Review")
        draft["stages"].insert(2, {"ref": "new:qa", "name": "QA", "category": "in_progress"})
        draft["replacements"] = {review: "new:qa"}
        workflow = pm.ok(_publish(client, owner, ws, space["id"], draft, confirmed=1))[
            "workflow"]
        qa = next(s for s in workflow["stages"] if s["name"] == "QA")
        assert qa["issue_count"] == 1
        moved = pm.ok(client.get(f"/workspaces/{ws}/issues/{card['id']}", headers=owner))
        assert moved["status_id"] == qa["id"]
        [event] = _events(db, card["id"])
        assert (event.from_value, event.to_value) == (review, qa["id"])

    def test_two_removed_stages_into_one_land_in_board_order_below_what_it_held(self,
                                                                               client, db):
        owner, ws, space, board_list = _setup(client, db)
        doing, review = pm.status_id(space, "In Progress"), pm.status_id(space, "In Review")
        done = pm.status_id(space, "Done")
        pm.issue(client, owner, ws, board_list["id"], "Already done", status_id=done)
        pm.issue(client, owner, ws, board_list["id"], "Reviewed", status_id=review)
        pm.issue(client, owner, ws, board_list["id"], "Doing 1", status_id=doing)
        pm.issue(client, owner, ws, board_list["id"], "Doing 2", status_id=doing)
        draft = _draft(_workflow(client, owner, ws, space["id"]))
        _drop(draft, "In Progress")
        _drop(draft, "In Review")
        draft["replacements"] = {doing: done, review: done}
        assert pm.ok(_publish(client, owner, ws, space["id"], draft, confirmed=3))[
            "moved_issues"] == 3
        board = pm.board(client, owner, ws, board_list["id"])
        assert pm.column_titles(board, "Done") == ["Already done", "Doing 1", "Doing 2",
                                                   "Reviewed"]

    def test_switching_back_to_any_keeps_the_designed_flow(self, client, db):
        """Transitions are ignored in `any` mode, not discarded: switching back to
        `restricted` must not mean drawing the flow again."""
        owner, ws, space, _list = _setup(client, db)
        draft = _draft(_workflow(client, owner, ws, space["id"]))
        to_do, doing, review, done = (s["ref"] for s in draft["stages"])
        draft["transition_mode"] = "restricted"
        draft["transitions"] = [{"from": to_do, "to": doing}, {"from": doing, "to": review},
                                {"from": review, "to": done}, {"from": None, "to": done}]
        restricted = pm.ok(_publish(client, owner, ws, space["id"], draft))["workflow"]
        loose = _draft(restricted)
        loose["transition_mode"] = "any"
        workflow = pm.ok(_publish(client, owner, ws, space["id"], loose))["workflow"]
        assert workflow["transition_mode"] == "any" and len(workflow["transitions"]) == 4
        space_row = _space_row(db, space["id"])
        statuses = {s.id: s for s in transitions.live_statuses(db, ws, space["id"])}
        assert transitions.transition_problem(db, ws, space_row, statuses[done],
                                              statuses[to_do]) is None

    def test_a_category_change_on_a_populated_stage_needs_the_acknowledgement(self, client,
                                                                              db):
        """Re-labelling a stage re-labels its issues and their history in every report;
        nobody should do that by accident."""
        owner, ws, space, board_list = _setup(client, db)
        review = pm.status_id(space, "In Review")
        pm.issue(client, owner, ws, board_list["id"], "Waiting", status_id=review)
        draft = _draft(_workflow(client, owner, ws, space["id"]))
        _stage(draft, "In Review")["category"] = "done"
        preview = pm.ok(_preview(client, owner, ws, space["id"], draft))
        assert preview["requires_reporting_ack"] is True
        assert preview["category_changes"][0]["issue_count"] == 1

        refused = _publish(client, owner, ws, space["id"], draft)
        assert refused.status_code == 422
        assert refused.json()["data"]["requires_reporting_ack"] is True
        assert _space_row(db, space["id"]).workflow_version == 1

        result = pm.ok(_publish(client, owner, ws, space["id"], draft, ack=True))
        assert {s["id"]: s["category"] for s in result["workflow"]["stages"]}[review] == "done"

    def test_a_stale_draft_is_refused(self, client, db):
        owner, ws, space, _list = _setup(client, db)
        draft = _draft(_workflow(client, owner, ws, space["id"]))
        pm.ok(_publish(client, owner, ws, space["id"], draft))
        _stage(draft, "Done")["name"] = "Finished"
        refused = _publish(client, owner, ws, space["id"], draft)
        assert refused.status_code == 409
        assert refused.json()["data"] == {"current_version": 2}
        assert "Finished" not in {s["name"] for s in _workflow(client, owner, ws,
                                                               space["id"])["stages"]}

    def test_a_count_that_changed_since_the_preview_is_refused(self, client, db):
        """"7 issues are in Review now, not 5": nobody reviewed the move of the other two."""
        owner, ws, space, board_list = _setup(client, db)
        review, done = pm.status_id(space, "In Review"), pm.status_id(space, "Done")
        pm.issue(client, owner, ws, board_list["id"], "Seen", status_id=review)
        late = pm.issue(client, owner, ws, board_list["id"], "Late")
        draft = _draft(_workflow(client, owner, ws, space["id"]))
        _drop(draft, "In Review")
        draft["replacements"] = {review: done}
        assert pm.ok(_preview(client, owner, ws, space["id"], draft))[
            "affected_issue_count"] == 1
        pm.ok(client.post(f"/workspaces/{ws}/issues/{late['id']}/move",
                          json={"status_id": review}, headers=owner))

        refused = _publish(client, owner, ws, space["id"], draft, confirmed=1)
        assert refused.status_code == 409
        assert refused.json()["data"] == {"affected_issue_count": 2}
        assert review in {s["id"] for s in _workflow(client, owner, ws, space["id"])["stages"]}
        assert pm.ok(_publish(client, owner, ws, space["id"], draft, confirmed=2))[
            "moved_issues"] == 2

    def test_an_issue_that_reached_a_removed_stage_after_the_count_is_never_stranded(
            self, client, db, monkeypatch):
        """The counts are read under the space lock, but a board move does not take it: an
        issue can land in a stage being removed between the count and the move. Leaving it
        in a soft-deleted stage would hide it from every board, so the publish refuses and
        the person previews again. The race is simulated by a snapshot that missed it."""
        owner, ws, space, board_list = _setup(client, db)
        review = pm.status_id(space, "In Review")
        pm.issue(client, owner, ws, board_list["id"], "Late arrival", status_id=review)
        real_snapshot = wf.snapshot

        def missed_it(session, workspace_id, space_row):
            stages = real_snapshot(session, workspace_id, space_row).stages
            return wf.WorkflowSnapshot(stages=tuple(
                replace(stage, issue_count=0, wip_count=0) if stage.id == review else stage
                for stage in stages))

        monkeypatch.setattr(wf, "snapshot", missed_it)
        draft = _draft(_workflow(client, owner, ws, space["id"]))
        _drop(draft, "In Review")
        refused = _publish(client, owner, ws, space["id"], draft, confirmed=1)
        assert refused.status_code == 409
        assert "no replacement" in refused.json()["message"]
        assert refused.json()["data"] == {"affected_issue_count": 1}
        assert _space_row(db, space["id"]).workflow_version == 1
        assert db.query(WorkflowStatus).filter(WorkflowStatus.id == review).one().deleted == 0

    def test_restricted_with_an_unreachable_stage_is_refused(self, client, db):
        owner, ws, space, _list = _setup(client, db)
        draft = _draft(_workflow(client, owner, ws, space["id"]))
        to_do, doing, review, done = (s["ref"] for s in draft["stages"])
        draft["transition_mode"] = "restricted"
        draft["transitions"] = [{"from": to_do, "to": doing}, {"from": doing, "to": done}]
        refused = _publish(client, owner, ws, space["id"], draft)
        assert refused.status_code == 422
        errors = refused.json()["data"]["errors"]
        assert [(e["code"], e["refs"]) for e in errors] == [("unreachable_stage", [review])]
        assert _space_row(db, space["id"]).transition_mode == "any"

    def test_removing_the_merge_target_forces_a_choice(self, client, db):
        """Never a silent fallback to some other completed stage (§6.2)."""
        owner, ws, space, _list = _setup(client, db)
        done = pm.status_id(space, "Done")
        draft = _draft(_workflow(client, owner, ws, space["id"]))
        _drop(draft, "Done")
        draft["stages"].append({"ref": "new:shipped", "name": "Shipped", "category": "done"})
        draft["replacements"] = {done: "new:shipped"}
        refused = _publish(client, owner, ws, space["id"], draft)
        assert refused.status_code == 422
        assert _codes(refused.json()["data"]) == ["merge_target_invalid"]

        draft["merge_action"] = {"mode": "none", "target": None}
        result = pm.ok(_publish(client, owner, ws, space["id"], draft))
        assert result["workflow"]["merge_action"] == {"mode": "none",
                                                      "target_status_id": None}
        space_row = _space_row(db, space["id"])
        assert (space_row.merge_action, space_row.merge_target_status_id) == ("none", None)
        assert transitions.merge_target(db, ws, space_row) is None

    def test_the_merge_target_can_move_to_a_new_stage(self, client, db):
        owner, ws, space, _list = _setup(client, db)
        draft = _draft(_workflow(client, owner, ws, space["id"]))
        draft["stages"].insert(3, {"ref": "new:qa", "name": "QA", "category": "in_progress"})
        draft["merge_action"] = {"mode": "status", "target": "new:qa"}
        result = pm.ok(_publish(client, owner, ws, space["id"], draft))
        qa = next(s for s in result["workflow"]["stages"] if s["name"] == "QA")
        assert result["workflow"]["merge_action"] == {"mode": "status",
                                                      "target_status_id": qa["id"]}
        assert transitions.merge_target(db, ws, _space_row(db, space["id"])).id == qa["id"]

    def test_a_canceled_merge_target_is_refused(self, client, db):
        """Merged work was not canceled; pointing the merge there would close delivered
        work as undelivered."""
        owner, ws, space, _list = _setup(client, db)
        draft = _draft(_workflow(client, owner, ws, space["id"]))
        draft["stages"].append({"ref": "new:wont", "name": "Won't do", "category": "canceled"})
        draft["merge_action"] = {"mode": "status", "target": "new:wont"}
        refused = _publish(client, owner, ws, space["id"], draft)
        assert refused.status_code == 422
        assert _codes(refused.json()["data"]) == ["merge_target_invalid"]

    def test_self_transitions_are_refused_and_duplicates_stored_once(self, client, db):
        owner, ws, space, _list = _setup(client, db)
        draft = _draft(_workflow(client, owner, ws, space["id"]))
        to_do, doing, review, done = (s["ref"] for s in draft["stages"])
        draft["transition_mode"] = "restricted"
        draft["transitions"] = [{"from": doing, "to": doing}]
        assert _publish(client, owner, ws, space["id"], draft).status_code == 422
        draft["transitions"] = [{"from": to_do, "to": doing}, {"from": to_do, "to": doing},
                                {"from": doing, "to": review}, {"from": review, "to": done},
                                {"from": None, "to": done}]
        result = pm.ok(_publish(client, owner, ws, space["id"], draft))
        stored = [(t["from_status_id"], t["to_status_id"])
                  for t in result["workflow"]["transitions"]]
        assert sorted(stored, key=str) == sorted([(to_do, doing), (doing, review),
                                                  (review, done), (None, done)], key=str)

    def test_what_is_published_is_what_transitions_obeys(self, client, db):
        """services/transitions.py is what the board move, the webhook and issue creation
        ask; a publish that stored a flow it then did not answer would be a restriction the
        API enforces nowhere."""
        owner, ws, space, _list = _setup(client, db)
        draft = _draft(_workflow(client, owner, ws, space["id"]))
        to_do, doing, review, done = (s["ref"] for s in draft["stages"])
        draft["stages"].insert(0, {"ref": "new:backlog", "name": "Backlog",
                                   "category": "todo"})
        draft["initial"] = "new:backlog"
        draft["transition_mode"] = "restricted"
        draft["transitions"] = [{"from": "new:backlog", "to": to_do},
                                {"from": to_do, "to": doing}, {"from": doing, "to": review},
                                {"from": review, "to": doing}, {"from": review, "to": done},
                                {"from": None, "to": "new:backlog"}]
        draft["merge_action"] = {"mode": "status", "target": review}
        preview = pm.ok(_preview(client, owner, ws, space["id"], draft))
        assert preview["valid"]
        assert preview["integration"]["unreachable_from"] == ["Backlog", "To Do"]
        workflow = pm.ok(_publish(client, owner, ws, space["id"], draft))["workflow"]
        backlog = workflow["stages"][0]["id"]

        space_row = _space_row(db, space["id"])
        statuses = {s.id: s for s in transitions.live_statuses(db, ws, space["id"])}
        assert transitions.initial_status(db, ws, space_row).id == backlog
        assert transitions.transition_problem(db, ws, space_row, statuses[review],
                                              statuses[done]) is None
        assert transitions.transition_problem(db, ws, space_row, statuses[done],
                                              statuses[backlog]) is None
        problem = transitions.transition_problem(db, ws, space_row, statuses[to_do],
                                                 statuses[done])
        assert problem is not None and "In Progress" in problem
        assert transitions.merge_target(db, ws, space_row).id == review
        assert [s.position for s in statuses.values()] == [0, 1, 2, 3, 4]

    def test_publishing_is_audited_and_recorded_as_a_revision(self, client, db):
        owner, ws, space, board_list = _setup(client, db)
        review, done = pm.status_id(space, "In Review"), pm.status_id(space, "Done")
        pm.issue(client, owner, ws, board_list["id"], "Moving", status_id=review)
        draft = _draft(_workflow(client, owner, ws, space["id"]))
        _drop(draft, "In Review")
        draft["replacements"] = {review: done}
        pm.ok(_publish(client, owner, ws, space["id"], draft, confirmed=1))

        db.expire_all()
        [revision] = db.query(WorkflowRevision).filter(
            WorkflowRevision.space_id == space["id"]).all()
        assert (revision.version, revision.published_by) == (2, "owner-1")
        assert [s["name"] for s in revision.definition["stages"]] == ["To Do", "In Progress",
                                                                     "Done"]
        assert revision.definition["merge_target_status_id"] == done
        assert revision.impact["moved_issues"] == 1
        assert revision.impact["removed"] == [{"id": review, "name": "In Review",
                                               "issue_count": 1, "replacement_id": done}]
        [audit] = db.query(ActivityLog).filter(ActivityLog.entity_type == "workflow").all()
        assert (audit.action, audit.entity_id, audit.user_id) == ("UPDATE", space["id"],
                                                                  "owner-1")
        assert "version 2" in audit.details and "1 issue moved" in audit.details

    @pytest.mark.parametrize("column", ["frozen_at", "archived_at"])
    def test_a_frozen_or_archived_space_refuses_the_publish(self, client, db, column):
        """423 from the one write guard; the preview, which writes nothing, still answers."""
        owner, ws, space, _list = _setup(client, db)
        draft = _draft(_workflow(client, owner, ws, space["id"]))
        db.query(Space).filter(Space.id == space["id"]).update(
            {getattr(Space, column): datetime.now(UTC)})
        db.commit()
        _stage(draft, "Done")["name"] = "Finished"
        refused = _publish(client, owner, ws, space["id"], draft)
        assert refused.status_code == 423
        assert pm.ok(_preview(client, owner, ws, space["id"], draft))["valid"] is True
        assert _space_row(db, space["id"]).workflow_version == 1


class TestResetToDefault:
    def test_the_default_template_is_published_through_the_same_safeguards(self, client,
                                                                             db):
        """Plan §4 item 8: resetting uses the same impact review and stage mapping as any
        other change. The editor maps template stages onto live ones by name, so matching
        stages keep their ids; the rest are removed and need replacements."""
        owner, ws, space, board_list = _setup(client, db)
        draft = _draft(_workflow(client, owner, ws, space["id"]))
        to_do, doing, review, done = (s["ref"] for s in draft["stages"])
        _stage(draft, "In Review")["name"] = "Review"
        draft["stages"].insert(3, {"ref": "new:qa", "name": "QA", "category": "in_progress"})
        draft["transition_mode"] = "restricted"
        draft["transitions"] = [{"from": to_do, "to": doing}, {"from": doing, "to": review},
                                {"from": review, "to": "new:qa"},
                                {"from": "new:qa", "to": done}, {"from": None, "to": to_do}]
        draft["merge_action"] = {"mode": "status", "target": "new:qa"}
        custom = pm.ok(_publish(client, owner, ws, space["id"], draft))["workflow"]
        qa = next(s["id"] for s in custom["stages"] if s["name"] == "QA")
        # A restricted flow creates only into its initial stage (§4.3), so each issue walks
        # the transitions to where the reset will find it.
        in_qa = pm.issue(client, owner, ws, board_list["id"], "In QA")
        in_review = pm.issue(client, owner, ws, board_list["id"], "In review")
        for issue, path in ((in_qa, (doing, review, qa)), (in_review, (doing, review))):
            for stage_id in path:
                pm.ok(client.post(f"/workspaces/{ws}/issues/{issue['id']}/move",
                                  json={"status_id": stage_id}, headers=owner))

        # What the editor does on "Reset to default": load the template and the workflow as
        # it is now, and map template stages onto live ones by name.
        templates = pm.ok(client.get(f"/workspaces/{ws}/workflow-templates", headers=owner))
        default = next(t for t in templates if t["id"] == "builtin:default")["definition"]
        custom = _workflow(client, owner, ws, space["id"])
        live = {s["name"].casefold(): s for s in custom["stages"]}
        stages = []
        for index, entry in enumerate(default["stages"]):
            match = live.get(entry["name"].casefold())
            stages.append({**entry, "ref": match["id"] if match else f"new:{index}"})
        kept = {s["ref"] for s in stages}
        replacements = {}
        for stage in custom["stages"]:
            if stage["id"] not in kept and stage["issue_count"]:
                same = next(s for s in stages if s["category"] == stage["category"])
                replacements[stage["id"]] = same["ref"]
        reset = {
            "base_version": custom["version"],
            "transition_mode": default["transition_mode"], "wip_policy": default["wip_policy"],
            "stages": stages, "initial": stages[default["initial"]]["ref"],
            "transitions": [{"from": None if t["from"] is None else stages[t["from"]]["ref"],
                             "to": stages[t["to"]]["ref"]} for t in default["transitions"]],
            "replacements": replacements,
            "merge_action": {"mode": "status", "target": stages[default["merge_target"]]["ref"]},
        }
        preview = pm.ok(_preview(client, owner, ws, space["id"], reset))
        assert preview["valid"], preview["errors"]
        assert preview["affected_issue_count"] == 2
        assert {r["name"] for r in preview["removed"]} == {"Review", "QA"}

        refused = _publish(client, owner, ws, space["id"], reset, confirmed=0)
        assert refused.status_code == 409
        result = pm.ok(_publish(client, owner, ws, space["id"], reset, confirmed=2))
        workflow = result["workflow"]
        assert [(s["name"], s["category"], s["color"]) for s in workflow["stages"]] == [
            (s["name"], s["category"], s["color"]) for s in default["stages"]]
        assert workflow["transition_mode"] == "any" and workflow["transitions"] == []
        ids = [s["id"] for s in workflow["stages"]]
        assert ids[0] == to_do and ids[1] == doing and ids[3] == done
        assert workflow["initial_status_id"] == to_do
        assert workflow["merge_action"]["target_status_id"] == done
        for card in (in_qa, in_review):
            [*_, event] = _events(db, card["id"])
            assert event.to_value == ids[1] and event.actor_id == "owner-1"


# ---- templates -------------------------------------------------------------------------------


class TestTemplates:
    def test_the_builtins_come_first_and_cannot_be_deleted(self, client, db):
        owner, ws, _space, _list = _setup(client, db)
        guest = _person_in(db, ws, "guest-1", "guest")
        templates = pm.ok(client.get(f"/workspaces/{ws}/workflow-templates", headers=guest))
        assert [t["id"] for t in templates] == ["builtin:default", "builtin:delivery",
                                                "builtin:support"]
        assert all(t["builtin"] and t["created_by"] is None for t in templates)
        delivery = templates[1]["definition"]
        assert delivery["transition_mode"] == "restricted"
        assert delivery["stages"][delivery["merge_target"]]["name"] == "QA"
        refused = client.delete(f"/workspaces/{ws}/workflow-templates/builtin:default",
                                headers=owner)
        assert refused.status_code == 404
        assert "Built-in templates" in refused.json()["message"]

    def test_save_a_spaces_workflow_and_read_it_back(self, client, db):
        owner, ws, space, _list = _setup(client, db)
        draft = _draft(_workflow(client, owner, ws, space["id"]))
        _stage(draft, "In Review")["name"] = "Review"
        pm.ok(_publish(client, owner, ws, space["id"], draft))
        saved = pm.ok(client.post(f"/workspaces/{ws}/workflow-templates", headers=owner,
                                  json={"name": "Our flow", "description": "Reviewed work",
                                        "from_space_id": space["id"]}), 201)
        assert saved["builtin"] is False and saved["key"] is None
        assert saved["created_by"]["id"] == "owner-1"
        assert [s["name"] for s in saved["definition"]["stages"]] == ["To Do", "In Progress",
                                                                      "Review", "Done"]
        assert saved["definition"]["merge_target"] == 3
        listed = pm.ok(client.get(f"/workspaces/{ws}/workflow-templates", headers=owner))
        assert [t["name"] for t in listed][3:] == ["Our flow"]
        [audit] = db.query(ActivityLog).filter(
            ActivityLog.entity_type == "workflow_template").all()
        assert audit.action == "CREATE"

    def test_who_may_save_a_template(self, client, db):
        """From a space: whoever may edit its workflow. From a written definition: admins,
        because nothing reviewed it."""
        owner, ws, space, _list = _setup(client, db)
        member = _person_in(db, ws, "member-1", "member")
        guest = _person_in(db, ws, "guest-1", "guest")
        definition = pm.ok(client.get(f"/workspaces/{ws}/workflow-templates",
                                      headers=owner))[0]["definition"]
        url = f"/workspaces/{ws}/workflow-templates"
        assert client.post(url, json={"name": "A", "from_space_id": space["id"]},
                           headers=member).status_code == 403
        assert client.post(url, json={"name": "A", "from_space_id": space["id"]},
                           headers=guest).status_code == 403
        assert client.post(url, json={"name": "A", "definition": definition},
                           headers=member).status_code == 403
        _grant(db, ws, space["id"], "member-1")
        pm.ok(client.post(url, json={"name": "A", "from_space_id": space["id"]},
                          headers=member), 201)
        pm.ok(client.post(url, json={"name": "B", "definition": definition},
                          headers=owner), 201)
        assert client.post(url, json={"name": "C", "from_space_id": "nope"},
                           headers=owner).status_code == 404

    def test_names_are_unique_ignoring_case(self, client, db):
        owner, ws, space, _list = _setup(client, db)
        url = f"/workspaces/{ws}/workflow-templates"
        pm.ok(client.post(url, json={"name": "Flow", "from_space_id": space["id"]},
                          headers=owner), 201)
        refused = client.post(url, json={"name": " FLOW ", "from_space_id": space["id"]},
                              headers=owner)
        assert refused.status_code == 409

    def test_an_invalid_definition_is_refused(self, client, db):
        owner, ws, _space, _list = _setup(client, db)
        body = {"name": "Broken", "definition": {
            "stages": [{"name": "To Do", "category": "todo"}], "initial": 0}}
        assert client.post(f"/workspaces/{ws}/workflow-templates", json=body,
                           headers=owner).status_code == 422

    def test_a_space_whose_workflow_is_not_a_valid_template_cannot_be_saved(self, client,
                                                                            db):
        """A space migrated with an old shape (here: no done stage) must be fixed by a
        publish first; a template is applied without review."""
        owner, ws, space, _list = _setup(client, db)
        db.query(WorkflowStatus).filter(WorkflowStatus.space_id == space["id"],
                                        WorkflowStatus.category == "done").update(
            {WorkflowStatus.category: "in_progress"})
        db.commit()
        refused = client.post(f"/workspaces/{ws}/workflow-templates", headers=owner,
                              json={"name": "Old", "from_space_id": space["id"]})
        assert refused.status_code == 422
        assert refused.json()["data"]["errors"]

    def test_delete_rules(self, client, db):
        owner, ws, space, _list = _setup(client, db)
        member = _person_in(db, ws, "member-1", "member")
        other = _person_in(db, ws, "member-2", "member")
        _grant(db, ws, space["id"], "member-1")
        url = f"/workspaces/{ws}/workflow-templates"
        mine = pm.ok(client.post(url, json={"name": "Mine", "from_space_id": space["id"]},
                                 headers=member), 201)
        theirs = pm.ok(client.post(url, json={"name": "Owner's",
                                              "from_space_id": space["id"]},
                                   headers=owner), 201)
        assert client.delete(f"{url}/{mine['id']}", headers=other).status_code == 403
        assert client.delete(f"{url}/{theirs['id']}", headers=member).status_code == 403
        assert pm.ok(client.delete(f"{url}/{mine['id']}", headers=member)) == {"id": mine["id"]}
        assert client.delete(f"{url}/{mine['id']}", headers=member).status_code == 404
        pm.ok(client.delete(f"{url}/{theirs['id']}", headers=owner))
        assert [t["builtin"] for t in pm.ok(client.get(url, headers=owner))] == [True] * 3
        db.expire_all()
        assert {row.deleted for row in db.query(WorkflowTemplate).all()} == {1}

    def test_the_governance_default_cannot_be_deleted(self, client, db):
        """Deleting it would leave new spaces with a default that names nothing."""
        owner, ws, space, _list = _setup(client, db)
        url = f"/workspaces/{ws}/workflow-templates"
        saved = pm.ok(client.post(url, json={"name": "House", "from_space_id": space["id"]},
                                  headers=owner), 201)
        workspace = db.query(Workspace).filter(Workspace.id == ws).one()
        settings = governance.settings_for(workspace).model_copy(
            update={"default_workflow_template": saved["id"]})
        governance.save_settings(workspace, settings)
        db.commit()
        assert client.delete(f"{url}/{saved['id']}", headers=owner).status_code == 409

    def test_another_workspaces_template_is_not_found(self, client, db):
        headers_a, ws_a, _space_a, _ = pm.setup_board(client, db, "alice", "alpha", "ALP")
        headers_b, ws_b, space_b, _ = pm.setup_board(client, db, "bob", "bravo", "BRA")
        theirs = pm.ok(client.post(f"/workspaces/{ws_b}/workflow-templates",
                                   json={"name": "Bravo", "from_space_id": space_b["id"]},
                                   headers=headers_b), 201)
        assert client.delete(f"/workspaces/{ws_a}/workflow-templates/{theirs['id']}",
                             headers=headers_a).status_code == 404
        listed = pm.ok(client.get(f"/workspaces/{ws_a}/workflow-templates",
                                  headers=headers_a))
        assert "Bravo" not in {t["name"] for t in listed}
        assert client.post(f"/workspaces/{ws_a}/workflow-templates",
                           json={"name": "Stolen", "from_space_id": space_b["id"]},
                           headers=headers_a).status_code == 404
        assert wf.resolve_template_definition(db, ws_a, theirs["id"]) is None

    def test_resolve_template_definition(self, client, db):
        """The resolver space creation uses: built-in keys with or without the prefix, a live
        template of this workspace, and nothing else."""
        owner, ws, space, _list = _setup(client, db)
        saved = pm.ok(client.post(f"/workspaces/{ws}/workflow-templates", headers=owner,
                                  json={"name": "Ours", "from_space_id": space["id"]}), 201)
        for ref in ("default", "builtin:default"):
            resolved = wf.resolve_template_definition(db, ws, ref)
            assert [s["name"] for s in resolved["stages"]] == ["To Do", "In Progress",
                                                               "In Review", "Done"]
        # A copy: a caller that changes it must not change the built-in for everyone.
        resolved["stages"].clear()
        assert wf.resolve_template_definition(db, ws, "default")["stages"]
        assert wf.resolve_template_definition(db, ws, "support")["merge_target"] is None
        assert wf.resolve_template_definition(db, ws, saved["id"]) == saved["definition"]
        assert wf.resolve_template_definition(db, ws, "nonsense") is None
        pm.ok(client.delete(f"/workspaces/{ws}/workflow-templates/{saved['id']}",
                            headers=owner))
        assert wf.resolve_template_definition(db, ws, saved["id"]) is None


# ---- editors ---------------------------------------------------------------------------------


class TestEditors:
    def test_an_admin_grants_and_revokes(self, client, db):
        owner, ws, space, _list = _setup(client, db)
        member = _person_in(db, ws, "member-1", "member")
        _person_in(db, ws, "member-2", "member")
        url = _url(ws, space["id"], "/editors")
        granted = pm.ok(client.put(url, json={"user_ids": ["member-1", "member-2",
                                                           "member-1"]}, headers=owner))
        assert sorted(e["user"]["id"] for e in granted) == ["member-1", "member-2"]
        assert {e["role"] for e in granted} == {"member"}
        assert _workflow(client, member, ws, space["id"])["can_edit"] is True
        assert len(_workflow(client, member, ws, space["id"])["editors"]) == 2

        kept = pm.ok(client.put(url, json={"user_ids": ["member-2"]}, headers=owner))
        assert [e["user"]["id"] for e in kept] == ["member-2"]
        assert _workflow(client, member, ws, space["id"])["can_edit"] is False
        assert pm.ok(client.get(url, headers=member)) == kept
        pm.ok(client.put(url, json={"user_ids": []}, headers=owner))
        assert pm.ok(client.get(url, headers=owner)) == []
        audits = db.query(ActivityLog).filter(
            ActivityLog.entity_type == "workflow_editors").all()
        assert len(audits) == 3 and all(a.entity_id == space["id"] for a in audits)

    def test_a_grant_that_stays_keeps_who_granted_it(self, client, db):
        owner, ws, space, _list = _setup(client, db)
        _person_in(db, ws, "member-1", "member")
        admin = _person_in(db, ws, "admin-1", "admin")
        url = _url(ws, space["id"], "/editors")
        pm.ok(client.put(url, json={"user_ids": ["member-1"]}, headers=owner))
        pm.ok(client.put(url, json={"user_ids": ["member-1", "admin-1"]}, headers=admin))
        db.expire_all()
        grants = {row.user_id: row.granted_by for row in db.query(SpaceWorkflowEditor)}
        assert grants == {"member-1": "owner-1", "admin-1": "admin-1"}

    def test_people_who_cannot_edit_are_named_and_nothing_changes(self, client, db):
        owner, ws, space, _list = _setup(client, db)
        _person_in(db, ws, "member-1", "member")
        _person_in(db, ws, "guest-1", "guest")
        pm.person(db, "outsider")
        url = _url(ws, space["id"], "/editors")
        refused = client.put(url, json={"user_ids": ["member-1", "guest-1", "outsider"]},
                             headers=owner)
        assert refused.status_code == 422
        body = refused.json()
        assert body["data"] == {"not_members": ["outsider"], "guests": ["guest-1"]}
        assert "outsider" in body["message"] and "Test User" in body["message"]
        assert pm.ok(client.get(url, headers=owner)) == []
        only_a_guest = client.put(url, json={"user_ids": ["guest-1"]}, headers=owner)
        assert only_a_guest.status_code == 422
        assert only_a_guest.json()["data"] == {"not_members": [], "guests": ["guest-1"]}

    def test_only_admins_grant(self, client, db):
        owner, ws, space, _list = _setup(client, db)
        member = _person_in(db, ws, "member-1", "member")
        _grant(db, ws, space["id"], "member-1")
        url = _url(ws, space["id"], "/editors")
        assert client.put(url, json={"user_ids": ["member-1"]},
                          headers=member).status_code == 403
        assert client.put(url, json={"user_ids": [f"u{i}" for i in range(51)]},
                          headers=owner).status_code == 422
        other = pm.setup_board(client, db, "bob", "bravo", "BRA")
        assert client.put(_url(ws, other[2]["id"], "/editors"), json={"user_ids": []},
                          headers=owner).status_code == 404
