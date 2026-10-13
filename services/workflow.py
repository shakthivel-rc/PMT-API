"""Changing a space's workflow: preview a draft, publish it; templates; who may edit.

docs/design/09-restructuring.md §4.4-§4.6. `services/transitions.py` is how the rest of the API
*obeys* a workflow; this module is the one place a workflow is *changed*, so the guarantees a
reader of transitions.py relies on are made here, on every publish: the live stages of a space
are contiguous (positions 0..n-1), `initial_status_id` is one of them, and
`merge_target_status_id` is one of them or the merge action is `none`.

**Two halves, split on purpose.**

* `evaluate` is **pure**. It takes a `WorkflowSnapshot` — the space's live stages and how many
  issues each holds, nothing more — and a draft, and returns the whole preview: every error
  and warning of the §4.4 tables, the resolved stages, what moves where. No session and no
  ORM, so tests/unit/test_workflow_validation.py covers every code without a database, and
  the preview and the publish cannot disagree about what is valid: the publish calls the same
  function again, on a snapshot read under the space's row lock.
* Everything else **reads or writes the database**, and is thin: the snapshot (one grouped
  count query), applying an evaluation, templates, editors.

Every query carries the workspace id, primary-key lookups included (services/workspace.py).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from pydantic import ValidationError
from sqlalchemy import and_, case, func
from sqlalchemy.orm import Session

from dependencies.workspace import WorkspaceAccess
from models.hierarchy import (
    Space,
    SpaceWorkflowEditor,
    WorkflowRevision,
    WorkflowStatus,
    WorkflowTemplate,
    WorkflowTransition,
)
from models.issue import Issue
from models.user import User
from models.workspace import WorkspaceMember
from schemas.workflow import TemplateDefinition, WorkflowDraft
from services import issue as issue_svc
from services import sprint as sprint_svc
from services import transitions, workflow_access, workflow_templates
from utils import workflow_rules as rules
from utils.rank import rank_after

logger = logging.getLogger("base_project.workflow")

#: The codes of the §4.4 tables and no others: the SPA keys its messages and highlighting on
#: them, so a code is a contract like a field name. tests/unit/test_workflow_validation.py
#: reads the tables out of the design document and fails when these sets and the tables
#: disagree, in either direction.
ERROR_CODES: frozenset[str] = frozenset({
    "no_stages", "too_many_stages", "name_invalid", "duplicate_name", "category_invalid",
    "color_invalid", "wip_limit_invalid", "unknown_ref", "initial_missing", "initial_closed",
    "no_completed_stage", "self_transition", "unreachable_stage", "replacement_required",
    "replacement_invalid", "merge_target_invalid",
})
WARNING_CODES: frozenset[str] = frozenset({
    "dead_end_stage", "category_change", "moves_into_closed", "active_sprint_moves",
    "wip_over_limit", "merge_target_unreachable", "duplicate_transition",
})

#: How a built-in template is named where a workspace template's id would go: the key alone
#: (`default`) or with this prefix (`builtin:default`), which is the id the listing gives it.
BUILTIN_PREFIX = "builtin:"

_LABEL = rules.CATEGORY_LABELS


# ---- the snapshot and the evaluation (pure) -------------------------------------------------


@dataclass(frozen=True)
class StageSnapshot:
    """A live stage, as validation needs it, and what it holds now."""

    id: str
    name: str
    category: str
    color: str = "neutral"
    position: int = 0
    counts_toward_wip: bool = False
    wip_limit: int | None = None
    #: Live issues in it, of every type: all of them must move if it is removed.
    issue_count: int = 0
    #: Of those, the story, task and bug issues (utils/workflow_rules.WIP_TYPES).
    wip_count: int = 0
    #: Of those, the ones in the space's active sprint.
    sprint_count: int = 0


@dataclass(frozen=True)
class WorkflowSnapshot:
    """The workflow a draft is checked against: the space's live stages, in board order."""

    stages: tuple[StageSnapshot, ...] = ()


@dataclass(frozen=True)
class ResolvedStage:
    """A stage of the draft as it would be stored, and how it differs from now."""

    ref: str
    id: str | None
    name: str
    category: str
    color: str
    position: int
    counts_toward_wip: bool
    wip_limit: int | None
    change: str
    previous_name: str | None
    previous_category: str | None
    issue_count: int


@dataclass(frozen=True)
class Removal:
    """A live stage the draft leaves out, and the draft ref its issues move to (None when
    none was given — valid only while it holds no issues)."""

    stage: StageSnapshot
    replacement: str | None


@dataclass(frozen=True)
class Evaluation:
    """`evaluate`'s answer: the preview payload, and the resolved draft a publish applies."""

    preview: dict[str, Any]
    stages: tuple[ResolvedStage, ...]
    removals: tuple[Removal, ...]
    #: Draft refs, deduplicated, every one resolving, no self-transitions.
    transitions: tuple[tuple[str | None, str], ...]
    initial: str | None
    merge_mode: str
    merge_target: str | None

    @property
    def valid(self) -> bool:
        return not self.preview["errors"]


class _Problems:
    def __init__(self) -> None:
        self.errors: list[dict[str, Any]] = []
        self.warnings: list[dict[str, Any]] = []

    @staticmethod
    def _entry(code: str, message: str, refs: tuple[str | None, ...]) -> dict[str, Any]:
        return {"code": code, "message": message,
                "refs": [ref for ref in dict.fromkeys(refs) if ref]}

    def error(self, code: str, message: str, *refs: str | None) -> None:
        self.errors.append(self._entry(code, message, refs))

    def warn(self, code: str, message: str, *refs: str | None) -> None:
        self.warnings.append(self._entry(code, message, refs))


