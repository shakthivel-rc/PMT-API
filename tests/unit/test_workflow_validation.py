"""The workflow editor's rules, without a database (docs/design/09-restructuring.md §4.4).

`services/workflow.evaluate` is the one function both the preview and the publish run, so a
check missing here is a check missing from both: a draft that leaves a stage unreachable gets
published and its issues can never be finished, or a populated stage is removed with nowhere
for its issues to go. Every error and warning code of the §4.4 tables has a scenario below
that triggers it, and `TestTheCodesAreTheDesignsCodes` fails when the codes the service uses
and the ones the design document lists drift apart — the SPA keys its messages on them.

The template definition is the other half: it is stored and later applied to a new space
without review (services/workflow_templates.materialize trusts it), so it must refuse
whatever a publish would.
"""
import pathlib
import re
from typing import get_args

import pytest
from pydantic import ValidationError

from models.hierarchy import Space, WorkflowStatus
from schemas import workflow as schema
from services import workflow as wf
from services import workflow_templates
from utils import workflow_rules as rules

pytestmark = pytest.mark.unit

ROOT = pathlib.Path(__file__).resolve().parent.parent.parent
DESIGN = ROOT / "docs" / "design" / "09-restructuring.md"

TODO, DOING, REVIEW, DONE = "s-todo", "s-doing", "s-review", "s-done"
_DEFAULT = ((TODO, "To Do", "todo", "neutral", False),
            (DOING, "In Progress", "in_progress", "info", True),
            (REVIEW, "In Review", "in_progress", "info", True),
            (DONE, "Done", "done", "success", False))


def snapshot(held: dict[str, tuple[int, int, int]] | None = None,
             **settings: dict) -> wf.WorkflowSnapshot:
    """The default four-stage workflow. `held` maps a stage id to (issues, work items, in
    the active sprint); `settings` overrides a stage's fields by id."""
    held = held or {}
    stages = []
    for position, (stage_id, name, category, color, counts) in enumerate(_DEFAULT):
        issues, work, sprint = held.get(stage_id, (0, 0, 0))
        fields = {"id": stage_id, "name": name, "category": category, "color": color,
                  "position": position, "counts_toward_wip": counts, "wip_limit": None,
                  "issue_count": issues, "wip_count": work, "sprint_count": sprint,
                  **settings.get(stage_id.replace("-", "_"), {})}
        stages.append(wf.StageSnapshot(**fields))
    return wf.WorkflowSnapshot(stages=tuple(stages))


def stage(ref: str, name: str, category: str, **fields) -> dict:
    return {"ref": ref, "name": name, "category": category, **fields}


DEFAULT_STAGES = [stage(TODO, "To Do", "todo"), stage(DOING, "In Progress", "in_progress"),
                  stage(REVIEW, "In Review", "in_progress"), stage(DONE, "Done", "done")]


def draft(**overrides) -> schema.WorkflowDraft:
    """The current workflow, unchanged, with `overrides` applied to the body."""
    body = {"base_version": 1, "transition_mode": "any", "wip_policy": "warn",
            "stages": DEFAULT_STAGES, "initial": TODO, "transitions": [], "replacements": {},
            "merge_action": {"mode": "status", "target": DONE}}
    body.update(overrides)
    return schema.WorkflowDraft.model_validate(body)


def without(*refs: str) -> list[dict]:
    return [entry for entry in DEFAULT_STAGES if entry["ref"] not in refs]


def run(draft_body: schema.WorkflowDraft, snap: wf.WorkflowSnapshot | None = None):
    return wf.evaluate(snap or snapshot(), draft_body)


def codes(evaluation: wf.Evaluation, kind: str = "errors") -> list[str]:
    return [problem["code"] for problem in evaluation.preview[kind]]


def problem(evaluation: wf.Evaluation, code: str, kind: str = "errors") -> dict:
    return next(entry for entry in evaluation.preview[kind] if entry["code"] == code)


def flow(*pairs: tuple[str | None, str]) -> list[dict]:
    return [{"from": source, "to": target} for source, target in pairs]


# ---- one scenario per code ---------------------------------------------------------------------

