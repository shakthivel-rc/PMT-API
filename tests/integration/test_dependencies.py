"""Typed dependencies, through the HTTP API (docs/design/09-restructuring.md §10).

A dependency is one row read from two sides, so most tests here look at both issues: what
the source says ("blocks") and what the target says ("is blocked by") must agree, and both
issues' histories must record the change. The cross-space test is the reason the feature
exists — the plan's way to record coordination between teams without a cross-space parent.
"""
from typing import get_args

import pytest
from sqlalchemy import event

import db.session as db_session
from models.hierarchy import ProjectList, Space, SpaceKeyAlias
from models.issue import DEPENDENCY_KINDS, Issue, IssueEvent, IssueKeyAlias, IssueLink
from schemas.dependencies import DependencyCreate, DependencyKind
from services import dependencies as dependency_svc
from services.dependencies import LABELS
from tests import pm_fixtures as pm
from utils.time_tracking import utc_now

pytestmark = pytest.mark.integration


def _setup(client, db):
    owner, ws, space, board_list = pm.setup_board(client, db)
    member = pm.person(db, "member-1")
    pm.join(db, ws, "member-1", "member")
    a = pm.issue(client, owner, ws, board_list["id"], title="Ship refunds", type="story")
    b = pm.issue(client, owner, ws, board_list["id"], title="Fraud check", type="task")
    return owner, member, ws, space, board_list, a, b


def _link(client, headers, ws, issue_id, status=201, **body):
    response = client.post(f"/workspaces/{ws}/issues/{issue_id}/dependencies", json=body,
                           headers=headers)
    assert response.status_code == status, response.text
    return response.json()


def _deps(client, headers, ws, issue_id):
    return pm.ok(client.get(f"/workspaces/{ws}/issues/{issue_id}/dependencies",
                            headers=headers))


def _other_space(client, owner, ws, key="OPS"):
    space = pm.space(client, owner, ws, key)
    board_list = pm.task_list(client, owner, ws, space["id"], name="Requests")
    return space, board_list


class _QueryCounter:
    def __init__(self):
        self.statements = []

    def __enter__(self):
        self._listener = lambda conn, cursor, statement, *a: self.statements.append(statement)
        event.listen(db_session.engine, "before_cursor_execute", self._listener)
        return self

    def __exit__(self, *exc):
        event.remove(db_session.engine, "before_cursor_execute", self._listener)


class TestKinds:
    def test_the_schema_the_model_and_the_labels_agree_on_the_kinds(self):
        """Would catch: a kind added to `models.issue.DEPENDENCY_KINDS` that the request
        schema refuses, or that is stored and then has no sentence to be read as."""
        assert set(get_args(DependencyKind)) == set(DEPENDENCY_KINDS)
        assert set(LABELS) == set(DEPENDENCY_KINDS)

    def test_the_documented_example_bodies_are_accepted(self):
        """Would catch: an example in `/docs` that the API itself refuses — the first request
        an integrator copies."""
        for example in DependencyCreate.model_config["json_schema_extra"]["examples"]:
            DependencyCreate.model_validate(example)


