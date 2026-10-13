"""The GitHub merge action, old keys, deferral and replay (09-restructuring.md §6, §7.3).

`test_github_webhook.py` holds the webhook's authentication and the behaviour every space had
before 2026-10-05 — a merge moves linked work to Done. This module holds what changed:

* a space chooses what a merge does — a stage of any category, or only the link — and a
  merge that cannot do it fails **visibly** (an event, the key in `failed`, a warning) instead
  of falling back to some other completed stage;
* an old key — from an issue that moved, or a space that was re-keyed — still finds the issue;
* a delivery naming an issue in a frozen space is held back whole and replayed, exactly once,
  when the space is unfrozen; archived containers are skipped issue by issue.

Workflows are shaped by writing rows through the `db` fixture rather than through the
workflow editor's routes: building a workflow is that editor's job and its own tests'; these
only need one to exist. Deliveries are signed exactly as the webhook tests sign them.
"""
import dataclasses
import hashlib
import hmac
import json
import logging
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func

import routes.integrations as webhook_route
from controllers import github_controller
from core.config import get_settings
from models.hierarchy import ProjectList, Space, SpaceKeyAlias, WorkflowStatus, WorkflowTransition
from models.issue import Issue, IssueEvent, IssueKeyAlias
from models.vcs import VcsLink, WebhookDelivery
from tests import pm_fixtures as pm

pytestmark = pytest.mark.integration

SECRET = "whsec-for-tests"
URL = "/integrations/github/webhook"
REPO = "acme/payments"


@pytest.fixture()
def secret(monkeypatch):
    configured = dataclasses.replace(get_settings(), github_webhook_secret=SECRET)
    monkeypatch.setattr(webhook_route, "get_settings", lambda: configured)


def _deliver(client, payload, *, delivery=None):
    body = json.dumps(payload).encode()
    digest = hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest()
    headers = {
        "Content-Type": "application/json",
        "X-GitHub-Event": "pull_request",
        "X-GitHub-Delivery": delivery or str(uuid.uuid4()),
        "X-Hub-Signature-256": f"sha256={digest}",
    }
    return client.post(URL, content=body, headers=headers)


def _pr(action="opened", *, merged=False, state="open", title="PAY-1 refunds",
        branch="feature/refunds", number=7):
    """A pull_request payload. The branch names no key unless a test says so, so the title
    alone decides which issues a delivery reaches."""
    return {
        "action": action,
        "repository": {"full_name": REPO},
        "pull_request": {"number": number, "title": title, "body": "", "state": state,
                         "merged": merged, "html_url": f"https://github.com/{REPO}/pull/{number}",
                         "head": {"ref": branch}, "user": {"login": "octocat"}},
    }


def _merged(title="PAY-1 refunds", **fields):
    return _pr("closed", merged=True, state="closed", title=title, **fields)


def _team(client, db, *, user="owner-1", slug="acme", key="PAY"):
    """An owner, a workspace with one space and one list, and the repository linked to it."""
    headers, ws, space, board_list = pm.setup_board(client, db, user, slug, key)
    pm.ok(client.post(f"/workspaces/{ws}/repositories", json={"full_name": REPO},
                      headers=headers), 201)
    return headers, ws, space, board_list


def _other_space(client, headers, ws, key="OPS"):
    space = pm.space(client, headers, ws, key)
    return space, pm.task_list(client, headers, ws, space["id"], f"{key} work")


def _set_space(db, space_id, **values):
    db.query(Space).filter(Space.id == space_id).update(values)
    db.commit()


def _freeze(db, space_id):
    _set_space(db, space_id, frozen_at=datetime.now(UTC), freeze_reason="wave 1")


def _unfreeze(db, space_id):
    _set_space(db, space_id, frozen_at=None, freeze_reason=None)


