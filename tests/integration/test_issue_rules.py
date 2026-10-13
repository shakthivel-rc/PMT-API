"""The issue rules of the restructuring, through the HTTP API (docs/design/09-restructuring.md).

What is pinned here, and why each matters:

* **The standard hierarchy for every relationship created or changed, and legacy ones left
  alone** (§2.1, §2.2). The second half is the one that is easy to break: a check that
  re-validates a legacy Story > Task tree on every edit would strand data the plan says must
  be reviewed by a person before it is flattened.
* **The epic's owner and completion criteria** (§2.3), both ways, and with the setting off.
* **Blocked work** (§5.3): a reason is required, and every change is an event.
* **Where a new issue starts, and where a move may go** (§4.3), including the refusal that
  names the stages an issue *can* reach.
* **WIP limits** (§4.7) under both policies, space-wide, for story/task/bug only.
* **The write guard** (§3.4): archived and frozen containers answer 423 to every write.

Legacy trees, restricted workflows, archived and frozen containers are written straight
through the `db` fixture: the routes that make them belong to other parts of the system (or
to history), and these tests are about how the issue routes treat them once they exist.
"""
from datetime import UTC, datetime

import pytest

from models.hierarchy import Folder, ProjectList, Space, WorkflowStatus, WorkflowTransition
from models.issue import Issue, IssueEvent, IssueKeyAlias
from models.workspace import Workspace
from services import issue as issue_svc
from tests import pm_fixtures as pm

pytestmark = pytest.mark.integration

OWNER = "owner-1"


# ---- helpers ------------------------------------------------------------------------------


def _patch(client, headers, ws, issue: dict, **fields):
    """PATCH with the version the caller last saw."""
    return client.patch(f"/workspaces/{ws}/issues/{issue['id']}",
                        json={"version": issue["version"], **fields}, headers=headers)


def _get(client, headers, ws, issue_id: str) -> dict:
    return pm.ok(client.get(f"/workspaces/{ws}/issues/{issue_id}", headers=headers))


def _move(client, headers, ws, issue_id: str, status_id: str, **fields):
    return client.post(f"/workspaces/{ws}/issues/{issue_id}/move",
                       json={"status_id": status_id, **fields}, headers=headers)


def _create(client, headers, ws, list_id: str, **fields):
    return client.post(f"/workspaces/{ws}/issues",
                       json={"list_id": list_id, "title": fields.pop("title", "x"), **fields},
                       headers=headers)


def _epic(client, headers, ws, list_id: str, title: str = "Epic", **fields) -> dict:
    """An epic the way the rule wants one: an owner and completion criteria."""
    return pm.issue(client, headers, ws, list_id, title, type="epic", assignee_id=OWNER,
                    completion_criteria="The outcome is measurable and met.", **fields)


def _insert(db, ws: str, space_id: str, list_id: str, issue_type: str, title: str,
            parent_id: str | None = None, **fields) -> str:
    """An issue written straight to the table — how a legacy row, made under the rules
    before 2026-10-05, gets into a test. Numbered from the space's counter so the API's next
    create cannot collide with it."""
    space = db.query(Space).filter(Space.id == space_id).one()
    space.issue_counter = space.issue_counter + 1
    status_id = fields.pop("status_id", None) or next(
        s.id for s in db.query(WorkflowStatus).filter(WorkflowStatus.space_id == space_id)
        .order_by(WorkflowStatus.position))
    issue = Issue(workspace_id=ws, space_id=space_id, list_id=list_id,
                  number=space.issue_counter, type=issue_type, title=title,
                  status_id=status_id, parent_id=parent_id, reporter_id=OWNER,
                  priority="none",
                  rank=issue_svc.bottom_rank(db, ws, space_id, status_id),
                  backlog_rank=issue_svc.bottom_backlog_rank(db, ws, space_id),
                  version=1, deleted=0, **fields)
    db.add(issue)
    db.commit()
    return str(issue.id)


def _set(db, model, row_id: str, **values) -> None:
    db.query(model).filter(model.id == row_id).update(values)
    db.commit()


def _governance(db, ws: str, **settings) -> None:
    """Write the governance document directly — its routes belong to the settings work."""
    workspace = db.query(Workspace).filter(Workspace.id == ws).one()
    document = dict(workspace.settings or {})
    document["governance"] = settings
    workspace.settings = document
    db.commit()


def _events(db, issue_id: str, event: str) -> list[IssueEvent]:
    db.expire_all()
    return (db.query(IssueEvent)
            .filter(IssueEvent.issue_id == issue_id, IssueEvent.event == event)
            .order_by(IssueEvent.created_at, IssueEvent.id).all())


def _restrict(db, ws: str, space: dict, *pairs: tuple[str | None, str]) -> None:
    """Make `space`'s workflow restricted to `pairs` of stage names (None: from anywhere)."""
    for source, target in pairs:
        db.add(WorkflowTransition(
            workspace_id=ws, space_id=space["id"],
            from_status_id=pm.status_id(space, source) if source else None,
            to_status_id=pm.status_id(space, target)))
    _set(db, Space, space["id"], transition_mode="restricted")


def _limit(db, space: dict, stage: str, limit: int, *, counts: bool = True) -> None:
    _set(db, WorkflowStatus, pm.status_id(space, stage), wip_limit=limit,
         counts_toward_wip=counts)


