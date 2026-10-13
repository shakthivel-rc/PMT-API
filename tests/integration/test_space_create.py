"""Creating a space: the charter, the owner, the workflow template and the first lists.

docs/design/09-restructuring.md §3.2, through the HTTP API. The test to read first is the
backward-compatibility one: every client written before 2026-10-05 sends `{name, key}` and
must keep getting the four-stage workflow. The rest pin the creation checklist the plan asks
for, the governance switch that makes it mandatory, and the rule that a template which cannot
be applied is a 422 — never a 500, never a half-built space.
"""
from typing import get_args

import pytest

from models.hierarchy import (
    ProjectList,
    Space,
    WorkflowStatus,
    WorkflowTemplate,
    WorkflowTransition,
)
from schemas.workspaces import DeliveryMethod
from tests import pm_fixtures as pm
from utils.workflow_rules import DELIVERY_METHODS

pytestmark = pytest.mark.integration


def _create(client, headers, ws: str, **body):
    return client.post(f"/workspaces/{ws}/spaces", json=body, headers=headers)


def _governance(client, headers, ws: str, **fields) -> dict:
    return pm.ok(client.patch(f"/workspaces/{ws}/settings/governance", json=fields,
                              headers=headers))


def _owner_and_workspace(client, db, user_id: str = "owner-1", slug: str = "acme"):
    headers = pm.person(db, user_id)
    return headers, pm.workspace(client, headers, slug)


def _template(db, ws: str, definition, *, name: str = "Team flow",
              created_by: str = "owner-1", deleted: int = 0) -> str:
    """A workspace template row written directly, the way a stored document that predates
    today's validation would be."""
    row = WorkflowTemplate(workspace_id=ws, name=name, definition=definition,
                           created_by=created_by, deleted=deleted)
    db.add(row)
    db.commit()
    return str(row.id)


def _names(payload: dict) -> list[str]:
    return [status["name"] for status in payload["statuses"]]


def _flow(payload: dict) -> set[tuple[str | None, str]]:
    """The transitions as stage names, so an assertion reads like the design's table."""
    names = {status["id"]: status["name"] for status in payload["statuses"]}
    return {(names.get(t["from_status_id"]), names[t["to_status_id"]])
            for t in payload["workflow"]["transitions"]}


THREE_STAGES = {
    "transition_mode": "any", "wip_policy": "warn",
    "stages": [{"name": "Open", "category": "todo", "color": "neutral",
                "counts_toward_wip": False, "wip_limit": None},
               {"name": "Doing", "category": "in_progress", "color": "info",
                "counts_toward_wip": True, "wip_limit": 3},
               {"name": "Shipped", "category": "done", "color": "success",
                "counts_toward_wip": False, "wip_limit": None}],
    "initial": 0, "transitions": [], "merge_target": 2,
}


class TestTheOldContract:
    def test_the_two_field_body_still_creates_the_four_stage_workflow(self, client, db):
        """Every client before 2026-10-05 sends only a name and a key. If the new optional
        fields, the governance default or the template resolver changed what that body
        creates, this fails."""
        headers, ws = _owner_and_workspace(client, db)
        space = pm.ok(_create(client, headers, ws, name="Payments", key="PAY"), 201)

        assert _names(space) == ["To Do", "In Progress", "In Review", "Done"]
        assert space["workflow"]["transition_mode"] == "any"
        assert space["workflow"]["initial_status_id"] == pm.status_id(space, "To Do")
        assert space["workflow"]["merge_action"] == {
            "mode": "status", "target_status_id": pm.status_id(space, "Done")}
        assert space["workflow"]["transitions"] == []
        assert space["folders"] == [] and space["lists"] == []
        assert space["description"] is None and space["owner"] is None
        assert space["delivery_method"] is None and space["has_charter"] is False
        assert space["archived"] is False and space["frozen"] is False
        assert space["can_edit_workflow"] is True

    def test_an_unknown_field_is_refused_rather_than_ignored(self, client, db):
        """A misspelt `workflow_template` silently creating a four-stage space is the failure
        `extra="forbid"` exists to prevent."""
        headers, ws = _owner_and_workspace(client, db)
        response = _create(client, headers, ws, name="Payments", key="PAY",
                           workflow_templat="delivery")
        assert response.status_code == 422
        assert db.query(Space).count() == 0

    def test_creating_a_space_is_for_admins_and_tenant_scoped(self, client, db):
        """A member is refused with 403 (they can see the workspace); a stranger gets the
        workspace's 404 and learns nothing."""
        _headers, ws = _owner_and_workspace(client, db)
        member = pm.person(db, "member-1")
        pm.join(db, ws, "member-1", "member")
        stranger = pm.person(db, "stranger")
        assert _create(client, member, ws, name="Ops", key="OPS").status_code == 403
        assert _create(client, stranger, ws, name="Ops", key="OPS").status_code == 404
        assert db.query(Space).count() == 0