def _add_stage(db, space_id, name, category) -> str:
    workspace_id = db.query(Space.workspace_id).filter(Space.id == space_id).scalar()
    last = db.query(func.max(WorkflowStatus.position)).filter(
        WorkflowStatus.space_id == space_id).scalar()
    stage = WorkflowStatus(workspace_id=workspace_id, space_id=space_id, name=name,
                           category=category, position=last + 1)
    db.add(stage)
    db.commit()
    return str(stage.id)


def _restrict(db, space_id, pairs):
    """Switch the space to a restricted flow allowing only `pairs` (from, to)."""
    workspace_id = db.query(Space.workspace_id).filter(Space.id == space_id).scalar()
    db.add_all(WorkflowTransition(workspace_id=workspace_id, space_id=space_id,
                                  from_status_id=source, to_status_id=target)
               for source, target in pairs)
    db.query(Space).filter(Space.id == space_id).update({"transition_mode": "restricted"})
    db.commit()


def _move(client, headers, ws, issue_id, status_id):
    return pm.ok(client.post(f"/workspaces/{ws}/issues/{issue_id}/move",
                             json={"status_id": status_id}, headers=headers))


def _status(db, issue_id) -> str:
    return db.query(Issue.status_id).filter(Issue.id == issue_id).scalar()


def _events(db, kind, **filters) -> list[IssueEvent]:
    db.expire_all()
    query = db.query(IssueEvent).filter(IssueEvent.event == kind)
    for name, value in filters.items():
        query = query.filter(getattr(IssueEvent, name) == value)
    return query.all()


def _links(db, issue_id) -> list[VcsLink]:
    db.expire_all()
    return db.query(VcsLink).filter(VcsLink.issue_id == issue_id).all()


def _row(db, delivery_id) -> WebhookDelivery:
    db.expire_all()
    return db.query(WebhookDelivery).filter(WebhookDelivery.id == delivery_id).one()


def _received(db, delivery_id, when):
    db.query(WebhookDelivery).filter(WebhookDelivery.id == delivery_id).update(
        {"received_at": when})
    db.commit()


class TestOldKeys:
    def test_an_issue_key_alias_links_and_moves_the_issue_where_it_lives_now(self, client, db,
                                                                             secret):
        """PAY-7 moved to OPS and is OPS-1 now; the branch was cut before the move. Without
        `key_resolution` the webhook looks for a live PAY-7, finds none and drops the link —
        the "GitHub references keep working" migration gate. The merge is OPS's action."""
        headers, ws, pay, _ = _team(client, db)
        ops, ops_list = _other_space(client, headers, ws)
        moved = pm.issue(client, headers, ws, ops_list["id"], title="Refunds")
        db.add(IssueKeyAlias(workspace_id=ws, issue_id=moved["id"], space_id=pay["id"],
                             space_key="PAY", number=7))
        db.commit()

        result = pm.ok(_deliver(client, _merged(title="PAY-7 refunds")))
        assert result["linked"] == ["OPS-1"]
        assert result["moved"] == ["OPS-1"] and result["moved_to_done"] == ["OPS-1"]
        assert [link.external_id for link in _links(db, moved["id"])] == ["acme/payments#7"]
        assert _status(db, moved["id"]) == pm.status_id(ops, "Done")

    def test_a_space_key_alias_links_the_issue_of_the_re_keyed_space(self, client, db, secret):
        """PAY was renamed PMT, and PAY-1 is PMT-1 now. The old prefix resolves through
        `space_key_aliases`; the result names the issue by the key it has today."""
        headers, ws, space, board_list = _team(client, db)
        issue = pm.issue(client, headers, ws, board_list["id"], title="Refunds")
        db.query(Space).filter(Space.id == space["id"]).update({"key": "PMT"})
        db.add(SpaceKeyAlias(workspace_id=ws, space_id=space["id"], key="PAY"))
        db.commit()

        result = pm.ok(_deliver(client, _pr(title="PAY-1 refunds")))
        assert result["linked"] == ["PMT-1"]
        assert len(_links(db, issue["id"])) == 1

    def test_an_old_and_a_new_key_of_one_issue_are_one_link_and_one_move(self, client, db,
                                                                         secret):
        """The branch says PAY-7, the title says OPS-1: one issue. Without deduplication by
        issue id it is linked twice in the answer and its merge action runs twice."""
        headers, ws, pay, _ = _team(client, db)
        _ops, ops_list = _other_space(client, headers, ws)
        moved = pm.issue(client, headers, ws, ops_list["id"], title="Refunds")
        db.add(IssueKeyAlias(workspace_id=ws, issue_id=moved["id"], space_id=pay["id"],
                             space_key="PAY", number=7))
        db.commit()

        result = pm.ok(_deliver(client, _merged(title="OPS-1 refunds",
                                                branch="feature/pay-7-refunds")))
        assert result["linked"] == ["OPS-1"] and result["moved"] == ["OPS-1"]
        assert len(_links(db, moved["id"])) == 1
        assert len(_events(db, "linked")) == 1
        assert len(_events(db, "status_changed")) == 1