# ---- the hierarchy (§2) -------------------------------------------------------------------


class TestStandardHierarchy:
    def test_new_relationships_follow_the_standard_convention(self, client, db):
        """Story, Task and Bug are peers under an optional epic; a subtask sits under any of
        the three. Would catch a create still accepting the legacy shapes — a task under a
        story, a bug under a subtask — which the plan retires for new work."""
        headers, ws, _space, board_list = pm.setup_board(client, db)
        lid = board_list["id"]
        epic = _epic(client, headers, ws, lid)
        story = pm.issue(client, headers, ws, lid, type="story", parent_id=epic["id"])
        task = pm.issue(client, headers, ws, lid, type="task", parent_id=epic["id"])
        bug = pm.issue(client, headers, ws, lid, type="bug", parent_id=epic["id"])
        for parent in (story, task, bug):
            pm.issue(client, headers, ws, lid, type="subtask", parent_id=parent["id"])
        pm.issue(client, headers, ws, lid, type="bug")          # standalone
        subtask = pm.issue(client, headers, ws, lid, type="subtask", parent_id=story["id"])

        refusals = {
            ("task", story["id"]): "A task can sit under: epic.",
            ("bug", story["id"]): "A bug can sit under: epic.",
            ("bug", subtask["id"]): "A bug can sit under: epic.",
            ("bug", task["id"]): "A bug can sit under: epic.",
            ("story", story["id"]): "A story can sit under: epic.",
            ("subtask", subtask["id"]): "A subtask can sit under: bug, story, task.",
            ("subtask", epic["id"]): "A subtask can sit under: bug, story, task.",
            ("epic", epic["id"]): "An epic cannot have a parent.",
        }
        for (issue_type, parent_id), message in refusals.items():
            response = _create(client, headers, ws, lid, type=issue_type, parent_id=parent_id,
                               assignee_id=OWNER, completion_criteria="c")
            assert response.status_code == 422, (issue_type, response.text)
            assert response.json()["message"] == message
        assert _create(client, headers, ws, lid, type="subtask").json()["message"] == (
            "A subtask needs a parent.")

    def test_a_legacy_tree_stays_editable_until_its_shape_changes(self, client, db):
        """The plan's Story > Task and Bug-under-Subtask trees, written before the standard.
        Editing what they say — title, assignee, points — must keep working, and so must a
        form that sends the unchanged type and parent back; re-typing or re-parenting into a
        non-standard shape must be refused with the rule. Would catch either half: a check
        that re-validates every edit (strands the data), or one that skips changed shapes."""
        headers, ws, space, board_list = pm.setup_board(client, db)
        lid = board_list["id"]
        story = _insert(db, ws, space["id"], lid, "story", "Story")
        task = _insert(db, ws, space["id"], lid, "task", "Task under story", parent_id=story)
        subtask = _insert(db, ws, space["id"], lid, "subtask", "Subtask", parent_id=story)
        bug = _insert(db, ws, space["id"], lid, "bug", "Bug under subtask", parent_id=subtask)
        other_story = _insert(db, ws, space["id"], lid, "story", "Other story")

        for issue_id in (task, bug):
            current = _get(client, headers, ws, issue_id)
            edited = pm.ok(_patch(client, headers, ws, current, title="Renamed",
                                  assignee_id=OWNER, story_points=3,
                                  type=current["type"], parent_id=current["parent_id"]))
            assert edited["title"] == "Renamed" and edited["story_points"] == 3
            assert edited["parent_id"] == current["parent_id"]

        current = _get(client, headers, ws, task)
        refused = _patch(client, headers, ws, current, type="bug")
        assert refused.status_code == 422 and refused.json()["message"] == (
            "A bug can sit under: epic.")
        refused = _patch(client, headers, ws, current, parent_id=other_story)
        assert refused.status_code == 422 and refused.json()["message"] == (
            "A task can sit under: epic.")
        current = _get(client, headers, ws, bug)
        refused = _patch(client, headers, ws, current, parent_id=other_story)
        assert refused.status_code == 422 and refused.json()["message"] == (
            "A bug can sit under: epic.")
        current = _get(client, headers, ws, subtask)
        refused = _patch(client, headers, ws, current, type="task")
        assert refused.status_code == 422 and refused.json()["message"] == (
            "A task can sit under: epic.")
        db.expire_all()
        assert db.query(Issue).filter(Issue.id == task).one().type == "task"

    def test_a_legacy_tree_becomes_standard_by_retyping_or_reparenting(self, client, db):
        """The plan's own conversions — "review for Subtask conversion or peer-item
        reparenting" — must be possible: re-typing the task under a story into a subtask,
        and moving the bug out from under its subtask to the epic, both produce standard
        relationships. Would catch a rule that refuses any change to a legacy row."""
        headers, ws, space, board_list = pm.setup_board(client, db)
        lid = board_list["id"]
        epic = _epic(client, headers, ws, lid)
        story = _insert(db, ws, space["id"], lid, "story", "Story", parent_id=epic["id"])
        task = _insert(db, ws, space["id"], lid, "task", "Task", parent_id=story)
        subtask = _insert(db, ws, space["id"], lid, "subtask", "Subtask", parent_id=story)
        bug = _insert(db, ws, space["id"], lid, "bug", "Bug", parent_id=subtask)

        converted = pm.ok(_patch(client, headers, ws, _get(client, headers, ws, task),
                                 type="subtask"))
        assert converted["type"] == "subtask" and converted["parent_id"] == story
        moved = pm.ok(_patch(client, headers, ws, _get(client, headers, ws, bug),
                             parent_id=epic["id"]))
        assert moved["parent_id"] == epic["id"]
        detached = pm.ok(_patch(client, headers, ws, moved, parent_id=None))
        assert detached["parent_id"] is None

    def test_retyping_checks_every_child_against_the_new_type(self, client, db):
        """Re-typing changes every child's relationship to the issue, so each is checked as
        if it were being put there now, and the refusal names the child and the rule. Would
        catch a re-type that leaves a subtask sitting under an epic."""
        headers, ws, _space, board_list = pm.setup_board(client, db)
        lid = board_list["id"]
        story = pm.issue(client, headers, ws, lid, "Story", type="story", assignee_id=OWNER,
                         completion_criteria="Done when done.")
        child = pm.issue(client, headers, ws, lid, "Child", type="subtask",
                         parent_id=story["id"])
        refused = _patch(client, headers, ws, story, type="epic")
        assert refused.status_code == 422
        assert refused.json()["message"] == (
            f"{child['key']} cannot sit under an epic. A subtask can sit under: bug, story, "
            "task. Move or re-type its children first.")
        # A bug may hold subtasks, so this re-type keeps every child standard.
        assert pm.ok(_patch(client, headers, ws, story, type="bug"))["type"] == "bug"

    def test_retyping_checks_the_issue_against_its_parent(self, client, db):
        """The other half of a re-type: the issue's own relationship. A story under an epic
        re-typed to a subtask would be a subtask under an epic."""
        headers, ws, _space, board_list = pm.setup_board(client, db)
        lid = board_list["id"]
        epic = _epic(client, headers, ws, lid)
        story = pm.issue(client, headers, ws, lid, type="story", parent_id=epic["id"])
        refused = _patch(client, headers, ws, story, type="subtask")
        assert refused.status_code == 422
        assert refused.json()["message"] == "A subtask can sit under: bug, story, task."

    def test_parent_and_type_changes_are_recorded_as_events(self, client, db):
        """A reorganisation is history the reports and the inventory read (§2.4): each
        change of parent and of type is its own event with from/to, beside `updated` — and a
        PATCH that sends the same values back records neither."""
        headers, ws, _space, board_list = pm.setup_board(client, db)
        lid = board_list["id"]
        first = _epic(client, headers, ws, lid, "First")
        second = _epic(client, headers, ws, lid, "Second")
        story = pm.issue(client, headers, ws, lid, type="story", parent_id=first["id"])

        moved = pm.ok(_patch(client, headers, ws, story, parent_id=second["id"]))
        [parent_event] = _events(db, story["id"], "parent_changed")
        assert (parent_event.from_value, parent_event.to_value) == (first["id"], second["id"])
        retyped = pm.ok(_patch(client, headers, ws, moved, type="task"))
        [type_event] = _events(db, story["id"], "type_changed")
        assert (type_event.from_value, type_event.to_value) == ("story", "task")
        assert len(_events(db, story["id"], "updated")) == 2

        pm.ok(_patch(client, headers, ws, retyped, type="task", parent_id=second["id"],
                     title="Same shape"))
        assert len(_events(db, story["id"], "parent_changed")) == 1
        assert len(_events(db, story["id"], "type_changed")) == 1
        detached = pm.ok(_patch(client, headers, ws, _get(client, headers, ws, story["id"]),
                                parent_id=None))
        assert detached["parent_id"] is None
        last = _events(db, story["id"], "parent_changed")[-1]
        assert (last.from_value, last.to_value) == (second["id"], None)

    def test_the_parent_must_be_in_the_same_space_and_cycles_are_refused(self, client, db):
        """The two checks the standard does not replace. Would catch a cross-space parent —
        a relationship the reconciliation gate counts as illegal — and a cycle, which would
        make every walk of the tree loop."""
        headers, ws, _space, board_list = pm.setup_board(client, db)
        other = pm.space(client, headers, ws, "OPS")
        other_list = pm.task_list(client, headers, ws, other["id"])
        foreign_epic = _epic(client, headers, ws, other_list["id"])
        response = _create(client, headers, ws, board_list["id"], type="story",
                           parent_id=foreign_epic["id"])
        assert response.status_code == 422
        assert response.json()["message"] == "The parent must be in the same space."

        task = pm.issue(client, headers, ws, board_list["id"], type="task")
        child = pm.issue(client, headers, ws, board_list["id"], type="subtask",
                         parent_id=task["id"])
        for parent_id in (child["id"], task["id"]):
            refused = _patch(client, headers, ws, task, type="subtask", parent_id=parent_id)
            assert refused.status_code == 422
            assert refused.json()["message"] == (
                "An issue cannot sit under itself or its own children.")
        assert _patch(client, headers, ws, task,
                      parent_id="no-such-issue").status_code == 404