#: Each code of the §4.4 tables, and the smallest draft that should produce it.
SCENARIOS = {
    "no_stages": lambda: run(draft(stages=[], initial=None)),
    "too_many_stages": lambda: run(draft(
        stages=[stage(f"new:{i}", f"S{i}", "done" if i == 20 else "todo") for i in range(21)],
        initial="new:0", merge_action={"mode": "status", "target": "new:20"},
        replacements={TODO: "new:0", DOING: "new:0", REVIEW: "new:0", DONE: "new:20"})),
    "name_invalid": lambda: run(draft(stages=[stage(TODO, "   ", "todo"), *without(TODO)])),
    "duplicate_name": lambda: run(draft(stages=[*DEFAULT_STAGES, stage("new:x", "DONE ",
                                                                       "done")])),
    "category_invalid": lambda: run(draft(stages=[*without(REVIEW),
                                                  stage(REVIEW, "In Review", "shipped")])),
    "color_invalid": lambda: run(draft(stages=[*without(REVIEW), stage(
        REVIEW, "In Review", "in_progress", color="#ff0000")])),
    "wip_limit_invalid": lambda: run(draft(stages=[*without(DOING), stage(
        DOING, "In Progress", "in_progress", wip_limit=0)])),
    "unknown_ref": lambda: run(draft(stages=[*DEFAULT_STAGES, stage("not-a-stage-id", "QA",
                                                                    "in_progress")])),
    "initial_missing": lambda: run(draft(initial=None)),
    "initial_closed": lambda: run(draft(initial=DONE)),
    "no_completed_stage": lambda: run(draft(stages=without(DONE), replacements={DONE: DOING},
                                            merge_action={"mode": "none"})),
    "self_transition": lambda: run(draft(transitions=flow((DOING, DOING)))),
    "unreachable_stage": lambda: run(draft(transition_mode="restricted",
                                           transitions=flow((TODO, DOING), (DOING, DONE)))),
    "replacement_required": lambda: run(draft(stages=without(REVIEW)),
                                        snapshot({REVIEW: (2, 2, 0)})),
    "replacement_invalid": lambda: run(draft(stages=without(DOING, REVIEW),
                                             replacements={REVIEW: DOING, DOING: TODO}),
                                       snapshot({REVIEW: (2, 2, 0)})),
    "merge_target_invalid": lambda: run(draft(merge_action={"mode": "status"})),
    "dead_end_stage": lambda: run(draft(transition_mode="restricted", transitions=flow(
        (TODO, DOING), (TODO, REVIEW), (DOING, DONE)))),
    "category_change": lambda: run(draft(stages=[*without(REVIEW), stage(REVIEW, "In Review",
                                                                         "done")])),
    "moves_into_closed": lambda: run(draft(stages=without(REVIEW),
                                           replacements={REVIEW: DONE}),
                                     snapshot({REVIEW: (3, 3, 0)})),
    "active_sprint_moves": lambda: run(draft(stages=without(REVIEW),
                                             replacements={REVIEW: DOING}),
                                       snapshot({REVIEW: (3, 3, 1)})),
    "wip_over_limit": lambda: run(draft(stages=[*without(DOING), stage(
        DOING, "In Progress", "in_progress", wip_limit=3)]), snapshot({DOING: (5, 5, 0)})),
    "merge_target_unreachable": lambda: run(draft(transition_mode="restricted", transitions=flow(
        (TODO, DOING), (DOING, REVIEW), (REVIEW, DONE)))),
    "duplicate_transition": lambda: run(draft(transitions=flow((TODO, DOING), (TODO, DOING)))),
}


class TestTheCodesAreTheDesignsCodes:
    """The codes are a contract with the SPA, like field names: it keys messages and
    highlighting on them. These read the §4.4 tables out of the design document itself."""

    @staticmethod
    def _section() -> list[str]:
        text = DESIGN.read_text(encoding="utf-8")
        start = text.index("### 4.4")
        return text[start:text.index("### 4.5", start)].splitlines()

    def test_the_error_codes_are_exactly_the_errors_table(self):
        lines = self._section()
        first = next(i for i, line in enumerate(lines) if line.startswith("Errors"))
        last = next(i for i, line in enumerate(lines) if line.startswith("Warnings"))
        documented = set()
        for line in lines[first:last]:
            if line.startswith("|") and not line.startswith("|---"):
                documented |= set(re.findall(r"`([a-z_]+)`", line.split("|")[1]))
        documented.discard("code")
        assert documented == wf.ERROR_CODES

    def test_the_warning_codes_are_exactly_the_warnings_list(self):
        lines = self._section()
        start = next(i for i, line in enumerate(lines) if line.startswith("Warnings"))
        paragraph = []
        for line in lines[start:]:
            if not line.strip():
                break
            paragraph.append(line)
        # Backticked words inside the parentheses are explanation, not codes.
        prose = re.sub(r"\([^)]*\)", "", " ".join(paragraph))
        assert set(re.findall(r"`([a-z_]+)`", prose)) == wf.WARNING_CODES

    def test_every_code_has_a_scenario(self):
        assert set(SCENARIOS) == wf.ERROR_CODES | wf.WARNING_CODES

    @pytest.mark.parametrize("code", sorted(SCENARIOS))
    def test_the_scenario_produces_its_code(self, code):
        evaluation = SCENARIOS[code]()
        kind = "errors" if code in wf.ERROR_CODES else "warnings"
        assert code in codes(evaluation, kind), evaluation.preview[kind]
        # A warning never blocks: each warning scenario is otherwise a publishable draft.
        assert evaluation.valid is (kind == "warnings"), evaluation.preview["errors"]

    @pytest.mark.parametrize("code", sorted(SCENARIOS))
    def test_no_scenario_emits_an_undocumented_code(self, code):
        evaluation = SCENARIOS[code]()
        assert set(codes(evaluation)) <= wf.ERROR_CODES
        assert set(codes(evaluation, "warnings")) <= wf.WARNING_CODES

    @pytest.mark.parametrize("code", sorted(SCENARIOS))
    def test_every_problem_has_a_message_and_string_refs(self, code):
        """A problem with no message is a red box with nothing to say; a None ref breaks
        the response model."""
        evaluation = SCENARIOS[code]()
        for entry in evaluation.preview["errors"] + evaluation.preview["warnings"]:
            assert entry["message"].strip()
            assert all(isinstance(ref, str) and ref for ref in entry["refs"])