class TestMergeAction:
    def test_link_only_links_and_moves_nothing(self, client, db, secret):
        """`merge_action = none` is the support template's choice: support work is not always
        code. A merge must still refresh the link and must not touch the status."""
        headers, ws, space, board_list = _team(client, db)
        issue = pm.issue(client, headers, ws, board_list["id"], title="Refunds")
        _set_space(db, space["id"], merge_action="none", merge_target_status_id=None)

        pm.ok(_deliver(client, _pr()))
        result = pm.ok(_deliver(client, _merged()))
        assert result["handled"] is True and result["linked"] == ["PAY-1"]
        assert result["moved"] == [] and result["moved_to_done"] == [] and result["failed"] == []
        assert [link.state for link in _links(db, issue["id"])] == ["merged"]
        assert _status(db, issue["id"]) == pm.status_id(space, "To Do")
        assert _events(db, "status_changed") == [] and _events(db, "automation_failed") == []

    def test_an_applied_delivery_is_recorded_as_processed_with_its_workspace(self, client, db,
                                                                            secret):
        """The delivery row is what the inventory and an operator read: which tenant it was
        for, what became of it and when. A row without its workspace cannot be found by the
        workspace's replay or listing."""
        _headers, ws, _space, _list = _team(client, db)
        pm.ok(_deliver(client, _pr(), delivery="applied-1"))
        row = _row(db, "applied-1")
        assert (row.status, row.workspace_id, row.payload) == ("processed", ws, None)
        assert row.processed_at is not None

    def test_a_qa_target_moves_to_qa_and_only_done_moves_count_as_done(self, client, db,
                                                                       secret):
        """The plan's migration gate: a team that accepts work in QA after the merge must not
        see it marked delivered at the merge. PAY merges into QA (in progress), OPS into Done,
        and one pull request names both: both are `moved`, only OPS-1 is `moved_to_done`."""
        headers, ws, space, board_list = _team(client, db)
        _ops, ops_list = _other_space(client, headers, ws)
        pay_issue = pm.issue(client, headers, ws, board_list["id"], title="Refunds")
        pm.issue(client, headers, ws, ops_list["id"], title="Runbook")
        qa = _add_stage(db, space["id"], "QA", "in_progress")
        _set_space(db, space["id"], merge_target_status_id=qa)

        result = pm.ok(_deliver(client, _merged(title="PAY-1 OPS-1 refunds")))
        assert result["moved"] == ["PAY-1", "OPS-1"]
        assert result["moved_to_done"] == ["OPS-1"]
        board = pm.board(client, headers, ws, board_list["id"])
        assert pm.column_titles(board, "QA") == ["Refunds"]
        assert pm.column_titles(board, "Done") == []

        # Already at the target: another merged pull request leaves it alone.
        again = pm.ok(_deliver(client, _merged(title="PAY-1 follow-up", number=8)))
        assert again["linked"] == ["PAY-1"] and again["moved"] == [] and again["failed"] == []
        assert len(_events(db, "status_changed", issue_id=pay_issue["id"])) == 1

    def test_closed_work_is_never_reopened_or_re_closed(self, client, db, secret):
        """A merge into QA must not drag Done or Canceled work back into QA — "already in a
        closed category: nothing" — and must not count as a failure either."""
        headers, ws, space, board_list = _team(client, db)
        done = pm.issue(client, headers, ws, board_list["id"], title="Shipped already")
        dropped = pm.issue(client, headers, ws, board_list["id"], title="Dropped")
        qa = _add_stage(db, space["id"], "QA", "in_progress")
        canceled = _add_stage(db, space["id"], "Won't do", "canceled")
        _set_space(db, space["id"], merge_target_status_id=qa)
        _move(client, headers, ws, done["id"], pm.status_id(space, "Done"))
        _move(client, headers, ws, dropped["id"], canceled)

        result = pm.ok(_deliver(client, _merged(title="PAY-1 PAY-2 cleanup")))
        assert result["linked"] == ["PAY-1", "PAY-2"]
        assert result["moved"] == [] and result["failed"] == []
        assert _status(db, done["id"]) == pm.status_id(space, "Done")
        assert _status(db, dropped["id"]) == canceled
        assert _events(db, "status_changed", actor_kind="automation") == []
        assert _events(db, "automation_failed") == []

    def test_a_restricted_flow_without_the_move_fails_visibly_and_never_falls_back(
            self, client, db, secret, caplog):
        """To Do -> Done is not allowed; To Do -> Shipped (also done) is. The old rule would
        have picked a done stage; the new one must not move PAY-1 at all, and must say so three
        ways: `failed`, an `automation_failed` event, a warning with no payload in it. PAY-2,
        in In Review where In Review -> Done is allowed, still moves."""
        headers, ws, space, board_list = _team(client, db)
        stuck = pm.issue(client, headers, ws, board_list["id"], title="Stuck")
        ready = pm.issue(client, headers, ws, board_list["id"], title="Ready")
        todo, doing, review, done = (pm.status_id(space, name) for name in
                                     ("To Do", "In Progress", "In Review", "Done"))
        shipped = _add_stage(db, space["id"], "Shipped", "done")
        _move(client, headers, ws, ready["id"], review)
        _restrict(db, space["id"], [(todo, doing), (doing, review), (review, done),
                                    (todo, shipped)])

        with caplog.at_level(logging.WARNING, logger="base_project.github"):
            result = pm.ok(_deliver(client, _merged(title="PAY-1 PAY-2 refunds")))

        assert result["linked"] == ["PAY-1", "PAY-2"]
        assert result["moved"] == ["PAY-2"] and result["moved_to_done"] == ["PAY-2"]
        [failure] = result["failed"]
        assert failure["key"] == "PAY-1" and "not an allowed move" in failure["reason"]
        assert _status(db, stuck["id"]) == todo
        assert [e.issue_id for e in _events(db, "status_changed", actor_kind="automation")] == [
            ready["id"]]

        [event] = _events(db, "automation_failed")
        assert (event.issue_id, event.actor_id, event.actor_kind) == (stuck["id"], None,
                                                                      "automation")
        assert event.from_value == todo and event.to_value == failure["reason"][:255]

        [record] = [r for r in caplog.records if r.getMessage() == "github merge action failed"]
        assert record.levelno == logging.WARNING
        assert record.extra_fields["issue_key"] == "PAY-1"
        assert record.extra_fields["reason"] == "transition_not_allowed"
        assert "refunds" not in json.dumps(record.extra_fields, default=str)

    def test_a_removed_target_stage_fails_instead_of_picking_another_done_stage(self, client,
                                                                                db, secret):
        """The target stage was removed and another done stage exists. Choosing it would be
        the positional inference §6.1 removed; the merge must fail with a reason that says
        what to fix, and leave the issue where it is."""
        headers, ws, space, board_list = _team(client, db)
        issue = pm.issue(client, headers, ws, board_list["id"], title="Refunds")
        _add_stage(db, space["id"], "Shipped", "done")
        db.query(WorkflowStatus).filter(WorkflowStatus.id == pm.status_id(space, "Done")).update(
            {"deleted": 1, "deleted_at": datetime.now(UTC)})
        db.commit()

        result = pm.ok(_deliver(client, _merged()))
        [failure] = result["failed"]
        assert failure["key"] == "PAY-1" and "no longer part of its workflow" in failure["reason"]
        assert result["moved"] == [] and result["linked"] == ["PAY-1"]
        assert _status(db, issue["id"]) == pm.status_id(space, "To Do")
        assert _events(db, "status_changed") == []
        assert [e.to_value for e in _events(db, "automation_failed")] == [failure["reason"]]

    def test_an_edit_to_a_merged_pull_request_does_not_repeat_the_merge(self, client, db,
                                                                       secret):
        """GitHub sends `edited`, still `merged`, when a merged pull request's title is fixed.
        With an open target, re-running the action on it would pull work that QA sent back to
        In Progress straight back into QA. The action runs on the delivery that makes the link
        merged, and only that one."""
        headers, ws, space, board_list = _team(client, db)
        issue = pm.issue(client, headers, ws, board_list["id"], title="Refunds")
        qa = _add_stage(db, space["id"], "QA", "in_progress")
        _set_space(db, space["id"], merge_target_status_id=qa)
        assert pm.ok(_deliver(client, _merged()))["moved"] == ["PAY-1"]
        _move(client, headers, ws, issue["id"], pm.status_id(space, "In Progress"))

        edited = _pr("edited", merged=True, state="closed", title="PAY-1 refunds (typo)")
        result = pm.ok(_deliver(client, edited))
        assert result["linked"] == ["PAY-1"] and result["moved"] == []
        assert _status(db, issue["id"]) == pm.status_id(space, "In Progress")
        assert [link.title for link in _links(db, issue["id"])] == ["PAY-1 refunds (typo)"]