# ---- the epic rule (§2.3) -----------------------------------------------------------------


class TestEpicRule:
    def test_an_epic_needs_an_owner_and_completion_criteria(self, client, db):
        """The plan's creation rule for an epic. Would catch either half being dropped, and a
        criteria field of only whitespace passing for a Definition of Done."""
        headers, ws, _space, board_list = pm.setup_board(client, db)
        lid = board_list["id"]
        cases = [
            ({}, "An epic needs an owner and completion criteria"),
            ({"completion_criteria": "Refunds work"}, "An epic needs an owner:"),
            ({"assignee_id": OWNER}, "An epic needs completion criteria:"),
            ({"assignee_id": OWNER, "completion_criteria": "   "},
             "An epic needs completion criteria:"),
        ]
        for fields, message in cases:
            response = _create(client, headers, ws, lid, type="epic", **fields)
            assert response.status_code == 422, fields
            assert response.json()["message"].startswith(message), response.text
        epic = pm.issue(client, headers, ws, lid, type="epic", assignee_id=OWNER,
                        completion_criteria="Refunds reach the original card in a day.")
        assert epic["completion_criteria"] == "Refunds reach the original card in a day."
        assert epic["assignee"]["id"] == OWNER

    def test_retyping_into_an_epic_needs_both_in_the_resulting_state(self, client, db):
        """A story that becomes an epic becomes an outcome, so it needs what an epic needs —
        from the body or already on the issue. Would catch a rule enforced only on create."""
        headers, ws, _space, board_list = pm.setup_board(client, db)
        lid = board_list["id"]
        story = pm.issue(client, headers, ws, lid, type="story", assignee_id=OWNER)
        refused = _patch(client, headers, ws, story, type="epic")
        assert refused.status_code == 422
        assert refused.json()["message"].startswith("An epic needs completion criteria")
        epic = pm.ok(_patch(client, headers, ws, story, type="epic",
                            completion_criteria="Customers can refund themselves."))
        assert epic["type"] == "epic"

        task = pm.issue(client, headers, ws, lid, type="task", completion_criteria="Shipped.")
        refused = _patch(client, headers, ws, task, type="epic")
        assert refused.json()["message"].startswith("An epic needs an owner")
        assert pm.ok(_patch(client, headers, ws, task, type="epic",
                            assignee_id=OWNER))["type"] == "epic"

    def test_an_epic_cannot_lose_its_owner_or_its_criteria(self, client, db):
        """Unassigning an epic or blanking its criteria would make a compliant epic into the
        ownerless bucket the rule exists to prevent; editing either stays free."""
        headers, ws, _space, board_list = pm.setup_board(client, db)
        pm.person(db, "member-1")
        pm.join(db, ws, "member-1", "member")
        epic = _epic(client, headers, ws, board_list["id"])
        refused = _patch(client, headers, ws, epic, assignee_id=None)
        assert refused.status_code == 422 and "must keep an owner" in refused.json()["message"]
        for blank in ("", "  ", None):
            refused = _patch(client, headers, ws, epic, completion_criteria=blank)
            assert refused.status_code == 422, blank
            assert "cannot be removed" in refused.json()["message"]
        edited = pm.ok(_patch(client, headers, ws, epic, assignee_id="member-1",
                              completion_criteria="A sharper finish line."))
        assert edited["assignee"]["id"] == "member-1"
        assert edited["completion_criteria"] == "A sharper finish line."

    def test_a_legacy_epic_without_either_stays_editable(self, client, db):
        """Epics written before the rule have no owner or criteria; the plan keeps them
        until someone reviews them, and the inventory counts them. Would catch a rule that
        refuses every edit of such an epic."""
        headers, ws, space, board_list = pm.setup_board(client, db)
        legacy = _insert(db, ws, space["id"], board_list["id"], "epic", "Old bucket")
        current = _get(client, headers, ws, legacy)
        edited = pm.ok(_patch(client, headers, ws, current, title="Reviewed bucket",
                              priority="high", assignee_id=None, completion_criteria=None))
        assert edited["title"] == "Reviewed bucket" and edited["priority"] == "high"
        owned = pm.ok(_patch(client, headers, ws, edited, assignee_id=OWNER))
        assert owned["assignee"]["id"] == OWNER

    def test_with_the_setting_off_an_epic_needs_neither(self, client, db):
        """`require_epic_owner_and_criteria` is the workspace's choice (§3.3). Would catch the
        rule ignoring it."""
        headers, ws, _space, board_list = pm.setup_board(client, db)
        _governance(db, ws, require_epic_owner_and_criteria=False)
        epic = pm.issue(client, headers, ws, board_list["id"], type="epic")
        owned = pm.ok(_patch(client, headers, ws, epic, assignee_id=OWNER,
                             completion_criteria="c"))
        unowned = pm.ok(_patch(client, headers, ws, owned, assignee_id=None,
                               completion_criteria=""))
        assert unowned["assignee"] is None
        story = pm.issue(client, headers, ws, board_list["id"], type="story")
        assert pm.ok(_patch(client, headers, ws, story, type="epic"))["type"] == "epic"