# ---- the current workflow, and what counts as a change ------------------------------------------


class TestUnchanged:
    def test_the_current_workflow_as_a_draft_is_valid_and_changes_nothing(self):
        """What the editor sends before anyone has touched anything must preview clean, or
        every edit starts with a false alarm."""
        evaluation = run(draft(), snapshot({TODO: (4, 3, 1)}))
        preview = evaluation.preview
        assert preview["valid"] and preview["errors"] == [] and preview["warnings"] == []
        assert [s["change"] for s in preview["stages"]] == ["unchanged"] * 4
        assert [s["id"] for s in preview["stages"]] == [TODO, DOING, REVIEW, DONE]
        assert preview["removed"] == [] and preview["category_changes"] == []
        assert preview["moves"] == {"total": 0, "into_closed": 0, "in_active_sprint": 0}
        assert preview["affected_issue_count"] == 0
        assert preview["requires_reporting_ack"] is False
        assert preview["stages"][0]["issue_count"] == 4

    def test_omitted_settings_keep_what_an_existing_stage_has(self):
        """An editor that only renames must not wipe a WIP limit it never displayed."""
        snap = snapshot(s_doing={"color": "warning", "wip_limit": 4,
                                 "counts_toward_wip": False})
        [_, doing, *_] = run(draft(), snap).preview["stages"]
        assert (doing["color"], doing["wip_limit"], doing["counts_toward_wip"]) == (
            "warning", 4, False)
        assert doing["change"] == "unchanged"

    def test_explicit_null_clears_a_limit_and_resets_a_colour(self):
        snap = snapshot(s_doing={"color": "warning", "wip_limit": 4})
        body = draft(stages=[DEFAULT_STAGES[0], stage(DOING, "In Progress", "in_progress",
                                                      color=None, wip_limit=None),
                             *DEFAULT_STAGES[2:]])
        [_, doing, *_] = run(body, snap).preview["stages"]
        assert (doing["color"], doing["wip_limit"], doing["change"]) == ("info", None,
                                                                         "updated")

    def test_a_new_stage_takes_its_categorys_defaults(self):
        body = draft(stages=[*DEFAULT_STAGES, stage("new:qa", "QA", "in_progress"),
                             stage("new:wont", "Won't do", "canceled")])
        *_, qa, wont = run(body).preview["stages"]
        assert (qa["id"], qa["change"], qa["color"], qa["counts_toward_wip"]) == (
            None, "added", "info", True)
        assert (wont["color"], wont["counts_toward_wip"], wont["position"]) == (
            "neutral", False, 5)

    def test_a_rename_is_reported_with_the_old_name(self):
        body = draft(stages=[*without(REVIEW, DONE), stage(REVIEW, "  Code review ",
                                                           "in_progress"),
                             DEFAULT_STAGES[3]])
        review = run(body).preview["stages"][2]
        assert (review["name"], review["change"], review["previous_name"]) == (
            "Code review", "renamed", "In Review")

    def test_dragging_one_column_marks_only_that_column(self):
        """Comparing positions would mark all four; the longest run that kept its order
        marks the one that was dragged."""
        body = draft(stages=[DEFAULT_STAGES[3], *DEFAULT_STAGES[:3]])
        changes = {s["ref"]: s["change"] for s in run(body).preview["stages"]}
        assert changes == {DONE: "updated", TODO: "unchanged", DOING: "unchanged",
                           REVIEW: "unchanged"}

    def test_removing_a_stage_does_not_mark_the_ones_after_it(self):
        body = draft(stages=without(TODO), initial=DOING, replacements={TODO: DOING})
        assert [s["change"] for s in run(body).preview["stages"]] == ["unchanged"] * 3
        assert [s["position"] for s in run(body).preview["stages"]] == [0, 1, 2]