def _plural(count: int, noun: str) -> str:
    return f"{count} {noun}" if count == 1 else f"{count} {noun}s"


def _reordered(kept: list[str], old_index: dict[str, int]) -> set[str]:
    """The kept stages whose place in the order changed.

    Everything outside one longest run of kept stages that is still in its old relative
    order. Comparing positions instead would mark every stage after a removed one as moved,
    and comparing each stage's rank among the kept ones would mark three columns when one was
    dragged past them; the longest increasing run marks the one that was dragged. At most a
    hundred stages reach here (schemas/workflow.DRAFT_MAX_STAGES), so the quadratic version
    is the readable one.
    """
    sequence = [old_index[ref] for ref in kept]
    count = len(sequence)
    if count < 2:
        return set()
    length = [1] * count
    previous = [-1] * count
    for i in range(count):
        for j in range(i):
            if sequence[j] < sequence[i] and length[j] + 1 > length[i]:
                length[i], previous[i] = length[j] + 1, j
    end = max(range(count), key=lambda i: length[i])
    in_order: set[int] = set()
    while end != -1:
        in_order.add(end)
        end = previous[end]
    return {kept[i] for i in range(count) if i not in in_order}


def evaluate(snapshot: WorkflowSnapshot, draft: WorkflowDraft) -> Evaluation:
    """Everything publishing `draft` over `snapshot` would do, and everything wrong with it.

    The checks follow the §4.4 tables, one method of `_Evaluator` per concern, in the order
    a person fixes things: the stages, the flow, the removals, the merge action, then what
    publishing would do to reports, issues and WIP. Two readings of the tables are decisions,
    recorded here:

    * A **ref that resolves to nothing** is `unknown_ref` wherever it appears — a stage whose
      ref is neither `new:...` nor a live stage of the space, a transition end, the initial
      stage, a replacement, the merge target. A ref that resolves to a stage **being
      removed** gets the code of the place it is used in: `replacement_invalid` for a
      replacement, `merge_target_invalid` for the merge target, and `unknown_ref` (with the
      stage named) for the initial stage and transitions, which have no code of their own.
    * `wip_over_limit` compares a limit with what the stage will hold once the moves are
      done, and fires only when that limit is new or changed, or work moves in: a stage that
      was over an unchanged limit yesterday is the board's to show, not news about the draft.

    A draft with no stages reports `no_stages` and nothing else: every other check would only
    restate it.
    """
    return _Evaluator(snapshot, draft).run()