# ---- blocked (§5.3) -----------------------------------------------------------------------


class TestBlocked:
    def test_blocking_needs_a_reason_and_records_who_when_and_why(self, client, db):
        """A blocked flag with no reason is the one the daily inspection cannot act on.
        Would catch a missing reason being accepted, a missing event, the who/when not being
        stamped, or blocking slipping past the optimistic version."""
        headers, ws, _space, board_list = pm.setup_board(client, db)
        issue = pm.issue(client, headers, ws, board_list["id"], type="story")
        for body in ({"blocked": True}, {"blocked": True, "blocked_reason": "   "}):
            refused = _patch(client, headers, ws, issue, **body)
            assert refused.status_code == 422, body
            assert refused.json()["message"].startswith("A blocked issue needs a reason")
        assert _patch(client, headers, ws, issue, blocked=None).status_code == 422

        reason = "Waiting on sandbox keys; Priya chases the provider; retry Thursday. " * 10
        blocked = pm.ok(_patch(client, headers, ws, issue, blocked=True,
                               blocked_reason=reason))
        assert blocked["blocked"] is True and blocked["blocked_reason"] == reason.strip()
        assert blocked["blocked_by"]["id"] == OWNER and blocked["blocked_at"] is not None
        assert blocked["version"] == issue["version"] + 1
        [event] = _events(db, issue["id"], "blocked")
        assert event.actor_id == OWNER
        assert event.to_value == reason.strip()[:255]
        card = pm.board(client, headers, ws, board_list["id"])["columns"][0]["cards"][0]
        assert card["blocked"] is True and card["blocked_reason"] == reason.strip()

    def test_a_new_reason_while_blocked_is_recorded_and_keeps_the_original_time(self, client,
                                                                                db):
        """The reason changes as the blocker moves on; each change is history. The age of
        the blocker — `blocked_at` — must not reset with it."""
        headers, ws, _space, board_list = pm.setup_board(client, db)
        issue = pm.issue(client, headers, ws, board_list["id"])
        blocked = pm.ok(_patch(client, headers, ws, issue, blocked=True,
                               blocked_reason="Waiting on legal"))
        again = pm.ok(_patch(client, headers, ws, blocked, blocked_reason="Legal replied; "
                                                                          "waiting on finance"))
        assert again["blocked_reason"] == "Legal replied; waiting on finance"
        assert again["blocked_at"] == blocked["blocked_at"]
        assert [e.to_value for e in _events(db, issue["id"], "blocked")] == [
            "Waiting on legal", "Legal replied; waiting on finance"]
        # The same reason again is not news.
        pm.ok(_patch(client, headers, ws, again, blocked=True,
                     blocked_reason="Legal replied; waiting on finance"))
        assert len(_events(db, issue["id"], "blocked")) == 2

    def test_unblocking_clears_the_flag_reason_and_stamp(self, client, db):
        """Unblocked work carries no stale reason, and the change is an event."""
        headers, ws, _space, board_list = pm.setup_board(client, db)
        issue = pm.issue(client, headers, ws, board_list["id"])
        blocked = pm.ok(_patch(client, headers, ws, issue, blocked=True,
                               blocked_reason="Waiting on design"))
        clear = pm.ok(_patch(client, headers, ws, blocked, blocked=False))
        assert clear["blocked"] is False and clear["blocked_reason"] is None
        assert clear["blocked_at"] is None and clear["blocked_by"] is None
        [event] = _events(db, issue["id"], "unblocked")
        assert event.from_value == "Waiting on design"
        db.expire_all()
        row = db.query(Issue).filter(Issue.id == issue["id"]).one()
        assert (row.blocked, row.blocked_reason, row.blocked_at, row.blocked_by_id) == (
            False, None, None, None)

    def test_a_reason_belongs_only_to_blocked_work(self, client, db):
        """A reason on unblocked work would be a blocker nobody sees, and a blocked issue
        cannot have its reason blanked out from under it."""
        headers, ws, _space, board_list = pm.setup_board(client, db)
        issue = pm.issue(client, headers, ws, board_list["id"])
        refused = _patch(client, headers, ws, issue, blocked_reason="Not actually blocked")
        assert refused.status_code == 422
        assert refused.json()["message"].startswith("Only a blocked issue")
        refused = _patch(client, headers, ws, issue, blocked=False, blocked_reason="x")
        assert refused.status_code == 422
        # Clearing a reason that is not there is harmless.
        cleared = pm.ok(_patch(client, headers, ws, issue, blocked=False, blocked_reason=None))
        blocked = pm.ok(_patch(client, headers, ws, cleared, blocked=True,
                               blocked_reason="Real blocker"))
        refused = _patch(client, headers, ws, blocked, blocked_reason="")
        assert refused.status_code == 422