class TestArchived:
    def test_archived_containers_are_skipped_issue_by_issue(self, client, db, secret):
        """An archived list or space is read-only for GitHub too: no link, no move, and the
        key in `skipped` with the guard's reason. Unlike a freeze, it does not hold back the
        issues beside it — PAY-2 in a live list is applied as usual."""
        headers, ws, space, board_list = _team(client, db)
        live_list = pm.task_list(client, headers, ws, space["id"], "Delivery")
        _ops, ops_list = _other_space(client, headers, ws)
        archived = pm.issue(client, headers, ws, board_list["id"], title="Old")
        live = pm.issue(client, headers, ws, live_list["id"], title="New")
        ops_issue = pm.issue(client, headers, ws, ops_list["id"], title="Ops")
        db.query(ProjectList).filter(ProjectList.id == board_list["id"]).update(
            {"archived_at": datetime.now(UTC)})
        db.query(Space).filter(Space.key == "OPS").update({"archived_at": datetime.now(UTC)})
        db.commit()

        result = pm.ok(_deliver(client, _merged(title="PAY-1 PAY-2 OPS-1")))
        assert result["handled"] is True and result["deferred"] is False
        assert [item["key"] for item in result["skipped"]] == ["PAY-1", "OPS-1"]
        assert all("archived" in item["reason"] for item in result["skipped"])
        assert result["linked"] == ["PAY-2"] and result["moved"] == ["PAY-2"]
        assert _links(db, archived["id"]) == [] and _links(db, ops_issue["id"]) == []
        assert _status(db, archived["id"]) == pm.status_id(space, "To Do")
        assert _status(db, live["id"]) == pm.status_id(space, "Done")