class _Evaluator:
    """One evaluation: the snapshot and the draft, what has been resolved, what is wrong."""

    def __init__(self, snapshot: WorkflowSnapshot, draft: WorkflowDraft) -> None:
        self.draft = draft
        self.problems = _Problems()
        self.live = {stage.id: stage for stage in snapshot.stages}
        self.old_index = {stage.id: index for index, stage in enumerate(snapshot.stages)}
        self.refs = [stage.ref for stage in draft.stages]
        in_draft = set(self.refs)
        self.removed = [stage for stage in snapshot.stages if stage.id not in in_draft]
        self.removed_ids = {stage.id for stage in self.removed}
        self.restricted = draft.transition_mode == "restricted"
        self.stages: list[ResolvedStage] = []
        self.by_ref: dict[str, ResolvedStage] = {}
        self.pairs: list[tuple[str | None, str]] = []
        self.removals = [Removal(stage=stage, replacement=draft.replacements.get(stage.id))
                         for stage in self.removed]
        self.merge_mode = draft.merge_action.mode
        self.merge_target = draft.merge_action.target if self.merge_mode == "status" else None
        self.merge_problem: str | None = None
        self.unreachable_from: list[str] = []
        self.category_changes: list[dict[str, Any]] = []
        self.requires_ack = False
        #: Per replacement ref: (issues, work items) moving into it.
        self.inflow: dict[str, tuple[int, int]] = {}

    def run(self) -> Evaluation:
        self.resolve_stages()
        if self.draft.stages:
            self.check_names()
            self.check_initial()
            self.check_transitions()
            self.check_reachability()
            self.check_removals()
            self.check_merge_action()
            self.check_reporting()
            self.check_moves()
            self.check_wip()
        initial = self.draft.initial
        return Evaluation(preview=self.preview(), stages=tuple(self.stages),
                          removals=tuple(self.removals), transitions=tuple(self.pairs),
                          initial=initial if initial in self.by_ref else None,
                          merge_mode=self.merge_mode, merge_target=self.merge_target)

    def name_of(self, ref: str | None) -> str:
        if ref is None:
            return "Any stage"
        if ref in self.by_ref:
            return self.by_ref[ref].name or ref
        return self.live[ref].name if ref in self.live else ref

    # ---- stages --------------------------------------------------------------------------

    def resolve_stages(self) -> None:
        """Each draft stage as it would be stored. For a stage that exists, an omitted
        colour, WIP flag or limit keeps what it has; a new stage takes its category's."""
        count = len(self.draft.stages)
        if not count:
            self.problems.error("no_stages", "A workflow needs at least one stage.")
        elif count > rules.MAX_STAGES:
            self.problems.error("too_many_stages",
                                f"A workflow can have at most {rules.MAX_STAGES} stages; this "
                                f"one has {count}.")
        reordered = _reordered([ref for ref in self.refs if ref in self.live], self.old_index)
        for position, stage in enumerate(self.draft.stages):
            current = self.live.get(stage.ref)
            given = stage.model_fields_set
            name = stage.name.strip()
            category = stage.category
            if stage.color is not None:
                color = stage.color
            elif current is not None and "color" not in given:
                color = current.color
            else:
                color = rules.DEFAULT_COLOR_BY_CATEGORY.get(category, "neutral")
            if stage.counts_toward_wip is not None:
                counts = stage.counts_toward_wip
            elif current is not None and "counts_toward_wip" not in given:
                counts = current.counts_toward_wip
            else:
                counts = category == "in_progress"
            if "wip_limit" in given:
                limit = stage.wip_limit
            else:
                limit = current.wip_limit if current is not None else None
            self._check_stage(stage.ref, position, name, category, color, limit,
                              known=current is not None)

            if current is None:
                change = "added"
            elif name != current.name:
                change = "renamed"
            elif ((category, color, counts, limit) != (current.category, current.color,
                                                       current.counts_toward_wip,
                                                       current.wip_limit)
                  or stage.ref in reordered):
                change = "updated"
            else:
                change = "unchanged"
            resolved = ResolvedStage(
                ref=stage.ref, id=current.id if current is not None else None, name=name,
                category=category, color=color, position=position, counts_toward_wip=counts,
                wip_limit=limit, change=change,
                previous_name=(current.name
                               if current is not None and name != current.name else None),
                previous_category=(current.category if current is not None
                                   and category != current.category else None),
                issue_count=current.issue_count if current is not None else 0)
            self.stages.append(resolved)
            self.by_ref[stage.ref] = resolved

    def _check_stage(self, ref: str, position: int, name: str, category: str, color: str,
                     limit: int | None, *, known: bool) -> None:
        label = name or f"Stage {position + 1}"
        if not known and not ref.startswith(rules.NEW_STAGE_PREFIX):
            self.problems.error("unknown_ref",
                                f"{label} names a stage that is not a live stage of this "
                                "space (a stage to create has a ref starting "
                                f"\"{rules.NEW_STAGE_PREFIX}\").", ref)
        if not name:
            self.problems.error("name_invalid", f"Stage {position + 1} needs a name.", ref)
        elif len(name) > rules.MAX_STAGE_NAME:
            self.problems.error("name_invalid",
                                f"The name of stage {position + 1} is {len(name)} characters "
                                f"long; a stage name is at most {rules.MAX_STAGE_NAME}.", ref)
        if category not in rules.STATUS_CATEGORIES:
            self.problems.error("category_invalid",
                                f"{label} needs a reporting category: todo (Not started), "
                                "in_progress (Active), done (Completed) or canceled.", ref)
        if color not in rules.STAGE_COLORS:
            self.problems.error("color_invalid",
                                f"{label} has a colour the board cannot draw. Choose one of: "
                                f"{', '.join(rules.STAGE_COLORS)}.", ref)
        if limit is not None and not 1 <= limit <= rules.MAX_WIP_LIMIT:
            self.problems.error("wip_limit_invalid",
                                f"{label}: a WIP limit is between 1 and {rules.MAX_WIP_LIMIT}, "
                                "or none at all.", ref)

    def check_names(self) -> None:
        groups: dict[str, list[ResolvedStage]] = {}
        for stage in self.stages:
            if stage.name:
                groups.setdefault(stage.name.casefold(), []).append(stage)
        for group in groups.values():
            if len(group) > 1:
                self.problems.error("duplicate_name",
                                    f"{len(group)} stages are called {group[0].name}. Stage "
                                    "names must differ, ignoring case.",
                                    *(stage.ref for stage in group))
        if not any(rules.is_completed(stage.category) for stage in self.stages):
            self.problems.error("no_completed_stage",
                                "No stage is in the Completed (done) category, so nothing in "
                                "this workflow could ever count as delivered.")

    # ---- the flow ------------------------------------------------------------------------

    def check_initial(self) -> None:
        initial = self.draft.initial
        if initial is None:
            self.problems.error("initial_missing", "Choose the stage new issues start in.")
        elif initial not in self.by_ref:
            if initial in self.removed_ids:
                message = (f"New issues cannot start in {self.live[initial].name}: it is "
                           "being removed.")
            else:
                message = "The initial stage is not one of this workflow's stages."
            self.problems.error("unknown_ref", message, initial)
        elif rules.is_closed(self.by_ref[initial].category):
            self.problems.error("initial_closed",
                                f"New issues cannot start in {self.name_of(initial)}: it is a "
                                f"{_LABEL[self.by_ref[initial].category]} stage. Choose an "
                                "open one.", initial)

    def check_transitions(self) -> None:
        """Every transition resolved; what survives is what would be stored, deduplicated."""
        seen: set[tuple[str | None, str]] = set()
        repeated: set[tuple[str | None, str]] = set()
        for transition in self.draft.transitions:
            source, target = transition.from_, transition.to
            if source is not None and source == target:
                self.problems.error("self_transition",
                                    f"{self.name_of(target)} -> {self.name_of(target)}: a "
                                    "stage cannot transition to itself; moving within a stage "
                                    "is always allowed.", target)
                continue
            unknown = [ref for ref in (source, target)
                       if ref is not None and ref not in self.by_ref]
            for ref in dict.fromkeys(unknown):
                if ref in self.removed_ids:
                    message = f"A transition uses {self.live[ref].name}, which is being removed."
                else:
                    message = "A transition names a stage that is not in this workflow."
                self.problems.error("unknown_ref", message, ref)
            if unknown:
                continue
            pair = (source, target)
            if pair in seen:
                if pair not in repeated:
                    repeated.add(pair)
                    self.problems.warn("duplicate_transition",
                                       f"{self.name_of(source)} -> {self.name_of(target)} is "
                                       "listed more than once; it is kept once.", source,
                                       target)
                continue
            seen.add(pair)
            self.pairs.append(pair)

    def check_reachability(self) -> None:
        """`restricted` only: column order alone imposes nothing in `any` mode (plan §4)."""
        if not self.restricted:
            return
        initial = self.draft.initial
        if initial is not None and initial in self.by_ref:
            reached = rules.reachable(initial, self.refs, self.pairs)
            for stage in self.stages:
                if stage.ref not in reached:
                    self.problems.error("unreachable_stage",
                                        f"{self.name_of(stage.ref)} cannot be reached from "
                                        f"{self.name_of(initial)}: no chain of transitions "
                                        "leads into it. Add a transition into it, or a global "
                                        "one.", stage.ref)
        categories = {stage.ref: stage.category for stage in self.stages}
        for ref in rules.dead_ends(self.refs, categories, self.pairs):
            self.problems.warn("dead_end_stage",
                               f"Nothing leaves {self.name_of(ref)}: an issue that reaches it "
                               "is stuck there. Add a transition out of it, or make it a "
                               "closed stage.", ref)

    # ---- removals and the merge action ----------------------------------------------------

    def check_removals(self) -> None:
        for removal in self.removals:
            stage, target = removal.stage, removal.replacement
            if target is None:
                if stage.issue_count:
                    self.problems.error("replacement_required",
                                        f"{stage.name} holds "
                                        f"{_plural(stage.issue_count, 'issue')}. Choose the "
                                        "stage its issues move to before removing it.",
                                        stage.id)
            elif target in self.removed_ids:
                self.problems.error("replacement_invalid",
                                    f"The issues of {stage.name} cannot move to "
                                    f"{self.live[target].name}: it is being removed too.",
                                    stage.id, target)
            elif target not in self.by_ref:
                self.problems.error("unknown_ref",
                                    f"The issues of {stage.name} are set to move to a stage "
                                    "that is not in this workflow.", stage.id, target)

    def check_merge_action(self) -> None:
        """Revalidated against the new stages: a target that stays and was not canceled, and
        never a fallback to some other completed stage (§6)."""
        target = self.merge_target
        if self.merge_mode != "status":
            return
        if target is None:
            self.merge_problem = ("Choose the stage a merged pull request moves its issues "
                                  "to, or turn the merge action off.")
            self.problems.error("merge_target_invalid", self.merge_problem)
        elif target in self.by_ref:
            if self.by_ref[target].category == "canceled":
                self.merge_problem = (f"A merge cannot move work to {self.name_of(target)}: "
                                      "it is a Canceled stage, and merged work was not "
                                      "canceled.")
                self.problems.error("merge_target_invalid", self.merge_problem, target)
        elif target in self.removed_ids:
            self.merge_problem = (f"Merges move work to {self.live[target].name}, which is "
                                  "being removed. Choose another stage, or turn the merge "
                                  "action off.")
            self.problems.error("merge_target_invalid", self.merge_problem, target)
        else:
            self.merge_problem = "The merge target is not one of this workflow's stages."
            self.problems.error("unknown_ref", self.merge_problem, target)
        if self.merge_problem is not None or target is None or not self.restricted:
            return
        # A merge is one move, so what matters is a direct transition, not a chain.
        stuck = [stage for stage in self.stages
                 if stage.ref != target and stage.category in rules.OPEN_CATEGORIES
                 and not rules.transition_allowed("restricted", self.pairs, stage.ref, target)]
        self.unreachable_from = [stage.name for stage in stuck]
        if stuck:
            self.problems.warn("merge_target_unreachable",
                               f"No transition leads from {', '.join(self.unreachable_from)} "
                               f"to {self.name_of(target)}: a merge of an issue there will "
                               "fail visibly and leave it where it is.",
                               *(stage.ref for stage in stuck))

    # ---- what publishing would do ---------------------------------------------------------

    def check_reporting(self) -> None:
        """A kept stage whose category changes re-labels its issues AND its history in every
        report (§4.2); when it holds issues, publishing needs the acknowledgement."""
        for stage in self.stages:
            current = self.live.get(stage.ref)
            if (current is None or stage.category == current.category
                    or stage.category not in rules.STATUS_CATEGORIES):
                continue
            self.category_changes.append({"id": current.id, "name": stage.name,
                                          "from_category": current.category,
                                          "to_category": stage.category,
                                          "issue_count": current.issue_count})
            self.requires_ack = self.requires_ack or current.issue_count > 0
            after = _LABEL[stage.category]
            held = (f"its {_plural(current.issue_count, 'issue')}, and everything that was "
                    "in it before," if current.issue_count else "everything that was in it")
            self.problems.warn("category_change",
                               f"{self.name_of(stage.ref)} changes from "
                               f"{_LABEL.get(current.category, current.category)} to {after}: "
                               f"reports re-label {held} as {after}.", stage.ref)

    def check_moves(self) -> None:
        for removal in self.removals:
            if removal.replacement in self.by_ref:
                issues, work_items = self.inflow.get(removal.replacement, (0, 0))
                self.inflow[removal.replacement] = (issues + removal.stage.issue_count,
                                                    work_items + removal.stage.wip_count)
        for ref, (issues, _work_items) in self.inflow.items():
            category = self.by_ref[ref].category
            if issues and rules.is_closed(category):
                outcome = ("they will count as delivered" if rules.is_completed(category)
                           else "they will be closed without counting as delivered")
                self.problems.warn("moves_into_closed",
                                   f"{_plural(issues, 'issue')} will move into "
                                   f"{self.name_of(ref)}, a {_LABEL[category]} stage: "
                                   f"{outcome}.", ref)
        in_sprint = sum(stage.sprint_count for stage in self.removed)
        if in_sprint:
            self.problems.warn("active_sprint_moves",
                               f"{_plural(in_sprint, 'issue')} that will move "
                               f"{'is' if in_sprint == 1 else 'are'} in the active sprint; its "
                               "board and burndown will show the stage change.",
                               *(stage.id for stage in self.removed if stage.sprint_count))

    def check_wip(self) -> None:
        for stage in self.stages:
            limit = stage.wip_limit
            if limit is None or not 1 <= limit <= rules.MAX_WIP_LIMIT:
                continue
            current = self.live.get(stage.ref)
            incoming = self.inflow.get(stage.ref, (0, 0))[1]
            held = (current.wip_count if current is not None else 0) + incoming
            limit_is_new = current is None or current.wip_limit != limit
            if held > limit and (limit_is_new or incoming):
                blocked = (" With the block policy nothing more can move into it until some "
                           "leave." if self.draft.wip_policy == "block" else "")
                self.problems.warn("wip_over_limit",
                                   f"{self.name_of(stage.ref)} will hold "
                                   f"{_plural(held, 'work item')}, over its WIP limit of "
                                   f"{limit}.{blocked}", stage.ref)

    # ---- the answer ------------------------------------------------------------------------

    def integration(self) -> dict[str, Any]:
        target = self.merge_target
        resolves = target is not None and target in self.by_ref
        if self.merge_mode == "none":
            message = ("A merged pull request only links to its issues; their stage does not "
                       "change.")
        elif self.merge_problem is not None:
            message = self.merge_problem
        elif target is None or not resolves:
            message = "The merge action has no stage to move work to."
        else:
            message = f"A merged pull request moves its issues to {self.name_of(target)}."
            if not rules.is_completed(self.by_ref[target].category):
                message += (" That is not a Completed stage, so merged work still needs "
                            "accepting before it counts as delivered.")
        return {"mode": self.merge_mode, "target_ref": target,
                "target_name": self.name_of(target) if resolves else None,
                "valid": self.merge_mode == "none" or (self.merge_problem is None and resolves),
                "message": message, "unreachable_from": self.unreachable_from}

    def preview(self) -> dict[str, Any]:
        total = sum(stage.issue_count for stage in self.removed)
        into_closed = sum(issues for ref, (issues, _work) in self.inflow.items()
                          if rules.is_closed(self.by_ref[ref].category))
        return {
            "valid": not self.problems.errors,
            "errors": self.problems.errors,
            "warnings": self.problems.warnings,
            "stages": [{"ref": stage.ref, "id": stage.id, "name": stage.name,
                        "category": stage.category, "color": stage.color,
                        "position": stage.position,
                        "counts_toward_wip": stage.counts_toward_wip,
                        "wip_limit": stage.wip_limit, "change": stage.change,
                        "previous_name": stage.previous_name,
                        "previous_category": stage.previous_category,
                        "issue_count": stage.issue_count}
                       for stage in self.stages],
            "removed": [{"id": removal.stage.id, "name": removal.stage.name,
                         "category": removal.stage.category,
                         "issue_count": removal.stage.issue_count,
                         "replacement_ref": removal.replacement,
                         "replacement_name": (self.name_of(removal.replacement)
                                              if removal.replacement in self.by_ref
                                              else None)}
                        for removal in self.removals],
            "category_changes": self.category_changes,
            "moves": {"total": total, "into_closed": into_closed,
                      "in_active_sprint": sum(stage.sprint_count for stage in self.removed)},
            "transitions": [{"from": source, "to": target,
                             "from_name": self.name_of(source) if source is not None else None,
                             "to_name": self.name_of(target)}
                            for source, target in self.pairs],
            "integration": self.integration(),
            "affected_issue_count": total,
            "requires_reporting_ack": self.requires_ack,
        }