# ---- where issues start and where they may go (§4.3) --------------------------------------


class TestCreateStage:
    def test_new_issues_start_in_the_spaces_initial_stage(self, client, db):
        """The space chooses its initial stage; it is not "the first todo column" any more.
        Would catch create ignoring `initial_status_id`."""
        headers, ws, space, board_list = pm.setup_board(client, db)
        _set(db, Space, space["id"], initial_status_id=pm.status_id(space, "In Progress"))
        issue = pm.issue(client, headers, ws, board_list["id"])
        assert issue["status_id"] == pm.status_id(space, "In Progress")
        assert issue["status_entered_at"] is not None

    def test_a_named_stage_must_be_a_live_stage_of_the_lists_space(self, client, db):
        """A stage of another workflow, or one that was removed, is never a place to create
        into; in a free workflow any live stage is (the board's quick-add)."""
        headers, ws, space, board_list = pm.setup_board(client, db)
        other = pm.space(client, headers, ws, "OPS")
        lid = board_list["id"]
        foreign = _create(client, headers, ws, lid, status_id=pm.status_id(other, "To Do"))
        assert foreign.status_code == 422
        _set(db, WorkflowStatus, pm.status_id(space, "In Review"), deleted=1,
             deleted_at=datetime.now(UTC))
        removed = _create(client, headers, ws, lid, status_id=pm.status_id(space, "In Review"))
        assert removed.status_code == 422
        done = pm.issue(client, headers, ws, lid, status_id=pm.status_id(space, "Done"))
        assert done["status_id"] == pm.status_id(space, "Done")

    def test_a_restricted_workflow_creates_only_into_its_initial_stage(self, client, db):
        """Jira's rule, and §4.3's: in a restricted flow an issue enters at the start and
        moves along the transitions; creating it halfway would skip them."""
        headers, ws, space, board_list = pm.setup_board(client, db)
        _restrict(db, ws, space, ("To Do", "In Progress"))
        refused = _create(client, headers, ws, board_list["id"],
                          status_id=pm.status_id(space, "In Progress"))
        assert refused.status_code == 422
        assert refused.json()["message"] == "New issues start in To Do in this workflow."
        named = pm.issue(client, headers, ws, board_list["id"],
                         status_id=pm.status_id(space, "To Do"))
        default = pm.issue(client, headers, ws, board_list["id"])
        assert named["status_id"] == default["status_id"] == pm.status_id(space, "To Do")