# ---- stages --------------------------------------------------------------------------------------


class TestStages:
    def test_no_stages_is_the_only_problem_it_reports(self):
        """Every other check would only restate it — the moves out of every stage included."""
        evaluation = run(draft(stages=[], initial=None), snapshot({DOING: (2, 2, 2)}))
        assert codes(evaluation) == ["no_stages"]
        assert evaluation.preview["warnings"] == []
        assert evaluation.preview["affected_issue_count"] == 2

    def test_twenty_stages_is_allowed(self):
        body = draft(stages=[stage(f"new:{i}", f"S{i}", "done" if i == 19 else "todo")
                             for i in range(20)], initial="new:0",
                     merge_action={"mode": "status", "target": "new:19"},
                     replacements={TODO: "new:0", DOING: "new:0", REVIEW: "new:0",
                                   DONE: "new:19"})
        assert "too_many_stages" not in codes(run(body))

    def test_a_name_over_fifty_characters_is_invalid(self):
        body = draft(stages=[stage(TODO, "x" * 51, "todo"), *without(TODO)])
        assert problem(run(body), "name_invalid")["refs"] == [TODO]
        fifty = draft(stages=[stage(TODO, "x" * 50, "todo"), *without(TODO)])
        assert "name_invalid" not in codes(run(fifty))

    def test_duplicate_names_ignore_case_and_name_every_stage_involved(self):
        evaluation = SCENARIOS["duplicate_name"]()
        assert problem(evaluation, "duplicate_name")["refs"] == [DONE, "new:x"]

    @pytest.mark.parametrize("limit", [0, -1, 1000])
    def test_a_wip_limit_is_between_one_and_999(self, limit):
        body = draft(stages=[*without(DOING), stage(DOING, "In Progress", "in_progress",
                                                    wip_limit=limit)])
        assert codes(run(body)) == ["wip_limit_invalid"]

    @pytest.mark.parametrize("limit", [1, 999])
    def test_the_ends_of_the_wip_range_are_valid(self, limit):
        body = draft(stages=[*without(DOING), stage(DOING, "In Progress", "in_progress",
                                                    wip_limit=limit)])
        assert run(body).valid

    def test_a_ref_that_is_not_a_live_stage_and_not_new_is_unknown(self):
        """A stale id from another space, or a stage removed since: guessing what it meant
        would put issues in the wrong place."""
        assert problem(SCENARIOS["unknown_ref"](), "unknown_ref")["refs"] == ["not-a-stage-id"]

    def test_a_canceled_stage_does_not_count_as_completed(self):
        """Canceled is closed, not delivered: a workflow of To Do and Canceled could never
        deliver anything."""
        body = draft(stages=[stage(TODO, "To Do", "todo"), stage(DONE, "Dropped", "canceled")],
                     replacements={DOING: TODO, REVIEW: TODO}, merge_action={"mode": "none"})
        assert "no_completed_stage" in codes(run(body))


# ---- the initial stage and the flow -------------------------------------------------------------