# ---- reading a workflow ---------------------------------------------------------------------


def stage_counts(db: Session, workspace_id: str, space_id: str,
                 sprint_id: str | None = None) -> dict[str, tuple[int, int, int]]:
    """Per stage of the space: live issues, of which work items (story, task, bug), of which
    in `sprint_id` — one grouped query, whatever the number of issues."""
    work_item = case((Issue.type.in_(sorted(rules.WIP_TYPES)), 1), else_=0)
    columns = [Issue.status_id, func.count(Issue.id), func.sum(work_item)]
    if sprint_id is not None:
        # Only with a sprint: `Issue.sprint_id == None` would compile to IS NULL and count
        # the backlog as "in the active sprint".
        columns.append(func.sum(case((Issue.sprint_id == sprint_id, 1), else_=0)))
    rows = (db.query(*columns)
            .filter(Issue.workspace_id == workspace_id, Issue.space_id == space_id,
                    Issue.deleted == 0)
            .group_by(Issue.status_id)
            .all())
    # `int()`: MySQL's SUM comes back as a DECIMAL, SQLite's as an int.
    return {row[0]: (int(row[1] or 0), int(row[2] or 0),
                     int(row[3] or 0) if sprint_id is not None else 0)
            for row in rows}


def snapshot(db: Session, workspace_id: str, space: Space) -> WorkflowSnapshot:
    """The space's live stages and what they hold, for `evaluate`."""
    space_id = str(space.id)
    sprint = sprint_svc.active_sprint(db, workspace_id, space_id)
    counts = stage_counts(db, workspace_id, space_id, sprint.id if sprint is not None else None)
    stages = []
    for status in transitions.live_statuses(db, workspace_id, space_id):
        issues, work_items, in_sprint = counts.get(status.id, (0, 0, 0))
        stages.append(StageSnapshot(
            id=status.id, name=status.name, category=status.category,
            color=status.color or "neutral", position=status.position,
            counts_toward_wip=bool(status.counts_toward_wip), wip_limit=status.wip_limit,
            issue_count=issues, wip_count=work_items, sprint_count=in_sprint))
    return WorkflowSnapshot(stages=tuple(stages))