class TestTheCharter:
    def test_a_space_carries_its_owner_delivery_method_description_and_charter(self, client,
                                                                                db):
        """What the creation checklist stores, and how the tree and the detail read it back.
        Blank charter fields are stored as missing, so "filled in" means "has words"."""
        headers, ws = _owner_and_workspace(client, db)
        pm.person(db, "lead-1")
        pm.join(db, ws, "lead-1", "member")
        space = pm.ok(_create(
            client, headers, ws, name="Payments", key="PAY", owner_id="lead-1",
            delivery_method="scrum", description="Card payments and refunds.",
            charter={"objective": "  Refunds without a phone call. ",
                     "success_measure": "Refund tickets halve in a quarter.",
                     "exclusions": "   "}), 201)

        assert space["owner"] == {"id": "lead-1", "display_name": "Test User"}
        assert space["delivery_method"] == "scrum"
        assert space["description"] == "Card payments and refunds."
        assert space["has_charter"] is True

        detail = pm.ok(client.get(f"/workspaces/{ws}/spaces/{space['id']}", headers=headers))
        assert detail["charter"]["objective"] == "Refunds without a phone call."
        assert detail["charter"]["success_measure"] == "Refund tickets halve in a quarter."
        assert detail["charter"]["exclusions"] is None
        assert set(detail["charter"]) >= {"scope", "reuse_justification", "retention_policy"}
        assert detail["key_aliases"] == [] and detail["created_at"]
        stored = db.query(Space).filter(Space.id == space["id"]).one()
        assert stored.charter == {"objective": "Refunds without a phone call.",
                                  "success_measure": "Refund tickets halve in a quarter."}

    def test_an_all_blank_charter_is_no_charter(self, client, db):
        """A form that posts every field empty has not filled in the checklist; `has_charter`
        and the inventory must not say it has."""
        headers, ws = _owner_and_workspace(client, db)
        space = pm.ok(_create(client, headers, ws, name="Payments", key="PAY",
                              charter={"objective": " ", "notes": ""}), 201)
        assert space["has_charter"] is False
        assert db.query(Space.charter).filter(Space.id == space["id"]).scalar() is None

    def test_the_stored_charter_is_validated_on_read(self, client, db):
        """The column does not constrain the document, so every read validates it: one that
        no longer validates reads as no charter rather than failing the tree, and a key this
        release does not know — a newer release's field, after a rollback — is dropped
        without losing the fields it does know."""
        headers, ws = _owner_and_workspace(client, db)
        space = pm.ok(_create(client, headers, ws, name="Payments", key="PAY",
                              charter={"objective": "Refunds."}), 201)

        def stored(document):
            db.query(Space).filter(Space.id == space["id"]).update({"charter": document})
            db.commit()
            detail = pm.ok(client.get(f"/workspaces/{ws}/spaces/{space['id']}",
                                      headers=headers))
            [in_tree] = pm.ok(client.get(f"/workspaces/{ws}", headers=headers))["spaces"]
            assert in_tree["has_charter"] is (detail["charter"] is not None)
            return detail["charter"]

        assert stored({"objective": "x" * 2001}) is None
        assert stored({"objective": 42}) is None
        assert stored(["not", "a", "document"]) is None
        kept = stored({"objective": "Refunds.", "sponsor": "a field from a newer release"})
        assert kept["objective"] == "Refunds." and "sponsor" not in kept

    def test_the_owner_must_be_a_member(self, client, db):
        """An owner outside the workspace could not even see the space they are accountable
        for; an unknown id is the same refusal."""
        headers, ws = _owner_and_workspace(client, db)
        pm.person(db, "outsider")
        for owner in ("outsider", "no-such-user"):
            response = _create(client, headers, ws, name="Payments", key="PAY",
                               owner_id=owner)
            assert response.status_code == 422, owner
            assert "member" in response.json()["message"]
        assert db.query(Space).count() == 0

    def test_the_charter_takes_only_its_own_fields_of_at_most_2000_characters(self, client,
                                                                              db):
        """`extra="forbid"` on the checklist and the 2,000-character cap of §3.2."""
        headers, ws = _owner_and_workspace(client, db)
        assert _create(client, headers, ws, name="P", key="PAY",
                       charter={"objectiv": "typo"}).status_code == 422
        assert _create(client, headers, ws, name="P", key="PAY",
                       charter={"objective": "x" * 2001}).status_code == 422
        assert _create(client, headers, ws, name="P", key="PAY",
                       description="x" * 10_001).status_code == 422
        pm.ok(_create(client, headers, ws, name="P", key="PAY",
                      charter={"objective": "x" * 2000}, description="x" * 10_000), 201)

    def test_the_delivery_method_is_scrum_or_kanban(self, client, db):
        """The contract's Literal and `utils/workflow_rules.DELIVERY_METHODS` must name the
        same methods, and anything else is refused."""
        assert set(get_args(DeliveryMethod)) == set(DELIVERY_METHODS)
        headers, ws = _owner_and_workspace(client, db)
        assert _create(client, headers, ws, name="P", key="PAY",
                       delivery_method="waterfall").status_code == 422
        space = pm.ok(_create(client, headers, ws, name="P", key="PAY",
                              delivery_method="kanban"), 201)
        assert space["delivery_method"] == "kanban"