class TestFlow:
    @pytest.mark.parametrize("initial,code", [("new:ghost", "unknown_ref"),
                                              (REVIEW, "unknown_ref")])
    def test_an_initial_stage_outside_the_draft_is_unknown(self, initial, code):
        body = draft(stages=without(REVIEW), initial=initial)
        assert code in codes(run(body))

    def test_an_initial_stage_being_removed_is_named(self):
        body = draft(stages=without(TODO), initial=TODO, replacements={TODO: DOING})
        assert "To Do" in problem(run(body), "unknown_ref")["message"]

    def test_a_canceled_initial_stage_is_closed_too(self):
        body = draft(stages=[stage("new:x", "Dropped", "canceled"), *DEFAULT_STAGES],
                     initial="new:x")
        assert "initial_closed" in codes(run(body))

    def test_unknown_transition_ends_are_reported_once_each(self):
        body = draft(transitions=flow((TODO, "new:ghost"), ("new:nope", DONE)))
        assert [p["refs"] for p in run(body).preview["errors"]] == [["new:ghost"], ["new:nope"]]

    def test_a_transition_into_a_removed_stage_is_named(self):
        body = draft(stages=without(REVIEW), transitions=flow((DOING, REVIEW)))
        assert "In Review" in problem(run(body), "unknown_ref")["message"]

    def test_any_mode_never_asks_for_reachability(self):
        """Column order alone imposes nothing in `any` mode (plan §4)."""
        evaluation = run(draft(transition_mode="any"))
        assert evaluation.valid and evaluation.preview["warnings"] == []

    def test_restricted_mode_names_every_unreachable_stage(self):
        body = draft(transition_mode="restricted", transitions=flow((TODO, DONE)))
        unreachable = [p["refs"] for p in run(body).preview["errors"]
                       if p["code"] == "unreachable_stage"]
        assert unreachable == [[DOING], [REVIEW]]

    def test_branching_skipping_and_reopening_are_just_transitions(self):
        body = draft(transition_mode="restricted", transitions=flow(
            (TODO, DOING), (TODO, DONE), (DOING, REVIEW), (REVIEW, DOING), (REVIEW, DONE),
            (DONE, DOING)))
        assert run(body).valid

    def test_a_global_transition_makes_its_target_reachable_and_rescues_dead_ends(self):
        body = draft(transition_mode="restricted", transitions=flow(
            (TODO, DOING), (DOING, REVIEW), (None, DONE)))
        evaluation = run(body)
        assert evaluation.valid and "dead_end_stage" not in codes(evaluation, "warnings")

    def test_a_closed_stage_may_be_a_dead_end(self):
        body = draft(transition_mode="restricted",
                     transitions=flow((TODO, DOING), (DOING, REVIEW), (REVIEW, DONE)))
        assert "dead_end_stage" not in codes(run(body), "warnings")

    def test_transitions_are_deduplicated_and_named_as_they_will_be_stored(self):
        body = draft(transitions=flow((TODO, DOING), (TODO, DOING), (None, DONE)))
        evaluation = run(body)
        assert evaluation.preview["transitions"] == [
            {"from": TODO, "to": DOING, "from_name": "To Do", "to_name": "In Progress"},
            {"from": None, "to": DONE, "from_name": None, "to_name": "Done"}]
        assert evaluation.transitions == ((TODO, DOING), (None, DONE))
        assert len(evaluation.preview["warnings"]) == 1

    def test_a_self_transition_is_refused_even_in_any_mode(self):
        assert codes(SCENARIOS["self_transition"]()) == ["self_transition"]


# ---- removing stages ----------------------------------------------------------------------------


class TestRemoval:
    def test_an_empty_stage_can_be_removed_without_a_replacement(self):
        evaluation = run(draft(stages=without(REVIEW)))
        assert evaluation.valid
        assert evaluation.preview["removed"] == [{
            "id": REVIEW, "name": "In Review", "category": "in_progress", "issue_count": 0,
            "replacement_ref": None, "replacement_name": None}]

    def test_a_populated_stage_needs_a_replacement_naming_its_count(self):
        evaluation = SCENARIOS["replacement_required"]()
        found = problem(evaluation, "replacement_required")
        assert found["refs"] == [REVIEW] and "2 issues" in found["message"]

    def test_the_replacement_may_be_a_new_stage(self):
        body = draft(stages=[*without(REVIEW), stage("new:qa", "QA", "in_progress")],
                     replacements={REVIEW: "new:qa"})
        evaluation = run(body, snapshot({REVIEW: (2, 2, 0)}))
        assert evaluation.valid
        assert evaluation.preview["removed"][0]["replacement_name"] == "QA"
        assert evaluation.removals[0].replacement == "new:qa"

    def test_a_replacement_that_names_nothing_is_unknown(self):
        body = draft(stages=without(REVIEW), replacements={REVIEW: "new:ghost"})
        assert codes(run(body, snapshot({REVIEW: (1, 1, 0)}))) == ["unknown_ref"]

    def test_a_replacement_for_a_stage_that_stays_is_ignored(self):
        assert run(draft(replacements={DOING: DONE})).valid

    def test_every_issue_that_moves_is_counted(self):
        body = draft(stages=without(DOING, REVIEW), replacements={DOING: TODO, REVIEW: DONE})
        preview = run(body, snapshot({DOING: (2, 1, 1), REVIEW: (3, 3, 0)})).preview
        assert preview["affected_issue_count"] == 5
        assert preview["moves"] == {"total": 5, "into_closed": 3, "in_active_sprint": 1}

    def test_moving_into_canceled_says_it_is_not_delivered(self):
        body = draft(stages=[*DEFAULT_STAGES[:2], DEFAULT_STAGES[3],
                             stage("new:x", "Dropped", "canceled")],
                     replacements={REVIEW: "new:x"})
        found = problem(run(body, snapshot({REVIEW: (2, 2, 0)})), "moves_into_closed",
                        "warnings")
        assert "without counting as delivered" in found["message"]

    def test_moving_into_done_says_it_counts_as_delivered(self):
        found = problem(SCENARIOS["moves_into_closed"](), "moves_into_closed", "warnings")
        assert "count as delivered" in found["message"] and found["refs"] == [DONE]