def editor_payloads(db: Session, workspace_id: str, space_id: str) -> list[dict[str, Any]]:
    """The members granted workflow-edit permission on the space, with their role now —
    grants whose holder has left the workspace or been deleted are not shown."""
    rows = (db.query(User, WorkspaceMember.role)
            .join(SpaceWorkflowEditor, SpaceWorkflowEditor.user_id == User.id)
            .join(WorkspaceMember,
                  and_(WorkspaceMember.user_id == User.id,
                       WorkspaceMember.workspace_id == SpaceWorkflowEditor.workspace_id))
            .filter(SpaceWorkflowEditor.workspace_id == workspace_id,
                    SpaceWorkflowEditor.space_id == space_id, User.deleted == 0)
            .order_by(User.first_name, User.last_name, User.id)
            .all())
    return [{"user": issue_svc.user_ref(user), "role": role} for user, role in rows]


def definition_payload(db: Session, access: WorkspaceAccess, space: Space) -> dict[str, Any]:
    """`WorkflowDefinition`: the workflow as the editor starts from it.

    The initial stage and the merge target are the *effective* ones, as services/transitions
    answers them for an issue create and a merge — so a space migrated without a choice shows
    the fallback it really uses, and a merge target that is no longer live shows as null.
    """
    workspace_id = access.workspace_id
    space_id = str(space.id)
    statuses = transitions.live_statuses(db, workspace_id, space_id)
    counts = stage_counts(db, workspace_id, space_id)
    position = {status.id: index for index, status in enumerate(statuses)}
    rows = [row for row in (db.query(WorkflowTransition)
                            .filter(WorkflowTransition.workspace_id == workspace_id,
                                    WorkflowTransition.space_id == space_id)
                            .all())
            if row.to_status_id in position
            and (row.from_status_id is None or row.from_status_id in position)]
    rows.sort(key=lambda row: (-1 if row.from_status_id is None
                               else position[row.from_status_id], position[row.to_status_id]))
    initial = transitions.initial_status(db, workspace_id, space)
    target = transitions.merge_target(db, workspace_id, space)
    return {
        "space_id": space.id,
        "version": int(space.workflow_version or 1),
        "transition_mode": space.transition_mode,
        "initial_status_id": initial.id if initial is not None else None,
        "wip_policy": space.wip_policy,
        "merge_action": {"mode": space.merge_action,
                         "target_status_id": target.id if target is not None else None},
        "stages": [{"id": status.id, "name": status.name, "category": status.category,
                    "color": status.color or "neutral", "position": status.position,
                    "counts_toward_wip": bool(status.counts_toward_wip),
                    "wip_limit": status.wip_limit,
                    "issue_count": counts.get(status.id, (0, 0, 0))[0],
                    "wip_count": counts.get(status.id, (0, 0, 0))[1]}
                   for status in statuses],
        "transitions": [{"id": row.id, "from_status_id": row.from_status_id,
                         "to_status_id": row.to_status_id} for row in rows],
        "can_edit": workflow_access.can_edit_workflow(db, access, space_id),
        "editors": editor_payloads(db, workspace_id, space_id),
    }