class TestRestrictedMoves:
    def test_a_move_outside_the_transitions_is_refused_naming_where_it_can_go(self, client,
                                                                               db):
        """A refusal with no next step is the least useful thing a board can say; this one
        names the allowed destinations. Would catch the board move bypassing the workflow
        the webhook obeys."""
        headers, ws, space, board_list = pm.setup_board(client, db)
        _restrict(db, ws, space, ("To Do", "In Progress"), ("In Progress", "In Review"),
                  ("In Review", "In Progress"), ("In Review", "Done"))
        issue = pm.issue(client, headers, ws, board_list["id"])
        refused = _move(client, headers, ws, issue["id"], pm.status_id(space, "Done"))
        assert refused.status_code == 422
        assert refused.json()["message"] == (
            "To Do → Done is not an allowed move in this workflow. From To Do an issue can "
            "move to: In Progress.")
        pm.ok(_move(client, headers, ws, issue["id"], pm.status_id(space, "In Progress")))
        refused = _move(client, headers, ws, issue["id"], pm.status_id(space, "Done"))
        assert refused.json()["message"].endswith(
            "From In Progress an issue can move to: In Review.")
        # Reordering inside a stage is always allowed.
        pm.ok(_move(client, headers, ws, issue["id"], pm.status_id(space, "In Progress")))
        db.expire_all()
        row = db.query(Issue).filter(Issue.id == issue["id"]).one()
        assert row.status_id == pm.status_id(space, "In Progress")

    def test_a_global_transition_leaves_every_stage(self, client, db):
        """`from_status_id` NULL means "from any stage" — the way a Canceled stage is reached
        from anywhere."""
        headers, ws, space, board_list = pm.setup_board(client, db)
        _restrict(db, ws, space, ("To Do", "In Progress"), (None, "Done"))
        issue = pm.issue(client, headers, ws, board_list["id"])
        moved = pm.ok(_move(client, headers, ws, issue["id"], pm.status_id(space, "Done")))
        assert moved["status_id"] == pm.status_id(space, "Done")

    def test_a_free_workflow_ignores_stored_transitions(self, client, db):
        """Transitions are kept while the mode is `any`, and imposed nothing; column order
        never imposes anything either."""
        headers, ws, space, board_list = pm.setup_board(client, db)
        _restrict(db, ws, space, ("To Do", "In Progress"))
        _set(db, Space, space["id"], transition_mode="any")
        issue = pm.issue(client, headers, ws, board_list["id"])
        pm.ok(_move(client, headers, ws, issue["id"], pm.status_id(space, "Done")))
        pm.ok(_move(client, headers, ws, issue["id"], pm.status_id(space, "To Do")))

    def test_a_removed_stage_is_not_a_destination(self, client, db):
        """A soft-deleted stage keeps its row for history; nothing may move into it."""
        headers, ws, space, board_list = pm.setup_board(client, db)
        issue = pm.issue(client, headers, ws, board_list["id"])
        _set(db, WorkflowStatus, pm.status_id(space, "In Review"), deleted=1)
        refused = _move(client, headers, ws, issue["id"], pm.status_id(space, "In Review"))
        assert refused.status_code == 422


# ---- WIP (§4.7) ---------------------------------------------------------------------------