# ---- categories, WIP, the merge action ----------------------------------------------------------


class TestReporting:
    def test_a_category_change_on_an_empty_stage_warns_but_needs_no_ack(self):
        evaluation = SCENARIOS["category_change"]()
        assert evaluation.preview["requires_reporting_ack"] is False
        assert evaluation.preview["category_changes"] == [{
            "id": REVIEW, "name": "In Review", "from_category": "in_progress",
            "to_category": "done", "issue_count": 0}]

    def test_a_category_change_on_a_populated_stage_needs_the_ack(self):
        body = draft(stages=[*without(REVIEW), stage(REVIEW, "In Review", "done")])
        evaluation = run(body, snapshot({REVIEW: (2, 2, 0)}))
        assert evaluation.valid and evaluation.preview["requires_reporting_ack"] is True
        assert evaluation.preview["stages"][3]["previous_category"] == "in_progress"

    def test_wip_counts_work_moving_in(self):
        body = draft(stages=[*without(DOING, REVIEW),
                             stage(DOING, "In Progress", "in_progress", wip_limit=4)],
                     replacements={REVIEW: DOING})
        snap = snapshot({DOING: (3, 3, 0), REVIEW: (2, 2, 0)}, s_doing={"wip_limit": 4})
        found = problem(run(body, snap), "wip_over_limit", "warnings")
        assert "5 work items" in found["message"]

    def test_an_unchanged_limit_already_exceeded_is_not_news(self):
        snap = snapshot({DOING: (5, 5, 0)}, s_doing={"wip_limit": 3})
        assert "wip_over_limit" not in codes(run(draft(), snap), "warnings")

    def test_only_work_items_count_toward_the_limit(self):
        """Epics and subtasks in a stage are not work in progress (§4.7)."""
        body = draft(stages=[*without(DOING), stage(DOING, "In Progress", "in_progress",
                                                    wip_limit=3)])
        assert "wip_over_limit" not in codes(run(body, snapshot({DOING: (9, 3, 0)})),
                                             "warnings")

    def test_the_block_policy_says_what_it_will_do(self):
        body = draft(wip_policy="block", stages=[*without(DOING), stage(
            DOING, "In Progress", "in_progress", wip_limit=3)])
        found = problem(run(body, snapshot({DOING: (5, 5, 0)})), "wip_over_limit", "warnings")
        assert "block policy" in found["message"]


class TestMergeAction:
    def test_a_canceled_target_is_refused(self):
        body = draft(stages=[*DEFAULT_STAGES, stage("new:x", "Dropped", "canceled")],
                     merge_action={"mode": "status", "target": "new:x"})
        assert codes(run(body)) == ["merge_target_invalid"]
        assert run(body).preview["integration"]["valid"] is False

    def test_removing_the_target_forces_a_choice(self):
        body = draft(stages=[*without(DONE), stage("new:shipped", "Shipped", "done")],
                     replacements={DONE: "new:shipped"})
        evaluation = run(body)
        assert codes(evaluation) == ["merge_target_invalid"]
        assert "Done" in evaluation.preview["integration"]["message"]

    @pytest.mark.parametrize("merge", [{"mode": "none"},
                                       {"mode": "status", "target": "new:shipped"}])
    def test_turning_it_off_or_choosing_a_new_target_resolves_it(self, merge):
        body = draft(stages=[*without(DONE), stage("new:shipped", "Shipped", "done")],
                     replacements={DONE: "new:shipped"}, merge_action=merge)
        assert run(body).valid

    def test_a_target_that_names_nothing_is_unknown(self):
        body = draft(merge_action={"mode": "status", "target": "new:ghost"})
        assert codes(run(body)) == ["unknown_ref"]

    def test_an_active_target_is_valid_and_says_acceptance_is_still_needed(self):
        """The plan's migration gate: a merge may move work to QA instead of completing it."""
        body = draft(merge_action={"mode": "status", "target": REVIEW})
        integration = run(body).preview["integration"]
        assert integration["valid"] and integration["target_name"] == "In Review"
        assert "not a Completed stage" in integration["message"]

    def test_mode_none_ignores_a_target(self):
        evaluation = run(draft(merge_action={"mode": "none", "target": "new:ghost"}))
        assert evaluation.valid and evaluation.merge_target is None

    def test_unreachable_from_lists_open_stages_without_a_direct_move(self):
        """A merge is one move, so what matters is a direct transition, not a chain."""
        integration = SCENARIOS["merge_target_unreachable"]().preview["integration"]
        assert integration["unreachable_from"] == ["To Do", "In Progress"]
        assert integration["valid"] is True