class TestLinking:
    def test_blocks_reads_from_both_ends(self, client, db):
        """Would catch: the target's side not seeing the row, or reading it the wrong way
        round — the SPA would show "PAY-2 blocks PAY-1" on the issue that is blocked."""
        _owner, member, ws, space, board_list, a, b = _setup(client, db)
        created = _link(client, member, ws, a["id"], kind="blocks",
                        target_issue_id=b["id"])["data"]
        assert created["kind"] == "blocks" and created["direction"] == "outward"
        assert created["label"] == "blocks"
        assert created["issue"]["id"] == b["id"] and created["issue"]["key"] == "PAY-2"
        assert created["issue"]["title"] == "Fraud check"
        assert created["issue"]["type"] == "task"
        assert created["issue"]["status_id"] == pm.status_id(space, "To Do")
        assert created["issue"]["status_name"] == "To Do"
        assert created["issue"]["category"] == "todo"
        assert created["issue"]["space_id"] == space["id"]
        assert created["issue"]["list_id"] == board_list["id"]
        assert created["created_by"] == {"id": "member-1", "display_name": "Test User"}

        [outward] = _deps(client, member, ws, a["id"])
        assert outward["id"] == created["id"] and outward["label"] == "blocks"
        [inward] = _deps(client, member, ws, b["id"])
        assert inward["id"] == created["id"]
        assert inward["direction"] == "inward" and inward["label"] == "is blocked by"
        assert inward["issue"]["id"] == a["id"] and inward["issue"]["key"] == "PAY-1"

    def test_every_kind_reads_as_its_own_sentence_from_each_side(self, client, db):
        """Would catch: a label table that names the source's sentence for the target (or
        the reverse) for any one kind."""
        owner, _member, ws, _space, board_list, a, _b = _setup(client, db)
        for kind, (outward, inward) in LABELS.items():
            other = pm.issue(client, owner, ws, board_list["id"], title=f"{kind} target")
            created = _link(client, owner, ws, a["id"], kind=kind,
                            target_issue_id=other["id"])["data"]
            assert (created["kind"], created["label"]) == (kind, outward)
            [theirs] = _deps(client, owner, ws, other["id"])
            assert (theirs["kind"], theirs["label"]) == (kind, inward)
        mine = [(d["kind"], d["label"]) for d in _deps(client, owner, ws, a["id"])]
        assert mine == [("blocks", "blocks"), ("relates", "relates to"),
                        ("duplicates", "duplicates"), ("clones", "clones")]

    def test_the_list_is_grouped_by_label_whatever_the_creation_order(self, client, db):
        """Would catch: dependencies listed in creation order, which puts the one blocker
        that matters wherever it happened to be added."""
        owner, _member, ws, _space, board_list, a, b = _setup(client, db)
        c = pm.issue(client, owner, ws, board_list["id"], title="C")
        _link(client, owner, ws, a["id"], kind="relates", target_issue_id=c["id"])
        _link(client, owner, ws, b["id"], kind="blocks", target_issue_id=a["id"])
        _link(client, owner, ws, a["id"], kind="blocks", target_issue_id=c["id"])
        labels = [d["label"] for d in _deps(client, owner, ws, a["id"])]
        assert labels == ["blocks", "is blocked by", "relates to"]

    def test_a_dependency_can_cross_spaces(self, client, db):
        """The point of the feature: coordination between two teams' spaces is a typed
        link, because a cross-space parent is refused."""
        owner, member, ws, space, _list, a, _b = _setup(client, db)
        ops, ops_list = _other_space(client, owner, ws)
        theirs = pm.issue(client, owner, ws, ops_list["id"], title="Open the firewall")
        created = _link(client, member, ws, a["id"], kind="blocks",
                        target_issue_id=theirs["id"])["data"]
        assert created["issue"]["key"] == "OPS-1"
        assert created["issue"]["space_id"] == ops["id"]
        [inward] = _deps(client, member, ws, theirs["id"])
        assert inward["label"] == "is blocked by"
        assert inward["issue"]["key"] == "PAY-1"
        assert inward["issue"]["space_id"] == space["id"]

    def test_a_target_key_resolves_live_and_old_keys(self, client, db):
        """Would catch: `target_key` matched against live keys only, so a key quoted before
        an issue moved space — or before its space changed key — finds nothing."""
        owner, _member, ws, space, board_list, a, b = _setup(client, db)
        c = pm.issue(client, owner, ws, board_list["id"], title="Moved here later")
        db.add(IssueKeyAlias(workspace_id=ws, issue_id=c["id"], space_id=space["id"],
                             space_key="OLD", number=7))
        db.add(SpaceKeyAlias(workspace_id=ws, space_id=space["id"], key="PAYOLD"))
        db.commit()

        live = _link(client, owner, ws, a["id"], kind="blocks", target_key="pay-2")["data"]
        assert live["issue"]["id"] == b["id"]
        old_issue_key = _link(client, owner, ws, a["id"], kind="relates",
                              target_key="OLD-7")["data"]
        assert old_issue_key["issue"]["id"] == c["id"]
        assert old_issue_key["issue"]["key"] == "PAY-3", "the current key, never the old one"
        old_space_key = _link(client, owner, ws, a["id"], kind="duplicates",
                              target_key="PAYOLD-2")["data"]
        assert old_space_key["issue"]["id"] == b["id"]

    def test_an_unknown_target_is_404(self, client, db):
        """Would catch: a target that resolves to nothing being answered as anything but 404 — by
        id, by a key whose number or space does not exist, or from an issue path that does not
        exist."""
        owner, _member, ws, _space, _list, a, _b = _setup(client, db)
        _link(client, owner, ws, a["id"], 404, kind="blocks", target_issue_id="no-such-issue")
        _link(client, owner, ws, a["id"], 404, kind="blocks", target_key="PAY-99")
        _link(client, owner, ws, a["id"], 404, kind="blocks", target_key="ZZZ-1")
        assert client.post(f"/workspaces/{ws}/issues/no-such-issue/dependencies",
                           json={"kind": "blocks", "target_issue_id": a["id"]},
                           headers=owner).status_code == 404

    def test_an_issue_cannot_depend_on_itself(self, client, db):
        """By id and by key: the key resolves to the same issue, and must be refused too."""
        owner, _member, ws, _space, _list, a, _b = _setup(client, db)
        _link(client, owner, ws, a["id"], 422, kind="blocks", target_issue_id=a["id"])
        _link(client, owner, ws, a["id"], 422, kind="relates", target_key="PAY-1")
        assert db.query(IssueLink).count() == 0

    def test_the_body_names_exactly_one_well_formed_target(self, client, db):
        """Would catch: a body naming both targets (which one wins?), neither, a key that is not
        key-shaped, or a kind the model does not have, reaching the database."""
        owner, _member, ws, _space, _list, a, b = _setup(client, db)
        _link(client, owner, ws, a["id"], 422, kind="blocks", target_issue_id=b["id"],
              target_key="PAY-2")
        _link(client, owner, ws, a["id"], 422, kind="blocks")
        _link(client, owner, ws, a["id"], 422, kind="blocks", target_key="PAY-0")
        _link(client, owner, ws, a["id"], 422, kind="blocks", target_key="PAY 2")
        _link(client, owner, ws, a["id"], 422, kind="causes", target_issue_id=b["id"])
        assert db.query(IssueLink).count() == 0

    def test_duplicates_are_refused_and_relates_counts_either_way_round(self, client, db):
        """Would catch: `B relates A` stored beside `A relates B` — the same fact twice, shown
        twice on both issues. A directional kind the other way round is a different fact."""
        owner, _member, ws, _space, _list, a, b = _setup(client, db)
        blocks = _link(client, owner, ws, a["id"], kind="blocks",
                       target_issue_id=b["id"])["data"]
        again = _link(client, owner, ws, a["id"], 409, kind="blocks", target_issue_id=b["id"])
        assert again["data"]["dependency_id"] == blocks["id"]
        assert again["message"] == "PAY-1 already blocks PAY-2."

        relates = _link(client, owner, ws, a["id"], kind="relates",
                        target_issue_id=b["id"])["data"]
        reverse = _link(client, owner, ws, b["id"], 409, kind="relates",
                        target_issue_id=a["id"])
        assert reverse["data"]["dependency_id"] == relates["id"]
        same_way = _link(client, owner, ws, a["id"], 409, kind="relates",
                         target_issue_id=b["id"])
        assert same_way["data"]["dependency_id"] == relates["id"]

        # A different kind between the same pair, and a directional kind reversed, are new.
        _link(client, owner, ws, a["id"], kind="duplicates", target_issue_id=b["id"])
        _link(client, owner, ws, b["id"], kind="blocks", target_issue_id=a["id"])
        assert db.query(IssueLink).count() == 4

    def test_both_issues_record_the_change(self, client, db):
        """Would catch: the event written on one side only, or without the direction — a
        history that cannot say whether the issue blocked the other or was blocked by it."""
        owner, member, ws, _space, _list, a, b = _setup(client, db)
        created = _link(client, member, ws, a["id"], kind="blocks",
                        target_issue_id=b["id"])["data"]

        def recorded(name):
            rows = db.query(IssueEvent).filter(IssueEvent.event == name).all()
            assert {r.actor_kind for r in rows} == {"user"}
            return {(r.issue_id, r.from_value, r.to_value, r.actor_id) for r in rows}

        assert recorded("dependency_added") == {
            (a["id"], "outward", f"blocks:{b['id']}", "member-1"),
            (b["id"], "inward", f"blocks:{a['id']}", "member-1"),
        }
        pm.ok(client.delete(f"/workspaces/{ws}/dependencies/{created['id']}", headers=member))
        assert recorded("dependency_removed") == {
            (a["id"], "outward", f"blocks:{b['id']}", "member-1"),
            (b["id"], "inward", f"blocks:{a['id']}", "member-1"),
        }
        # Planning, not an edit: someone with either issue open must not get a 409 for it.
        for issue in (a, b):
            detail = pm.ok(client.get(f"/workspaces/{ws}/issues/{issue['id']}", headers=owner))
            assert detail["version"] == 1

    def test_another_writer_can_record_itself_as_the_actor(self, client, db):
        """Would catch: a transfer's `relates` link to the parent it detached from (§7.2)
        recorded as the admin's own everyday act, indistinguishable in the history."""
        _owner, _member, ws, _space, _list, a, b = _setup(client, db)
        source, target = (db.query(Issue).filter(Issue.id == issue["id"]).one()
                          for issue in (a, b))
        assert dependency_svc.existing_link(db, ws, a["id"], b["id"], "relates") is None
        link = dependency_svc.create_link(db, source, target, "relates", "owner-1",
                                          actor_kind="migration")
        db.commit()
        assert dependency_svc.existing_link(db, ws, b["id"], a["id"], "relates").id == link.id
        dependency_svc.remove_link(db, link, source, target, "owner-1", actor_kind="migration")
        db.commit()
        kinds = {(row.event, row.actor_kind) for row in db.query(IssueEvent).filter(
            IssueEvent.event.in_(["dependency_added", "dependency_removed"]))}
        assert kinds == {("dependency_added", "migration"), ("dependency_removed", "migration")}

    def test_delete_removes_it_from_both_sides(self, client, db):
        """Would catch: a delete that leaves the row visible from the other issue, or a second
        delete of the same id answering anything but 404."""
        owner, member, ws, _space, _list, a, b = _setup(client, db)
        created = _link(client, owner, ws, a["id"], kind="clones",
                        target_issue_id=b["id"])["data"]
        path = f"/workspaces/{ws}/dependencies/{created['id']}"
        assert pm.ok(client.delete(path, headers=member)) == {"id": created["id"]}
        assert _deps(client, owner, ws, a["id"]) == []
        assert _deps(client, owner, ws, b["id"]) == []
        assert client.delete(path, headers=member).status_code == 404

    def test_a_link_to_a_deleted_issue_is_hidden_but_kept(self, client, db):
        """Would catch: a row pointing at an issue that answers 404 everywhere else, or the
        row itself destroyed by a soft delete of one end."""
        owner, _member, ws, _space, _list, a, b = _setup(client, db)
        created = _link(client, owner, ws, a["id"], kind="blocks",
                        target_issue_id=b["id"])["data"]
        pm.ok(client.delete(f"/workspaces/{ws}/issues/{b['id']}", headers=owner))
        assert _deps(client, owner, ws, a["id"]) == []
        assert db.query(IssueLink).filter(IssueLink.id == created["id"]).count() == 1
        pm.ok(client.delete(f"/workspaces/{ws}/dependencies/{created['id']}", headers=owner))