class TestWip:
    def test_warn_lets_the_move_through_and_says_the_stage_is_over(self, client, db):
        """The default policy: the move succeeds and the answer says by how much. Would catch
        a warn that blocks, or one that says nothing."""
        headers, ws, space, board_list = pm.setup_board(client, db)
        _limit(db, space, "In Review", 2)
        review = pm.status_id(space, "In Review")
        issues = [pm.issue(client, headers, ws, board_list["id"], f"S{n}", type="story")
                  for n in range(3)]
        for issue in issues[:2]:
            assert _move(client, headers, ws, issue["id"], review).json()["message"] == (
                "Issue moved")
        over = _move(client, headers, ws, issues[2]["id"], review)
        assert over.status_code == 200
        assert over.json()["message"] == (
            "Issue moved. In Review is over its WIP limit (3 of 2).")
        assert over.json()["data"]["status_id"] == review

    def test_block_refuses_the_move_with_the_limit_in_the_message(self, client, db):
        """`block`: a full stage takes nothing more, and the issue stays where it was."""
        headers, ws, space, board_list = pm.setup_board(client, db)
        _limit(db, space, "In Progress", 1)
        _set(db, Space, space["id"], wip_policy="block")
        doing = pm.status_id(space, "In Progress")
        first = pm.issue(client, headers, ws, board_list["id"], "First", type="task")
        second = pm.issue(client, headers, ws, board_list["id"], "Second", type="bug")
        pm.ok(_move(client, headers, ws, first["id"], doing))
        refused = _move(client, headers, ws, second["id"], doing)
        assert refused.status_code == 409
        assert refused.json()["message"] == (
            "In Progress is at its WIP limit of 1. Finish or move something out of it first.")
        db.expire_all()
        assert db.query(Issue).filter(Issue.id == second["id"]).one().status_id == (
            pm.status_id(space, "To Do"))

    def test_only_story_task_and_bug_count_and_a_reorder_is_never_checked(self, client, db):
        """Epics are outcomes and subtasks are parts of an item already counted; a reorder
        inside a full stage adds no work in progress. Would catch WIP counting every type,
        or refusing a reorder in a full column."""
        headers, ws, space, board_list = pm.setup_board(client, db)
        lid = board_list["id"]
        _limit(db, space, "In Progress", 1)
        _set(db, Space, space["id"], wip_policy="block")
        doing = pm.status_id(space, "In Progress")
        epic = _epic(client, headers, ws, lid)
        story = pm.issue(client, headers, ws, lid, type="story", parent_id=epic["id"])
        subtask = pm.issue(client, headers, ws, lid, type="subtask", parent_id=story["id"])
        pm.ok(_move(client, headers, ws, epic["id"], doing))
        pm.ok(_move(client, headers, ws, subtask["id"], doing))
        pm.ok(_move(client, headers, ws, story["id"], doing))
        late = pm.issue(client, headers, ws, lid, type="story")
        assert _move(client, headers, ws, late["id"], doing).status_code == 409
        # A stage already over its limit — the limit was lowered under it — still lets its
        # own cards be reordered: the work in progress does not change.
        _limit(db, space, "In Progress", 2)
        pm.ok(_move(client, headers, ws, late["id"], doing))
        _limit(db, space, "In Progress", 1)
        pm.ok(_move(client, headers, ws, story["id"], doing, after_id=late["id"]))
        pm.ok(_move(client, headers, ws, late["id"], doing, before_id=subtask["id"]))

    def test_the_count_is_space_wide_and_a_stage_outside_wip_has_no_limit(self, client, db):
        """WIP is the team's, across every list of the space. And a limit on a stage that
        does not count toward WIP is never enforced."""
        headers, ws, space, board_list = pm.setup_board(client, db)
        second_list = pm.task_list(client, headers, ws, space["id"], "Support")
        _limit(db, space, "In Review", 1)
        _limit(db, space, "Done", 1, counts=False)
        _set(db, Space, space["id"], wip_policy="block")
        review, done = pm.status_id(space, "In Review"), pm.status_id(space, "Done")
        here = pm.issue(client, headers, ws, board_list["id"], type="task")
        there = pm.issue(client, headers, ws, second_list["id"], type="task")
        pm.ok(_move(client, headers, ws, here["id"], review))
        assert _move(client, headers, ws, there["id"], review).status_code == 409
        pm.ok(_move(client, headers, ws, there["id"], done))
        pm.ok(_move(client, headers, ws, here["id"], done))

    def test_creating_into_a_full_stage_is_held_to_the_limit(self, client, db):
        """Creating straight into a stage is entering it; without this the board's quick-add
        would be the way around a `block` policy."""
        headers, ws, space, board_list = pm.setup_board(client, db)
        lid = board_list["id"]
        _limit(db, space, "In Progress", 1)
        doing = pm.status_id(space, "In Progress")
        pm.issue(client, headers, ws, lid, type="story", status_id=doing)
        warned = _create(client, headers, ws, lid, type="story", status_id=doing)
        assert warned.status_code == 201
        assert warned.json()["message"] == (
            "Issue created. In Progress is over its WIP limit (2 of 1).")
        _set(db, Space, space["id"], wip_policy="block")
        refused = _create(client, headers, ws, lid, type="story", status_id=doing)
        assert refused.status_code == 409
        assert pm.issue(client, headers, ws, lid, type="subtask", status_id=doing,
                        parent_id=warned.json()["data"]["id"])["status_id"] == doing


# ---- the write guard (§3.4) ---------------------------------------------------------------