# ---- publishing -----------------------------------------------------------------------------


def lock_space(db: Session, workspace_id: str, space_id: str) -> Space | None:
    """The space row, locked to the end of the transaction and read fresh.

    `SELECT ... FOR UPDATE` serialises publishes of one space, as services/sprint.lock_space
    serialises sprint starts. `populate_existing` because the session may already hold this
    row: without it, a caller that read the space earlier in the same transaction would be
    handed that copy back, with the `workflow_version` it had before the lock was granted —
    the one value the stale-draft check must not trust. (The publish ends its first
    transaction before locking, which expires that copy too; this keeps the function right
    for a caller that does not.) SQLite ignores FOR UPDATE; it has one writer.
    """
    return (db.query(Space)
            .filter(Space.workspace_id == workspace_id, Space.id == space_id,
                    Space.deleted == 0)
            .with_for_update()
            .populate_existing()
            .first())


def issues_to_move(db: Session, workspace_id: str, space_id: str,
                   evaluation: Evaluation) -> dict[str, list[Issue]]:
    """The live issues of every stage being removed, by stage, in rank order — locked, so an
    issue that someone else is moving finishes its move before it is counted, and none
    changes stage between being counted and being moved."""
    stage_ids = [removal.stage.id for removal in evaluation.removals]
    if not stage_ids:
        return {}
    rows = (db.query(Issue)
            .filter(Issue.workspace_id == workspace_id, Issue.space_id == space_id,
                    Issue.status_id.in_(stage_ids), Issue.deleted == 0)
            .order_by(Issue.rank, Issue.id)
            .with_for_update()
            .populate_existing()
            .all())
    grouped: dict[str, list[Issue]] = {}
    for issue in rows:
        grouped.setdefault(str(issue.status_id), []).append(issue)
    return grouped