class TestDeferral:
    def test_a_frozen_space_defers_and_the_replay_applies_it_exactly_once(self, client, db,
                                                                          secret):
        """The whole life of a deferred delivery. While PAY is frozen nothing is written, the
        delivery is kept with its payload, GitHub's redelivery is still a duplicate, and a
        replay leaves it deferred. Once unfrozen, a replay applies it — link and merge — marks
        it replayed and drops the payload; a second replay applies nothing."""
        headers, ws, space, board_list = _team(client, db)
        issue = pm.issue(client, headers, ws, board_list["id"], title="Refunds")
        _freeze(db, space["id"])
        payload = _merged()

        result = pm.ok(_deliver(client, payload, delivery="frozen-1"))
        assert result["deferred"] is True and result["handled"] is False
        assert result["linked"] == [] and result["moved"] == []
        assert "PAY" in result["note"]
        row = _row(db, "frozen-1")
        assert (row.status, row.workspace_id, row.payload) == ("deferred", ws, payload)
        assert row.processed_at is None and "PAY" in row.note
        assert _links(db, issue["id"]) == [] and _events(db, "status_changed") == []

        again = pm.ok(_deliver(client, payload, delivery="frozen-1"))
        assert again["duplicate"] is True and again["deferred"] is False
        assert "deferred" in again["note"]

        assert github_controller.replay_deferred(db, ws) == {
            "replayed": 0, "still_deferred": 1, "failed": 0}
        assert _row(db, "frozen-1").status == "deferred"
        assert _links(db, issue["id"]) == []

        _unfreeze(db, space["id"])
        assert github_controller.replay_deferred(db, ws) == {
            "replayed": 1, "still_deferred": 0, "failed": 0}
        row = _row(db, "frozen-1")
        assert (row.status, row.payload) == ("replayed", None)
        assert row.processed_at is not None and "PAY-1" in row.note
        assert [link.state for link in _links(db, issue["id"])] == ["merged"]
        board = pm.board(client, headers, ws, board_list["id"])
        assert pm.column_titles(board, "Done") == ["Refunds"]

        assert github_controller.replay_deferred(db, ws) == {
            "replayed": 0, "still_deferred": 0, "failed": 0}
        assert [e.actor_kind for e in _events(db, "status_changed")] == ["automation"]
        assert len(_links(db, issue["id"])) == 1
        assert pm.ok(_deliver(client, payload, delivery="frozen-1"))["duplicate"] is True

    def test_one_frozen_issue_holds_back_the_whole_delivery(self, client, db, secret):
        """OPS is not frozen, but the pull request also names PAY-1 in frozen PAY. Applying
        the OPS half now and the PAY half later would split one delivery in two; the design
        defers it whole."""
        headers, ws, space, board_list = _team(client, db)
        ops, ops_list = _other_space(client, headers, ws)
        pm.issue(client, headers, ws, board_list["id"], title="Refunds")
        ops_issue = pm.issue(client, headers, ws, ops_list["id"], title="Runbook")
        _freeze(db, space["id"])

        result = pm.ok(_deliver(client, _merged(title="PAY-1 OPS-1 refunds")))
        assert result["deferred"] is True and result["linked"] == []
        assert _links(db, ops_issue["id"]) == []
        assert _status(db, ops_issue["id"]) == pm.status_id(ops, "To Do")

    def test_a_replay_keeps_what_is_still_frozen_and_applies_the_rest(self, client, db,
                                                                      secret):
        """Two deferred deliveries, one per frozen space. Unfreezing OPS replays the
        workspace, but the PAY delivery must stay deferred until PAY is unfrozen too."""
        headers, ws, pay, pay_list = _team(client, db)
        ops, ops_list = _other_space(client, headers, ws)
        pay_issue = pm.issue(client, headers, ws, pay_list["id"], title="Refunds")
        ops_issue = pm.issue(client, headers, ws, ops_list["id"], title="Runbook")
        _freeze(db, pay["id"])
        _freeze(db, ops["id"])
        pm.ok(_deliver(client, _merged(title="PAY-1 refunds"), delivery="for-pay"))
        pm.ok(_deliver(client, _merged(title="OPS-1 runbook", number=8), delivery="for-ops"))

        _unfreeze(db, ops["id"])
        assert github_controller.replay_deferred(db, ws) == {
            "replayed": 1, "still_deferred": 1, "failed": 0}
        assert _row(db, "for-ops").status == "replayed"
        assert _row(db, "for-pay").status == "deferred"
        assert _status(db, ops_issue["id"]) == pm.status_id(ops, "Done")
        assert _status(db, pay_issue["id"]) == pm.status_id(pay, "To Do")

    def test_replays_run_oldest_first(self, client, db, secret, monkeypatch):
        """Order is what makes `opened` then `merged` end merged. The deliveries are sent
        newest first and their ids sort the wrong way, so neither insertion order nor id order
        can pass for `received_at` order."""
        headers, ws, space, board_list = _team(client, db)
        pm.issue(client, headers, ws, board_list["id"], title="One")
        pm.issue(client, headers, ws, board_list["id"], title="Two")
        _freeze(db, space["id"])
        pm.ok(_deliver(client, _pr(title="PAY-1"), delivery="order-a"))
        pm.ok(_deliver(client, _pr(title="PAY-2", number=8), delivery="order-z"))
        _received(db, "order-a", datetime(2026, 10, 5, 12, 0))
        _received(db, "order-z", datetime(2026, 10, 5, 11, 0))
        _unfreeze(db, space["id"])

        seen = []
        real = github_controller._apply_pull_request

        def spy(db, repo, pr, *, delivery_id, received_at=None):
            seen.append(delivery_id)
            return real(db, repo, pr, delivery_id=delivery_id, received_at=received_at)

        monkeypatch.setattr(github_controller, "_apply_pull_request", spy)
        github_controller.replay_deferred(db, ws)
        assert seen == ["order-z", "order-a"]

    def test_a_replay_that_raises_is_rolled_back_alone_and_marked_failed(self, client, db,
                                                                         secret, monkeypatch):
        """The first delivery raises after it has written its link and its move: those must
        be rolled back, it must be marked failed with its payload kept, and the second must
        still be replayed. The unfreeze is left uncommitted in the session on purpose — a
        replay that rolled it back with the failing delivery would leave the space frozen."""
        headers, ws, space, board_list = _team(client, db)
        first = pm.issue(client, headers, ws, board_list["id"], title="One")
        second = pm.issue(client, headers, ws, board_list["id"], title="Two")
        _freeze(db, space["id"])
        pm.ok(_deliver(client, _merged(title="PAY-1"), delivery="replay-fails"))
        pm.ok(_deliver(client, _merged(title="PAY-2", number=8), delivery="replay-works"))
        _received(db, "replay-fails", datetime(2026, 10, 5, 11, 0))
        _received(db, "replay-works", datetime(2026, 10, 5, 12, 0))

        real = github_controller._apply_pull_request

        def flaky(db, repo, pr, *, delivery_id, received_at=None):
            outcome = real(db, repo, pr, delivery_id=delivery_id, received_at=received_at)
            if delivery_id == "replay-fails":
                raise RuntimeError("the database went away")
            return outcome

        monkeypatch.setattr(github_controller, "_apply_pull_request", flaky)
        db.expire_all()
        db.query(Space).filter(Space.id == space["id"]).one().frozen_at = None

        assert github_controller.replay_deferred(db, ws) == {
            "replayed": 1, "still_deferred": 0, "failed": 1}
        failed = _row(db, "replay-fails")
        assert failed.status == "failed" and "RuntimeError" in failed.note
        assert failed.payload is not None and failed.processed_at is not None
        assert _links(db, first["id"]) == []
        assert _status(db, first["id"]) == pm.status_id(space, "To Do")
        assert _row(db, "replay-works").status == "replayed"
        assert _status(db, second["id"]) == pm.status_id(space, "Done")
        assert db.query(Space.frozen_at).filter(Space.id == space["id"]).scalar() is None

    def test_a_replay_does_not_put_stale_state_over_a_newer_delivery(self, client, db,
                                                                     secret):
        """The pull request named PAY-1 and OPS-1 while PAY was frozen (deferred), then was
        merged with a title naming only OPS-1 (applied at once). Replaying the older delivery
        must link PAY-1 without turning OPS-1's `merged` link back to `open`."""
        headers, ws, pay, pay_list = _team(client, db)
        ops, ops_list = _other_space(client, headers, ws)
        pay_issue = pm.issue(client, headers, ws, pay_list["id"], title="Refunds")
        ops_issue = pm.issue(client, headers, ws, ops_list["id"], title="Runbook")
        _freeze(db, pay["id"])
        pm.ok(_deliver(client, _pr(title="PAY-1 OPS-1 refunds"), delivery="older"))
        _received(db, "older", datetime.now(UTC) - timedelta(hours=1))
        newer = pm.ok(_deliver(client, _merged(title="OPS-1 refunds")))
        assert newer["moved"] == ["OPS-1"]

        _unfreeze(db, pay["id"])
        assert github_controller.replay_deferred(db, ws)["replayed"] == 1
        assert [link.state for link in _links(db, pay_issue["id"])] == ["open"]
        assert [link.state for link in _links(db, ops_issue["id"])] == ["merged"]
        assert _status(db, ops_issue["id"]) == pm.status_id(ops, "Done")