class TestGovernanceRequiresTheChecklist:
    def test_every_missing_item_is_named_at_once(self, client, db):
        """With `require_space_charter` on, the 422 lists all five items in one answer —
        a form that learns them one per submit is the failure §3.2 rules out — in the shape
        a validation 422 has, so the SPA highlights the fields it already knows how to."""
        headers, ws = _owner_and_workspace(client, db)
        _governance(client, headers, ws, require_space_charter=True)

        refused = _create(client, headers, ws, name="Payments", key="PAY")
        assert refused.status_code == 422
        body = refused.json()
        assert body["data"]["missing"] == [
            "owner_id", "delivery_method", "charter.objective", "charter.success_measure",
            "charter.reuse_justification"]
        assert [error["loc"] for error in body["data"]["errors"]] == [
            ["body", "owner_id"], ["body", "delivery_method"], ["body", "charter", "objective"],
            ["body", "charter", "success_measure"],
            ["body", "charter", "reuse_justification"]]
        assert "an owner" in body["message"] and "the success measure" in body["message"]
        assert db.query(Space).count() == 0

    def test_partly_filled_lists_the_rest_and_blank_text_does_not_count(self, client, db):
        """Whitespace is not an objective; the 422 names exactly what is still missing."""
        headers, ws = _owner_and_workspace(client, db)
        _governance(client, headers, ws, require_space_charter=True)
        refused = _create(client, headers, ws, name="Payments", key="PAY", owner_id="owner-1",
                          charter={"objective": "   ", "success_measure": "Tickets halve."})
        assert refused.status_code == 422
        assert refused.json()["data"]["missing"] == [
            "delivery_method", "charter.objective", "charter.reuse_justification"]

    def test_a_complete_checklist_is_accepted(self, client, db):
        """The rule is satisfiable: owner, method and the three charter answers suffice."""
        headers, ws = _owner_and_workspace(client, db)
        _governance(client, headers, ws, require_space_charter=True)
        space = pm.ok(_create(
            client, headers, ws, name="Payments", key="PAY", owner_id="owner-1",
            delivery_method="scrum",
            charter={"objective": "Self-service refunds.", "success_measure": "Tickets halve.",
                     "reuse_justification": "Its own sprint cadence."}), 201)
        assert space["has_charter"] is True