def publish(db: Session, space: Space, draft: WorkflowDraft, evaluation: Evaluation,
            moving: dict[str, list[Issue]], actor_id: str) -> dict[str, Any]:
    """Apply a valid evaluation: steps 4-7 of §4.4 except the audit line, which is the
    controller's. Flushes, never commits. Returns the impact summary it stored.

    The caller has checked what only it can: the lock, the base version, the confirmed count,
    the reporting acknowledgement, and that every stage in `moving` has a replacement.
    """
    workspace_id = str(space.workspace_id)
    space_id = str(space.id)
    now = datetime.now(UTC)
    rows = {status.id: status for status in transitions.live_statuses(db, workspace_id,
                                                                      space_id)}

    # 4. Stages: kept ones updated in place (their ids are what issues and history point
    # at), new ones inserted, removed ones soft-deleted so history still resolves them.
    ids: dict[str, str] = {}
    created: list[tuple[str, WorkflowStatus]] = []
    for stage in evaluation.stages:
        row = rows.get(stage.id) if stage.id is not None else None
        if row is None:
            row = WorkflowStatus(workspace_id=workspace_id, space_id=space_id, deleted=0)
            db.add(row)
            created.append((stage.ref, row))
        else:
            ids[stage.ref] = row.id
        row.name = stage.name
        row.category = stage.category
        row.color = stage.color
        row.position = stage.position
        row.counts_toward_wip = stage.counts_toward_wip
        row.wip_limit = stage.wip_limit
    for removal in evaluation.removals:
        rows[removal.stage.id].deleted = 1
        rows[removal.stage.id].deleted_at = now
    db.flush()
    for ref, row in created:
        ids[ref] = row.id
    by_id = {**rows, **{row.id: row for _ref, row in created}}

    # 5. Moves: through set_status, the one place a status changes, so each move bumps the
    # issue's version, restarts its stage age and records `status_changed` by the publisher.
    # Appended below the target column's last card in the order they had, removed stage by
    # removed stage in board order; the rank is re-read per target so a second removed stage
    # mapped to the same one lands below the first.
    moved = 0
    for removal in evaluation.removals:
        issues = moving.get(removal.stage.id, [])
        if not issues or removal.replacement is None:
            continue
        target = by_id[ids[removal.replacement]]
        rank = issue_svc.bottom_rank(db, workspace_id, space_id, target.id)
        for issue in issues:
            issue_svc.set_status(db, issue, target, rank, actor_id, "user")
            # After set_status, which may have rebalanced the column and with it this rank.
            rank = rank_after(issue.rank)
            moved += 1

    # 6. Transitions replaced. A bulk DELETE runs now, not at the next flush: the unit of work
    # inserts before it deletes, and re-inserting a kept pair beside its old row would break
    # `uq_workflow_transitions_pair`.
    (db.query(WorkflowTransition)
     .filter(WorkflowTransition.workspace_id == workspace_id,
             WorkflowTransition.space_id == space_id)
     .delete(synchronize_session=False))
    for source, target_ref in evaluation.transitions:
        db.add(WorkflowTransition(workspace_id=workspace_id, space_id=space_id,
                                  from_status_id=ids[source] if source is not None else None,
                                  to_status_id=ids[target_ref]))
    if evaluation.initial is not None:
        space.initial_status_id = ids[evaluation.initial]
    space.transition_mode = draft.transition_mode
    space.wip_policy = draft.wip_policy
    if evaluation.merge_mode == "status" and evaluation.merge_target is not None:
        space.merge_action = "status"
        space.merge_target_status_id = ids[evaluation.merge_target]
    else:
        space.merge_action = "none"
        space.merge_target_status_id = None
    space.workflow_version = int(space.workflow_version or 1) + 1
    db.flush()

    # 7. The revision: the definition with real ids, and what publishing it did.
    impact = {
        "base_version": draft.base_version,
        "moved_issues": moved,
        "moves": evaluation.preview["moves"],
        "added": [{"id": ids[stage.ref], "name": stage.name}
                  for stage in evaluation.stages if stage.id is None],
        "renamed": [{"id": stage.id, "from": stage.previous_name, "to": stage.name}
                    for stage in evaluation.stages if stage.previous_name is not None],
        "removed": [{"id": removal.stage.id, "name": removal.stage.name,
                     "issue_count": len(moving.get(removal.stage.id, [])),
                     "replacement_id": ids.get(removal.replacement or "")}
                    for removal in evaluation.removals],
        "category_changes": evaluation.preview["category_changes"],
        "warnings": [warning["code"] for warning in evaluation.preview["warnings"]],
    }
    definition = {
        "transition_mode": space.transition_mode,
        "wip_policy": space.wip_policy,
        "initial_status_id": space.initial_status_id,
        "merge_action": space.merge_action,
        "merge_target_status_id": space.merge_target_status_id,
        "stages": [{"id": ids[stage.ref], "name": stage.name, "category": stage.category,
                    "color": stage.color, "position": stage.position,
                    "counts_toward_wip": stage.counts_toward_wip,
                    "wip_limit": stage.wip_limit} for stage in evaluation.stages],
        "transitions": [{"from_status_id": ids[source] if source is not None else None,
                         "to_status_id": ids[target_ref]}
                        for source, target_ref in evaluation.transitions],
    }
    db.add(WorkflowRevision(workspace_id=workspace_id, space_id=space_id,
                            version=space.workflow_version, definition=definition,
                            impact=impact, published_by=actor_id))
    db.flush()
    return impact


# ---- templates (§4.5) -----------------------------------------------------------------------


def valid_definition(document: Any) -> dict[str, Any] | None:
    """`document` as a normalised template definition (defaults filled, duplicates dropped),
    or None when it is not one. Stored documents are validated on every read, like the
    settings documents: a row that no longer validates is left out, not a 500 for the
    whole listing."""
    try:
        return TemplateDefinition.model_validate(document).model_dump(by_alias=True)
    except ValidationError:
        return None