class TestDeferredListing:
    def _deferred(self, client, db):
        headers, ws, space, board_list = _team(client, db)
        pm.issue(client, headers, ws, board_list["id"], title="Refunds")
        pm.issue(client, headers, ws, board_list["id"], title="Chargebacks")
        _freeze(db, space["id"])
        pm.ok(_deliver(client, _pr(title="PAY-1 refunds"), delivery="listed-old"))
        pm.ok(_deliver(client, _merged(title="PAY-2 and PAY-9", number=8),
                       delivery="listed-new"))
        _received(db, "listed-old", datetime(2026, 10, 5, 11, 0))
        _received(db, "listed-new", datetime(2026, 10, 5, 12, 0))
        return headers, ws

    def test_admins_see_what_is_held_back_newest_first(self, client, db, secret):
        """The keys come from the stored payload as written — PAY-9 does not exist and is
        still listed, because that is what the pull request says. A delivery that was applied
        is not listed: it is not waiting for anything."""
        headers, ws = self._deferred(client, db)
        rows = pm.ok(client.get(f"/workspaces/{ws}/integrations/deferred", headers=headers))
        assert [row["id"] for row in rows] == ["listed-new", "listed-old"]
        assert [row["keys"] for row in rows] == [["PAY-2", "PAY-9"], ["PAY-1"]]
        assert all(row["event"] == "pull_request" and "PAY" in row["note"] for row in rows)
        assert rows[0]["received_at"].startswith("2026-10-05T12:00")

        _unfreeze(db, db.query(Space.id).filter(Space.workspace_id == ws).scalar())
        github_controller.replay_deferred(db, ws)
        assert pm.ok(client.get(f"/workspaces/{ws}/integrations/deferred",
                                headers=headers)) == []

    def test_the_listing_is_admin_only_and_never_crosses_tenants(self, client, db, secret):
        """A member sees issues but not the integration plumbing (403). Another workspace's
        owner gets the same 404 as for a workspace that does not exist, and their own listing
        does not contain this tenant's deliveries."""
        _headers, ws = self._deferred(client, db)
        member = pm.person(db, "member-1")
        pm.join(db, ws, "member-1", "member")
        assert client.get(f"/workspaces/{ws}/integrations/deferred",
                          headers=member).status_code == 403

        headers_b, ws_b, _, _ = pm.setup_board(client, db, "bob", "bravo", "OPS")
        assert client.get(f"/workspaces/{ws}/integrations/deferred",
                          headers=headers_b).status_code == 404
        assert pm.ok(client.get(f"/workspaces/{ws_b}/integrations/deferred",
                                headers=headers_b)) == []