class TestWriteGuard:
    def test_a_frozen_space_at_either_end_refuses_with_423(self, client, db):
        """Would catch: only the issue in the path being checked — a migration wave's frozen
        issue would gain a dependency (and an event) mid-move, from the other side."""
        owner, member, ws, _space, _list, a, _b = _setup(client, db)
        ops, ops_list = _other_space(client, owner, ws)
        theirs = pm.issue(client, owner, ws, ops_list["id"], title="Theirs")
        existing = _link(client, member, ws, a["id"], kind="relates",
                         target_issue_id=theirs["id"])["data"]
        db.query(Space).filter(Space.id == ops["id"]).update(
            {Space.frozen_at: utc_now(), Space.freeze_reason: "wave 1"})
        db.commit()

        outward = _link(client, member, ws, a["id"], 423, kind="blocks",
                        target_issue_id=theirs["id"])
        assert "frozen" in outward["message"] and "wave 1" in outward["message"]
        _link(client, member, ws, theirs["id"], 423, kind="blocks", target_issue_id=a["id"])
        assert client.delete(f"/workspaces/{ws}/dependencies/{existing['id']}",
                             headers=member).status_code == 423
        # Reads are never refused.
        assert len(_deps(client, member, ws, theirs["id"])) == 1
        assert db.query(IssueLink).count() == 1

    def test_an_archived_list_refuses_with_423(self, client, db):
        """Would catch: the write guard consulted for frozen spaces only — an archived list is
        read-only for good, and its issues must not gain dependencies either."""
        _owner, member, ws, _space, board_list, a, b = _setup(client, db)
        db.query(ProjectList).filter(ProjectList.id == board_list["id"]).update(
            {ProjectList.archived_at: utc_now()})
        db.commit()
        refused = _link(client, member, ws, a["id"], 423, kind="blocks",
                        target_issue_id=b["id"])
        assert "archived" in refused["message"]