def builtin_key(ref: str) -> str | None:
    """The built-in template `ref` names (`default` or `builtin:default`), or None."""
    key = ref[len(BUILTIN_PREFIX):] if ref.startswith(BUILTIN_PREFIX) else ref
    return key if key in workflow_templates.BUILTIN_TEMPLATES else None


def get_template(db: Session, workspace_id: str, template_id: str) -> WorkflowTemplate | None:
    return (db.query(WorkflowTemplate)
            .filter(WorkflowTemplate.workspace_id == workspace_id,
                    WorkflowTemplate.id == template_id, WorkflowTemplate.deleted == 0)
            .first())


def resolve_template_definition(db: Session, workspace_id: str,
                                ref: str) -> dict[str, Any] | None:
    """The definition `ref` names, ready for `workflow_templates.materialize`, or None.

    `ref` is a built-in key (`default`, `delivery`, `support`), the same with the `builtin:`
    prefix the template listing uses as its id, or the id of a live template of this
    workspace — never another workspace's. The answer is validated, normalised and freshly
    built (`model_dump` makes new dicts), so a caller may change it without changing a
    built-in for every workspace in the process.
    """
    key = builtin_key(ref)
    if key is not None:
        return valid_definition(workflow_templates.builtin_definition(key))
    row = get_template(db, workspace_id, ref)
    return valid_definition(row.definition) if row is not None else None


def template_name_taken(db: Session, workspace_id: str, name: str) -> bool:
    """Whether a live template of the workspace has `name`, ignoring case. Compared here
    rather than in SQL because SQLite's lower() folds ASCII only; a workspace holds a handful
    of templates, so reading their names is cheap."""
    wanted = name.casefold()
    names = (db.query(WorkflowTemplate.name)
             .filter(WorkflowTemplate.workspace_id == workspace_id,
                     WorkflowTemplate.deleted == 0)
             .all())
    return any(str(existing).casefold() == wanted for (existing,) in names)


def template_payload(row: WorkflowTemplate, definition: dict[str, Any],
                     creator: User | None) -> dict[str, Any]:
    return {"id": row.id, "key": None, "name": row.name, "description": row.description,
            "builtin": False,
            "created_by": issue_svc.user_ref(creator) or {"id": row.created_by,
                                                          "display_name": ""},
            "created_at": row.created_at, "definition": definition}


def template_payloads(db: Session, workspace_id: str) -> list[dict[str, Any]]:
    """`WorkflowTemplateSummary` for the built-ins, then the workspace's own, by name."""
    payloads: list[dict[str, Any]] = []
    for key, template in workflow_templates.BUILTIN_TEMPLATES.items():
        payloads.append({"id": f"{BUILTIN_PREFIX}{key}", "key": key, "name": template["name"],
                         "description": template["description"], "builtin": True,
                         "created_by": None, "created_at": None,
                         "definition": TemplateDefinition.model_validate(
                             template["definition"]).model_dump(by_alias=True)})
    rows = sorted((db.query(WorkflowTemplate)
                   .filter(WorkflowTemplate.workspace_id == workspace_id,
                           WorkflowTemplate.deleted == 0)
                   .all()),
                  key=lambda row: (str(row.name).casefold(), str(row.id)))
    creators = issue_svc.users_by_id(db, (row.created_by for row in rows))
    for row in rows:
        definition = valid_definition(row.definition)
        if definition is None:
            logger.warning("workflow template no longer validates; left out of the listing",
                           extra={"extra_fields": {"template_id": row.id,
                                                   "workspace_id": workspace_id}})
            continue
        payloads.append(template_payload(row, definition, creators.get(row.created_by)))
    return payloads


def space_definition(db: Session, workspace_id: str, space: Space) -> dict[str, Any]:
    """The space's live workflow as a template definition, not yet validated."""
    space_id = str(space.id)
    return workflow_templates.definition_of_space(
        transitions.live_statuses(db, workspace_id, space_id), space,
        transitions.transition_pairs(db, workspace_id, space_id))


# ---- editors (§4.6) -------------------------------------------------------------------------


def members_by_id(db: Session, workspace_id: str,
                  user_ids: list[str]) -> dict[str, tuple[User, str]]:
    """The live accounts among `user_ids` that are members here, with their role."""
    if not user_ids:
        return {}
    rows = (db.query(User, WorkspaceMember.role)
            .join(WorkspaceMember, WorkspaceMember.user_id == User.id)
            .filter(WorkspaceMember.workspace_id == workspace_id,
                    User.id.in_(set(user_ids)), User.deleted == 0)
            .all())
    return {user.id: (user, role) for user, role in rows}


def replace_editors(db: Session, workspace_id: str, space_id: str, user_ids: list[str],
                    granted_by: str) -> tuple[list[str], list[str]]:
    """Make the space's grants exactly `user_ids`. Returns the user ids (added, removed).

    A grant that stays keeps its row, and with it who granted it and when: replacing the set
    is what the request means, rewriting every row's provenance is not.
    """
    existing = {row.user_id: row for row in (db.query(SpaceWorkflowEditor)
                                             .filter(SpaceWorkflowEditor.workspace_id
                                                     == workspace_id,
                                                     SpaceWorkflowEditor.space_id == space_id)
                                             .all())}
    wanted = list(dict.fromkeys(user_ids))
    removed = [user_id for user_id in existing if user_id not in wanted]
    added = [user_id for user_id in wanted if user_id not in existing]
    for user_id in removed:
        db.delete(existing[user_id])
    for user_id in added:
        db.add(SpaceWorkflowEditor(workspace_id=workspace_id, space_id=space_id,
                                   user_id=user_id, granted_by=granted_by))
    db.flush()
    return added, removed