class TestTemplates:
    def test_the_delivery_template_is_restricted_six_stages_and_merges_to_qa(self, client,
                                                                             db):
        """The plan's Delivery example as a starting point: restricted flow with rework and
        reopen paths, and a merge that moves work to QA rather than completing it."""
        headers, ws = _owner_and_workspace(client, db)
        space = pm.ok(_create(client, headers, ws, name="Payments", key="PAY",
                              workflow_template="delivery"), 201)

        assert _names(space) == ["Backlog", "Ready", "Development", "Review", "QA",
                                 "Released"]
        workflow = space["workflow"]
        assert workflow["transition_mode"] == "restricted"
        assert workflow["initial_status_id"] == pm.status_id(space, "Backlog")
        assert workflow["merge_action"] == {"mode": "status",
                                            "target_status_id": pm.status_id(space, "QA")}
        expected = {("Backlog", "Ready"), ("Ready", "Backlog"), ("Ready", "Development"),
                    ("Development", "Review"), ("Review", "Development"), ("Review", "QA"),
                    ("QA", "Development"), ("QA", "Released"), ("Released", "Development")}
        assert _flow(space) == expected

        [in_tree] = pm.ok(client.get(f"/workspaces/{ws}", headers=headers))["spaces"]
        assert _flow(in_tree) == expected
        assert in_tree["workflow"] == workflow

    def test_builtin_prefix_and_a_template_that_only_links_merges(self, client, db):
        """`builtin:support` is the support template, whose merge action only links."""
        headers, ws = _owner_and_workspace(client, db)
        space = pm.ok(_create(client, headers, ws, name="Support", key="SUP",
                              workflow_template="builtin:support"), 201)
        assert _names(space) == ["New", "Triage", "Investigating", "Resolved"]
        assert space["workflow"]["merge_action"] == {"mode": "none", "target_status_id": None}
        assert _flow(space) >= {("Resolved", "Investigating")}

    def test_a_workspace_template_by_id_and_only_this_workspaces(self, client, db):
        """A live template of this workspace applies; another tenant's id, and a deleted
        template, are unknown here — the same rule as every other id."""
        headers, ws = _owner_and_workspace(client, db)
        _other, other_ws = _owner_and_workspace(client, db, "owner-2", "bravo")
        ours = _template(db, ws, THREE_STAGES)
        theirs = _template(db, other_ws, THREE_STAGES, created_by="owner-2")
        gone = _template(db, ws, THREE_STAGES, name="Retired", deleted=1)

        space = pm.ok(_create(client, headers, ws, name="Team", key="TEAM",
                              workflow_template=ours), 201)
        assert _names(space) == ["Open", "Doing", "Shipped"]
        doing = next(s for s in space["statuses"] if s["name"] == "Doing")
        assert doing["wip_limit"] == 3 and doing["counts_toward_wip"] is True
        assert space["workflow"]["merge_action"]["target_status_id"] == pm.status_id(
            space, "Shipped")

        for ref in (theirs, gone):
            response = _create(client, headers, ws, name="X", key="XX", workflow_template=ref)
            assert response.status_code == 422, ref

    def test_an_unknown_template_is_a_422_and_creates_nothing(self, client, db):
        """Neither a made-up key nor a `builtin:` prefix on one may fall back to the default
        quietly: the caller asked for a specific workflow."""
        headers, ws = _owner_and_workspace(client, db)
        for ref in ("kanban-pro", "builtin:nope", "builtin:"):
            response = _create(client, headers, ws, name="P", key="PAY", workflow_template=ref)
            assert response.status_code == 422, ref
            assert "no workflow template" in response.json()["message"]
        assert db.query(Space).count() == 0
        assert db.query(WorkflowStatus).count() == 0

    @pytest.mark.parametrize("definition, why", [
        (["not", "a", "definition"], "not a workflow definition"),
        ({"stages": []}, "no stages"),
        ({"stages": [{"category": "todo"}, {"name": "Done", "category": "done"}]}, "name"),
        ({"stages": [{"name": "A", "category": "later"}]}, "category"),
        ({"stages": [{"name": "Open", "category": "todo"},
                     {"name": "open", "category": "done"}]}, "two stages"),
        ({"stages": [{"name": "Open", "category": "todo"}]}, "no completed stage"),
        ({"stages": [{"name": "Open", "category": "todo"},
                     {"name": "Done", "category": "done"}], "initial": 7}, "initial"),
        ({"stages": [{"name": "Open", "category": "todo"},
                     {"name": "Done", "category": "done"}], "initial": 1}, "closed stage"),
        ({"stages": [{"name": "Open", "category": "todo"},
                     {"name": "Done", "category": "done", "wip_limit": 0}]}, "WIP limit"),
        ({"stages": [{"name": "Open", "category": "todo"},
                     {"name": "Done", "category": "done"}],
          "transitions": [{"from": 0, "to": 0}]}, "to itself"),
        ({"stages": [{"name": "Open", "category": "todo"},
                     {"name": "Done", "category": "done"}],
          "transitions": [{"from": 0, "to": 9}]}, "does not have"),
        ({"stages": [{"name": "Open", "category": "todo"},
                     {"name": "Done", "category": "done"}],
          "transition_mode": "restricted", "transitions": []}, "cannot be reached"),
        ({"stages": [{"name": "Open", "category": "todo"},
                     {"name": "Done", "category": "done"},
                     {"name": "Dropped", "category": "canceled"}], "merge_target": 2},
         "canceled"),
    ])
    def test_a_stored_template_that_cannot_be_applied_is_a_422_not_a_500(
            self, client, db, definition, why):
        """Each document is one a validation of today would have refused, stored as an
        older release or a hand edit might have left it. Applying it must answer 422 naming
        the problem and leave no space, no stage and no transition behind."""
        headers, ws = _owner_and_workspace(client, db)
        broken = _template(db, ws, definition)
        response = _create(client, headers, ws, name="P", key="PAY", workflow_template=broken)
        assert response.status_code == 422, response.text
        assert "cannot be applied" in response.json()["message"]
        assert why in response.json()["message"]
        db.expire_all()
        assert db.query(Space).count() == 0
        assert db.query(WorkflowStatus).count() == 0
        assert db.query(WorkflowTransition).count() == 0

    def test_a_repeated_transition_is_dropped_not_fatal(self, client, db):
        """The publish drops a duplicate transition with a warning (§4.4); creating a space
        from a template that repeats one must not trip the unique constraint instead."""
        headers, ws = _owner_and_workspace(client, db)
        twice = {**THREE_STAGES, "transition_mode": "restricted",
                 "transitions": [{"from": 0, "to": 1}, {"from": 0, "to": 1},
                                 {"from": 1, "to": 2}]}
        space = pm.ok(_create(client, headers, ws, name="P", key="PAY",
                              workflow_template=_template(db, ws, twice)), 201)
        assert _flow(space) == {("Open", "Doing"), ("Doing", "Shipped")}
        assert db.query(WorkflowTransition).count() == 2

    def test_the_governance_default_applies_and_a_deleted_one_falls_back(self, client, db):
        """With no template named, the workspace's default is used; when that names a
        template that has since been deleted, the built-in default stands in rather than
        refusing every space anyone creates."""
        headers, ws = _owner_and_workspace(client, db)
        _governance(client, headers, ws, default_workflow_template="support")
        support = pm.ok(_create(client, headers, ws, name="Support", key="SUP"), 201)
        assert _names(support) == ["New", "Triage", "Investigating", "Resolved"]

        ours = _template(db, ws, THREE_STAGES)
        _governance(client, headers, ws, default_workflow_template=ours)
        team = pm.ok(_create(client, headers, ws, name="Team", key="TEAM"), 201)
        assert _names(team) == ["Open", "Doing", "Shipped"]

        db.query(WorkflowTemplate).filter(WorkflowTemplate.id == ours).update({"deleted": 1})
        db.commit()
        fallback = pm.ok(_create(client, headers, ws, name="Ops", key="OPS"), 201)
        assert _names(fallback) == ["To Do", "In Progress", "In Review", "Done"]

    def test_a_default_template_that_no_longer_applies_is_a_422_naming_it(self, client, db):
        """A default that exists but was corrupted is not silently replaced: the admin is
        told which setting to fix, and an explicit template still works."""
        headers, ws = _owner_and_workspace(client, db)
        ours = _template(db, ws, THREE_STAGES)
        _governance(client, headers, ws, default_workflow_template=ours)
        db.query(WorkflowTemplate).filter(WorkflowTemplate.id == ours).update(
            {"definition": {"stages": []}})
        db.commit()
        refused = _create(client, headers, ws, name="Team", key="TEAM")
        assert refused.status_code == 422
        assert "default workflow template" in refused.json()["message"]
        pm.ok(_create(client, headers, ws, name="Team", key="TEAM",
                      workflow_template="default"), 201)