class TestWriteGuard:
    def test_an_archived_list_refuses_every_issue_write_and_still_reads(self, client, db):
        """Archived is read-only for good: every write the issue routes make answers 423 with
        a message naming the list; every read still answers. Would catch one route that
        forgot to ask the guard."""
        headers, ws, space, board_list = pm.setup_board(client, db)
        lid = board_list["id"]
        issue = pm.issue(client, headers, ws, lid)
        other = pm.issue(client, headers, ws, lid)
        _set(db, ProjectList, lid, archived_at=datetime.now(UTC))
        base = f"/workspaces/{ws}/issues/{issue['id']}"
        writes = [
            _create(client, headers, ws, lid),
            _patch(client, headers, ws, issue, title="No"),
            _move(client, headers, ws, issue["id"], pm.status_id(space, "Done")),
            client.post(f"{base}/backlog-rank", json={"after_id": other["id"]},
                        headers=headers),
            client.post(f"{base}/comments", json={"body": "hello"}, headers=headers),
            client.delete(base, headers=headers),
        ]
        for response in writes:
            assert response.status_code == 423, response.request.url
            assert response.json()["message"] == (
                "The list Backlog is archived, so it is read-only. An admin can unarchive it.")
        assert client.get(base, headers=headers).status_code == 200
        assert client.get(f"{base}/comments", headers=headers).status_code == 200
        assert pm.board(client, headers, ws, lid)["total"] == 2

    def test_a_frozen_space_refuses_writes_and_says_why(self, client, db):
        """A space frozen for a migration wave pauses every write in it, and the message
        carries the freeze reason so a person knows it is temporary."""
        headers, ws, space, board_list = pm.setup_board(client, db)
        issue = pm.issue(client, headers, ws, board_list["id"])
        _set(db, Space, space["id"], frozen_at=datetime.now(UTC), frozen_by=OWNER,
             freeze_reason="wave 2 of the reorganisation")
        for response in (_create(client, headers, ws, board_list["id"]),
                         _patch(client, headers, ws, issue, title="No")):
            assert response.status_code == 423
            assert response.json()["message"] == (
                "The space PAY space is frozen while it is being reorganised (wave 2 of the "
                "reorganisation). Changes are paused until it is unfrozen.")

    def test_an_archived_folder_locks_its_lists(self, client, db):
        """The guard reads up the containment chain: list, folder, space."""
        headers, ws, space, _board_list = pm.setup_board(client, db)
        folder = pm.ok(client.post(f"/workspaces/{ws}/spaces/{space['id']}/folders",
                                   json={"name": "Q4"}, headers=headers), 201)
        inner = pm.ok(client.post(f"/workspaces/{ws}/lists",
                                  json={"space_id": space["id"], "folder_id": folder["id"],
                                        "name": "Inner"}, headers=headers), 201)
        _set(db, Folder, folder["id"], archived_at=datetime.now(UTC))
        refused = _create(client, headers, ws, inner["id"])
        assert refused.status_code == 423
        assert refused.json()["message"].startswith("The folder Q4 is archived")


# ---- the projection (§5) ------------------------------------------------------------------


class TestProjection:
    def test_the_card_and_the_detail_carry_the_new_fields(self, client, db):
        """The SPA draws a blocked badge, the stage age and the backlog order from the card,
        and the drawer the criteria, who blocked it and the old keys. Would catch a field
        dropped from the projection."""
        headers, ws, space, board_list = pm.setup_board(client, db)
        first = pm.issue(client, headers, ws, board_list["id"], "First")
        second = pm.issue(client, headers, ws, board_list["id"], "Second",
                          completion_criteria="Written down.")
        for card in (first, second):
            assert card["blocked"] is False and card["blocked_reason"] is None
            assert card["status_entered_at"] is not None
            assert card["previous_keys"] == [] and card["blocked_by"] is None
        assert second["completion_criteria"] == "Written down."
        assert first["backlog_rank"] < second["backlog_rank"]

        db.add(IssueKeyAlias(workspace_id=ws, issue_id=second["id"], space_id=space["id"],
                             space_key="OLD", number=3,
                             created_at=datetime(2026, 1, 1, tzinfo=UTC)))
        db.add(IssueKeyAlias(workspace_id=ws, issue_id=second["id"], space_id=space["id"],
                             space_key="MID", number=9,
                             created_at=datetime(2026, 6, 1, tzinfo=UTC)))
        db.commit()
        assert _get(client, headers, ws, second["id"])["previous_keys"] == ["OLD-3", "MID-9"]

    def test_the_stage_age_restarts_on_a_stage_change_only(self, client, db):
        """`status_entered_at` is the work-item age: a move to another stage restarts it, a
        reorder inside the stage does not."""
        headers, ws, space, board_list = pm.setup_board(client, db)
        issue = pm.issue(client, headers, ws, board_list["id"])
        old = datetime(2026, 1, 1)
        _set(db, Issue, issue["id"], status_entered_at=old)
        reordered = pm.ok(_move(client, headers, ws, issue["id"], issue["status_id"]))
        assert reordered["status_entered_at"].startswith("2026-01-01")
        moved = pm.ok(_move(client, headers, ws, issue["id"],
                            pm.status_id(space, "In Progress")))
        assert not moved["status_entered_at"].startswith("2026-01-01")