class TestAccess:
    def test_guests_read_but_do_not_link_or_unlink(self, client, db):
        """Would catch: the member gate missing from the create or the delete route — a guest is
        read-only everywhere else in a workspace."""
        owner, _member, ws, _space, _list, a, b = _setup(client, db)
        guest = pm.person(db, "guest-1")
        pm.join(db, ws, "guest-1", "guest")
        created = _link(client, owner, ws, a["id"], kind="blocks",
                        target_issue_id=b["id"])["data"]
        assert len(_deps(client, guest, ws, a["id"])) == 1
        _link(client, guest, ws, b["id"], 403, kind="relates", target_issue_id=a["id"])
        assert client.delete(f"/workspaces/{ws}/dependencies/{created['id']}",
                             headers=guest).status_code == 403

    def test_another_workspace_is_404_everywhere(self, client, db):
        """Would catch: a target resolved outside the caller's workspace — a link would then
        join two tenants and show each one the other's issue titles."""
        headers_a, ws_a, _space_a, list_a = pm.setup_board(client, db, "alice", "alpha", "ALP")
        headers_b, ws_b, _space_b, list_b = pm.setup_board(client, db, "bob", "bravo", "BRA")
        mine = pm.issue(client, headers_a, ws_a, list_a["id"], title="Mine")
        theirs = pm.issue(client, headers_b, ws_b, list_b["id"], title="Bravo secret")
        their_other = pm.issue(client, headers_b, ws_b, list_b["id"], title="Bravo two")
        their_link = _link(client, headers_b, ws_b, theirs["id"], kind="blocks",
                           target_issue_id=their_other["id"])["data"]

        assert client.get(f"/workspaces/{ws_a}/issues/{theirs['id']}/dependencies",
                          headers=headers_a).status_code == 404
        _link(client, headers_a, ws_a, theirs["id"], 404, kind="blocks",
              target_issue_id=mine["id"])
        _link(client, headers_a, ws_a, mine["id"], 404, kind="blocks",
              target_issue_id=theirs["id"])
        _link(client, headers_a, ws_a, mine["id"], 404, kind="blocks", target_key="BRA-1")
        assert client.delete(f"/workspaces/{ws_a}/dependencies/{their_link['id']}",
                             headers=headers_a).status_code == 404
        stranger = client.get(f"/workspaces/{ws_b}/issues/{theirs['id']}/dependencies",
                              headers=headers_a)
        assert stranger.status_code == 404
        assert stranger.json()["message"] == "Workspace not found"
        assert db.query(IssueLink).count() == 1


class TestCost:
    def test_listing_costs_the_same_number_of_queries_for_one_link_or_many(self, client, db):
        """The N+1 guard: the other ends, their keys, stages and the people who linked them
        are fetched once per request, not once per dependency."""
        owner, member, ws, _space, board_list, a, b = _setup(client, db)
        _ops, ops_list = _other_space(client, owner, ws)
        _link(client, member, ws, a["id"], kind="blocks", target_issue_id=b["id"])
        with _QueryCounter() as one:
            assert len(_deps(client, owner, ws, a["id"])) == 1
        for n in range(5):
            target_list = board_list if n % 2 else ops_list
            other = pm.issue(client, owner, ws, target_list["id"], title=f"other {n}")
            _link(client, member if n % 2 else owner, ws, other["id"], kind="relates",
                  target_issue_id=a["id"])
        with _QueryCounter() as many:
            assert len(_deps(client, owner, ws, a["id"])) == 6
        assert len(many.statements) <= len(one.statements), (
            f"queries grew with dependencies: {len(one.statements)} -> "
            f"{len(many.statements)}")