class TestEvaluationForPublish:
    def test_it_carries_what_the_publish_applies(self):
        body = draft(stages=[*without(REVIEW), stage("new:qa", "QA", "in_progress")],
                     replacements={REVIEW: "new:qa"}, transitions=flow((TODO, "new:qa")),
                     merge_action={"mode": "status", "target": "new:qa"})
        evaluation = run(body, snapshot({REVIEW: (1, 1, 0)}))
        assert evaluation.valid
        assert [s.ref for s in evaluation.stages] == [TODO, DOING, DONE, "new:qa"]
        assert [(r.stage.id, r.replacement) for r in evaluation.removals] == [(REVIEW,
                                                                              "new:qa")]
        assert evaluation.transitions == ((TODO, "new:qa"),)
        assert (evaluation.initial, evaluation.merge_mode, evaluation.merge_target) == (
            TODO, "status", "new:qa")


# ---- the request schemas ------------------------------------------------------------------------


class TestDraftSchema:
    def test_the_literal_types_spell_the_rules_vocabulary(self):
        """Literals are spelled out for the type checker; they must not drift from the
        tuples every other reader uses."""
        assert get_args(schema.StageCategory) == rules.STATUS_CATEGORIES
        assert get_args(schema.StageColor) == rules.STAGE_COLORS
        assert get_args(schema.TransitionMode) == rules.TRANSITION_MODES
        assert get_args(schema.WipPolicy) == rules.WIP_POLICIES
        assert get_args(schema.MergeMode) == rules.MERGE_ACTIONS

    def test_two_stages_with_one_ref_are_not_a_workflow(self):
        with pytest.raises(ValidationError):
            draft(stages=[*DEFAULT_STAGES, stage(TODO, "Again", "todo")])

    def test_what_the_preview_reports_is_not_refused_here(self):
        """Blank names, unknown categories and bad limits must reach the preview, which
        says what is wrong with the stage's ref beside it."""
        body = draft(stages=[stage("new:a", "", "nonsense", color="plaid", wip_limit=0)])
        assert body.stages[0].category == "nonsense"

    def test_a_body_that_is_not_a_workflow_is_refused(self):
        with pytest.raises(ValidationError):
            draft(transition_mode="sometimes")
        with pytest.raises(ValidationError):
            draft(stages=[stage(f"new:{i}", f"S{i}", "todo")
                          for i in range(schema.DRAFT_MAX_STAGES + 1)])
        with pytest.raises(ValidationError):
            draft(merge_action={"mode": "first_done"})

    def test_the_publish_body_needs_the_confirmed_count(self):
        body = draft().model_dump(by_alias=True)
        with pytest.raises(ValidationError):
            schema.WorkflowPublish.model_validate(body)
        published = schema.WorkflowPublish.model_validate({**body,
                                                           "confirmed_affected_issues": 0})
        assert published.acknowledge_reporting_changes is False