class TestInitialLists:
    def test_initial_lists_are_created_in_order_at_the_space_root(self, client, db):
        """The answer is the full node, lists included, so the sidebar needs no second
        read; the order given is the order kept."""
        headers, ws = _owner_and_workspace(client, db)
        space = pm.ok(_create(client, headers, ws, name="Client A", key="CLA",
                              initial_lists=["Website Delivery", " Content Operations ",
                                             "Support"]), 201)
        assert [(item["name"], item["position"], item["folder_id"], item["archived"])
                for item in space["lists"]] == [
            ("Website Delivery", 0, None, False), ("Content Operations", 1, None, False),
            ("Support", 2, None, False)]
        [in_tree] = pm.ok(client.get(f"/workspaces/{ws}", headers=headers))["spaces"]
        assert [item["id"] for item in in_tree["lists"]] == [
            item["id"] for item in space["lists"]]
        # The lists take issues straight away.
        pm.issue(client, headers, ws, space["lists"][0]["id"])

    def test_at_most_ten_distinct_non_blank_names(self, client, db):
        """Ten is the design's cap; a repeated or blank name is a typo, refused before
        anything is created."""
        headers, ws = _owner_and_workspace(client, db)
        for lists in ([f"L{n}" for n in range(11)], ["Delivery", "delivery"], ["  "],
                      ["x" * 101]):
            response = _create(client, headers, ws, name="P", key="PAY", initial_lists=lists)
            assert response.status_code == 422, lists
        assert db.query(ProjectList).count() == 0
        space = pm.ok(_create(client, headers, ws, name="P", key="PAY",
                              initial_lists=[f"L{n}" for n in range(10)]), 201)
        assert len(space["lists"]) == 10


class TestKeys:
    def test_a_live_key_or_one_a_space_used_to_have_is_refused(self, client, db):
        """A retired key stays reserved: if a new space could take `PAY`, `PAY-7` in an old
        commit would name two issues. Each refusal says which case it is."""
        headers, ws = _owner_and_workspace(client, db)
        space = pm.ok(_create(client, headers, ws, name="Payments", key="PAY"), 201)
        pm.ok(client.patch(f"/workspaces/{ws}/spaces/{space['id']}", json={"key": "PAYM"},
                           headers=headers))

        retired = _create(client, headers, ws, name="Again", key="PAY")
        assert retired.status_code == 409
        assert "used to belong" in retired.json()["message"]
        live = _create(client, headers, ws, name="Again", key="PAYM")
        assert live.status_code == 409
        assert "already used" in live.json()["message"]