class TestTemplateDefinition:
    @pytest.mark.parametrize("key", sorted(workflow_templates.BUILTIN_TEMPLATES))
    def test_every_builtin_is_a_valid_template(self, key):
        """The listing validates them on every read: an invalid built-in would be a 500."""
        definition = schema.TemplateDefinition.model_validate(
            workflow_templates.builtin_definition(key))
        assert definition.stages

    def test_defaults_are_filled_in_and_duplicates_dropped(self):
        definition = schema.TemplateDefinition.model_validate({
            "stages": [{"name": " To Do ", "category": "todo"},
                       {"name": "Doing", "category": "in_progress"},
                       {"name": "Done", "category": "done"}],
            "transitions": [{"from": 0, "to": 1}, {"from": 0, "to": 1}],
            "merge_target": 2})
        dumped = definition.model_dump(by_alias=True)
        assert dumped["stages"][0] == {"name": "To Do", "category": "todo", "color": "neutral",
                                       "counts_toward_wip": False, "wip_limit": None}
        assert dumped["stages"][1]["counts_toward_wip"] is True
        assert dumped["transitions"] == [{"from": 0, "to": 1}]

    @pytest.mark.parametrize("change", [
        {"stages": [{"name": "To Do", "category": "todo"}, {"name": "Done", "category": "done"},
                    {"name": " to do", "category": "todo"}]},
        {"initial": 5},
        {"initial": 1},
        {"stages": [{"name": "A", "category": "todo"}, {"name": "B", "category": "in_progress"}],
         "merge_target": None},
        {"transitions": [{"from": 0, "to": 5}]},
        {"transitions": [{"from": 7, "to": 1}]},
        {"transitions": [{"from": 1, "to": 1}]},
        {"merge_target": 9},
        {"stages": [{"name": "A", "category": "todo"}, {"name": "B", "category": "done"},
                    {"name": "C", "category": "canceled"}], "merge_target": 2},
        {"transition_mode": "restricted", "transitions": [{"from": 0, "to": 1}]},
        {"stages": [{"name": "  ", "category": "todo"}, {"name": "B", "category": "done"}]},
        {"stages": []},
        {"stages": [{"name": "A", "category": "shipped"}, {"name": "B", "category": "done"}]},
    ], ids=["duplicate-names", "initial-out-of-range", "initial-closed", "no-done-stage",
            "transition-to-out-of-range", "transition-from-out-of-range", "self-transition",
            "merge-target-out-of-range", "canceled-merge-target", "restricted-unreachable",
            "blank-name", "no-stages", "unknown-category"])
    def test_whatever_a_publish_would_refuse_a_template_refuses(self, change):
        """A template is applied to a new space without review; it must be a workflow that
        space could have published. Each case breaks one rule of an otherwise valid body
        (`test_the_base_of_those_cases_is_valid`), so each refusal is that rule's."""
        body = {"stages": [{"name": "To Do", "category": "todo"},
                           {"name": "Done", "category": "done"},
                           {"name": "Doing", "category": "in_progress"}],
                "initial": 0, "merge_target": 1, **change}
        with pytest.raises(ValidationError):
            schema.TemplateDefinition.model_validate(body)

    def test_the_base_of_those_cases_is_valid(self):
        schema.TemplateDefinition.model_validate({
            "stages": [{"name": "To Do", "category": "todo"},
                       {"name": "Done", "category": "done"},
                       {"name": "Doing", "category": "in_progress"}],
            "initial": 0, "merge_target": 1})

    def test_a_restricted_template_with_a_complete_flow_is_valid(self):
        schema.TemplateDefinition.model_validate({
            "transition_mode": "restricted",
            "stages": [{"name": "To Do", "category": "todo"},
                       {"name": "Done", "category": "done"}],
            "transitions": [{"from": 0, "to": 1}, {"from": None, "to": 0}]})

    def test_a_spaces_workflow_round_trips_through_a_template(self):
        """What "save as template" stores must be what `materialize` builds back."""
        statuses = [WorkflowStatus(id=sid, name=name, category=category, color=color,
                                   counts_toward_wip=counts, wip_limit=None)
                    for sid, name, category, color, counts in _DEFAULT]
        space = Space(id="space-1", transition_mode="restricted", wip_policy="block",
                      initial_status_id=TODO, merge_action="status",
                      merge_target_status_id=REVIEW)
        definition = workflow_templates.definition_of_space(
            statuses, space, [(TODO, DOING), (DOING, REVIEW), (REVIEW, DONE), (None, TODO)])
        validated = schema.TemplateDefinition.model_validate(definition)
        assert validated.model_dump(by_alias=True) == definition


class TestTemplateCreate:
    def test_exactly_one_source(self):
        definition = workflow_templates.builtin_definition("default")
        with pytest.raises(ValidationError):
            schema.WorkflowTemplateCreate.model_validate({"name": "X"})
        with pytest.raises(ValidationError):
            schema.WorkflowTemplateCreate.model_validate(
                {"name": "X", "from_space_id": "s", "definition": definition})
        created = schema.WorkflowTemplateCreate.model_validate(
            {"name": "  Mine ", "definition": definition})
        assert created.name == "Mine"

    def test_a_blank_name_is_refused(self):
        with pytest.raises(ValidationError):
            schema.WorkflowTemplateCreate.model_validate({"name": "   ",
                                                          "from_space_id": "s"})

    def test_at_most_fifty_editors(self):
        with pytest.raises(ValidationError):
            schema.WorkflowEditorsUpdate.model_validate(
                {"user_ids": [f"u{i}" for i in range(schema.MAX_WORKFLOW_EDITORS + 1)]})
        assert schema.WorkflowEditorsUpdate.model_validate({"user_ids": []}).user_ids == []


class TestBuiltinRefs:
    @pytest.mark.parametrize("ref,key", [("default", "default"), ("builtin:support", "support"),
                                         ("builtin:nope", None), ("delivery", "delivery"),
                                         ("3f2a-template-id", None)])
    def test_builtin_key(self, ref, key):
        assert wf.builtin_key(ref) == key
