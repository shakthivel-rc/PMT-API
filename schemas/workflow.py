"""Request bodies of the workflow routes (`routes/workflow.py`), and the template definition.

docs/design/09-restructuring.md §4.4-§4.6. Two kinds of model live here, validated in
opposite spirits on purpose.

**A draft (`WorkflowDraft`) is lenient about everything the preview reports.** The preview's
error codes (§4.4) are the product: "Stage 3 needs a name", carrying the stage's ref, so the
editor can put the message beside the field it is about. If this schema refused a blank name
or a WIP limit of 0, the SPA would get pydantic's error list instead —
`stages.2.name: String should have at least 1 character` — and the preview could never say
it. So a draft is refused here only when it is not a workflow at all: the wrong shape, an
unknown transition mode or WIP policy, two stages claiming one ref, or sizes far past
anything a workflow could be (the caps below, each with its reason).

**A template definition (`TemplateDefinition`) is strict**, because it is stored and later
applied without anyone looking at it: `services/workflow_templates.materialize` builds a new
space's stages from it and trusts it. Whatever a publish would refuse, a template refuses at
the door, so a template can never produce a workflow its space could not have published.

The vocabulary is `utils/workflow_rules.py`. The `Literal` types below spell its tuples out
because a type checker cannot read a tuple, and tests/unit/test_workflow_validation.py fails
the day the two disagree.
"""
from typing import Annotated, Any, Literal, cast

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)

from utils import workflow_rules as rules

StageCategory = Literal["todo", "in_progress", "done", "canceled"]
StageColor = Literal["neutral", "info", "primary", "success", "warning", "danger"]
TransitionMode = Literal["any", "restricted"]
WipPolicy = Literal["warn", "block"]
MergeMode = Literal["status", "none"]

#: A stage reference: an existing stage id (a UUID, 36 characters) or `new:<anything>`. 64
#: leaves the `<anything>` room for whatever client-side id an editor mints (a UUID fits,
#: with its prefix) and still refuses a value that is plainly not a reference.
StageRef = Annotated[str, StringConstraints(min_length=1, max_length=64)]

#: Five times the 20-stage limit (utils/workflow_rules.MAX_STAGES): room for the preview to
#: report `too_many_stages` on a draft somebody over-filled, while a body that is not a
#: workflow at all is refused before it is read.
DRAFT_MAX_STAGES = 5 * rules.MAX_STAGES
#: Ten times the 50-character stage name, for the same reason: a pasted sentence comes back
#: as `name_invalid` with the stage's ref, a pasted file does not get that far.
DRAFT_MAX_NAME = 10 * rules.MAX_STAGE_NAME
#: Every ordered pair of 20 stages plus a global transition into each is 20 x 19 + 20 = 400
#: distinct transitions; a draft may repeat some (the preview drops and reports them as
#: `duplicate_transition`), and 1,000 is generous for that while still bounding the body.
DRAFT_MAX_TRANSITIONS = 1000
#: A template stores distinct transitions only, so its ceiling is the 400 above.
TEMPLATE_MAX_TRANSITIONS = rules.MAX_STAGES * rules.MAX_STAGES
#: A space is edited by its lead and perhaps a deputy or two. Fifty is far past any team that
#: shares one workflow, and keeps the PUT one bounded membership query.
MAX_WORKFLOW_EDITORS = 50

_STAGE_EXAMPLE: dict[str, Any] = {"ref": "new:qa", "name": "QA", "category": "in_progress",
                                  "color": "warning", "counts_toward_wip": True,
                                  "wip_limit": 2}


class WorkflowDraftStage(BaseModel):
    """One stage of a draft. Its place in `WorkflowDraft.stages` is its place on the board.

    The three optional settings follow PATCH rules for a stage that already exists: omitted
    keeps what it has, so an editor that only renames does not have to know its WIP limit.
    """

    model_config = ConfigDict(extra="forbid", json_schema_extra={"examples": [_STAGE_EXAMPLE]})

    ref: StageRef = Field(
        description="An existing live stage id of the space (the stage keeps its id, so its "
                    "issues and history stay attached whatever else changes), or "
                    "`new:<anything>` for a stage to create.")
    name: str = Field(
        max_length=DRAFT_MAX_NAME,
        description="1-50 characters once trimmed, unique in the workflow ignoring case. A "
                    "bad one is reported by the preview as `name_invalid` or "
                    "`duplicate_name`, not refused here.")
    # 40, here and for `color`: twice the column (`String(20)`), so a near miss such as
    # `in-progress ` still reaches the preview and comes back as `category_invalid` beside
    # its stage, while a value that is no kind of category word is refused here.
    category: str = Field(
        max_length=40,
        description="The reporting category: `todo`, `in_progress`, `done` (delivered) or "
                    "`canceled` (closed, not delivered). Reports and automations reason "
                    "about this, never about the name. Anything else is `category_invalid`.")
    color: str | None = Field(
        default=None, max_length=40,
        description="A theme tone: neutral, info, primary, success, warning or danger. Omit to "
                    "keep an existing stage's colour (a new stage takes its category's); null "
                    "resets it to the category's.")
    counts_toward_wip: bool | None = Field(
        default=None,
        description="Whether story, task and bug issues here count as work in progress. Omit "
                    "to keep an existing stage's setting; a new stage counts exactly when it "
                    "is `in_progress`.")
    wip_limit: int | None = Field(
        default=None,
        description="How many work items the stage should hold, 1-999, or null for no limit. "
                    "Omit to keep an existing stage's limit.")


class WorkflowDraftTransition(BaseModel):
    """One allowed move. Only consulted in `restricted` mode, but kept in `any` mode too, so
    switching back restores the flow that was designed."""

    model_config = ConfigDict(extra="forbid", validate_by_name=True,
                              json_schema_extra={"examples": [{"from": None, "to": "new:qa"}]})

    from_: StageRef | None = Field(
        default=None, alias="from",
        description="The stage it leaves, as a ref; null for a global transition, allowed "
                    "from every stage (e.g. into Canceled).")
    to: StageRef = Field(description="The stage it enters, as a ref.")


class WorkflowDraftMergeAction(BaseModel):
    """What a merged, linked pull request does to its issues (09-restructuring.md §6)."""

    model_config = ConfigDict(extra="forbid",
                              json_schema_extra={"examples": [{"mode": "status",
                                                               "target": "new:qa"}]})

    mode: MergeMode = Field(
        description="`status`: move the issue to `target`. `none`: only link the pull request. "
                    "There is no \"first done stage\" mode: choosing a stage by position is "
                    "the inference the design rules out.")
    target: StageRef | None = Field(
        default=None,
        description="With `status`, the ref of a stage that stays, and is not `canceled` — "
                    "merged work was not canceled. Ignored with `none`.")


class WorkflowDraft(BaseModel):
    """A whole proposed workflow, as the editor holds it. Nothing is stored until it is
    published, so cancelling an edit is discarding the draft.

    A live stage of the space that is missing from `stages` is being **removed**; if it still
    holds issues, `replacements` must say where they go.
    """

    model_config = ConfigDict(extra="forbid", json_schema_extra={"examples": [{
        "base_version": 3,
        "transition_mode": "restricted",
        "wip_policy": "warn",
        "stages": [
            {"ref": "<backlog stage id>", "name": "Backlog", "category": "todo",
             "color": "neutral", "counts_toward_wip": False, "wip_limit": None},
            _STAGE_EXAMPLE,
            {"ref": "<done stage id>", "name": "Done", "category": "done"},
        ],
        "initial": "<backlog stage id>",
        "transitions": [{"from": "<backlog stage id>", "to": "new:qa"},
                        {"from": "new:qa", "to": "<done stage id>"},
                        {"from": None, "to": "new:qa"}],
        "replacements": {"<removed stage id>": "new:qa"},
        "merge_action": {"mode": "status", "target": "new:qa"},
    }]})

    base_version: int = Field(
        ge=1,
        description="The workflow `version` the draft was made from. Publishing refuses it "
                    "with 409 when someone has published since.")
    transition_mode: TransitionMode = Field(
        description="`any`: every stage may move to every other, whatever the column order. "
                    "`restricted`: only `transitions` are allowed.")
    wip_policy: WipPolicy = Field(
        description="`warn`: a move over a stage's WIP limit succeeds and says so. `block`: "
                    "it is refused.")
    stages: list[WorkflowDraftStage] = Field(
        default_factory=list, max_length=DRAFT_MAX_STAGES,
        description="Every stage the workflow will have, in board order (left to right). "
                    "At most 20; the preview reports more as `too_many_stages`.")
    initial: StageRef | None = Field(
        default=None,
        description="The ref of the stage new issues start in. It must be an open stage.")
    transitions: list[WorkflowDraftTransition] = Field(
        default_factory=list, max_length=DRAFT_MAX_TRANSITIONS,
        description="The allowed moves. Duplicates are dropped (and reported).")
    replacements: dict[StageRef, StageRef] = Field(
        default_factory=dict, max_length=DRAFT_MAX_STAGES,
        description="For each removed stage that still holds issues: its id, mapped to the "
                    "ref of the stage its issues move to. Entries for stages that are not "
                    "being removed are ignored.")
    merge_action: WorkflowDraftMergeAction = Field(
        description="What a merged pull request does, revalidated against the new stages.")

    @model_validator(mode="after")
    def _one_stage_per_ref(self) -> "WorkflowDraft":
        """Two stages claiming one ref would be two columns with one identity: there is no
        answer to "which of them keeps the issues", so the draft is not a workflow."""
        seen: set[str] = set()
        for stage in self.stages:
            if stage.ref in seen:
                raise ValueError(f"two stages have the ref {stage.ref!r}; every stage needs "
                                 "its own")
            seen.add(stage.ref)
        return self


class WorkflowPublish(WorkflowDraft):
    """The draft the preview was run on, plus what the person saw and agreed to."""

    model_config = ConfigDict(extra="forbid", json_schema_extra={"examples": [{
        "base_version": 3, "transition_mode": "any", "wip_policy": "warn",
        "stages": [{"ref": "<to do stage id>", "name": "To Do", "category": "todo"},
                   {"ref": "<done stage id>", "name": "Done", "category": "done"}],
        "initial": "<to do stage id>", "transitions": [],
        "replacements": {"<in review stage id>": "<done stage id>"},
        "merge_action": {"mode": "status", "target": "<done stage id>"},
        "confirmed_affected_issues": 7, "acknowledge_reporting_changes": False,
    }]})

    confirmed_affected_issues: int = Field(
        ge=0,
        description="The `affected_issue_count` the preview showed. When the number of "
                    "issues that would move is different now, publishing is refused with 409 "
                    "rather than moving issues nobody reviewed.")
    acknowledge_reporting_changes: bool = Field(
        default=False,
        description="Required (true) when a stage that holds issues changes category: its "
                    "issues, and their history, are re-labelled in every report.")


# ---- templates (§4.5) ----------------------------------------------------------------------


class WorkflowTemplateStage(BaseModel):
    """One stage of a template, by position."""

    model_config = ConfigDict(extra="forbid", json_schema_extra={"examples": [{
        "name": "QA", "category": "in_progress", "color": "warning",
        "counts_toward_wip": True, "wip_limit": None}]})

    name: str = Field(min_length=1, max_length=rules.MAX_STAGE_NAME,
                      description="1-50 characters, unique in the template ignoring case.")
    category: StageCategory = Field(description="todo, in_progress, done or canceled.")
    color: StageColor | None = Field(
        default=None, description="A theme tone; omitted or null takes the category's.")
    counts_toward_wip: bool | None = Field(
        default=None, description="Omitted or null: true exactly for `in_progress`.")
    wip_limit: int | None = Field(default=None, ge=1, le=rules.MAX_WIP_LIMIT,
                                  description="1-999, or null for none.")

    @field_validator("name")
    @classmethod
    def _trimmed(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("a stage needs a name")
        return value.strip()

    @model_validator(mode="after")
    def _defaults(self) -> "WorkflowTemplateStage":
        """Filled in here rather than left null, so a stored definition says what it means
        and `materialize` and a draft built from it agree without each defaulting."""
        if self.color is None:
            self.color = cast(StageColor, rules.DEFAULT_COLOR_BY_CATEGORY[self.category])
        if self.counts_toward_wip is None:
            self.counts_toward_wip = self.category == "in_progress"
        return self


class WorkflowTemplateTransition(BaseModel):
    """An allowed move between two template stages, by index."""

    model_config = ConfigDict(extra="forbid", validate_by_name=True,
                              json_schema_extra={"examples": [{"from": 0, "to": 1}]})

    from_: int | None = Field(default=None, alias="from", ge=0,
                              description="Index of the stage it leaves; null for any stage.")
    to: int = Field(ge=0, description="Index of the stage it enters.")


class TemplateDefinition(BaseModel):
    """A workflow without ids: stages by position, and the initial stage, the transitions
    and the merge target as indexes into them — what `workflow_templates.definition` stores
    and `services/workflow_templates.materialize` builds a space from."""

    model_config = ConfigDict(extra="forbid", json_schema_extra={"examples": [{
        "transition_mode": "any", "wip_policy": "warn",
        "stages": [{"name": "To Do", "category": "todo"},
                   {"name": "Doing", "category": "in_progress"},
                   {"name": "Done", "category": "done"}],
        "initial": 0, "transitions": [], "merge_target": 2}]})

    transition_mode: TransitionMode = Field(default="any",
                                            description="`any` or `restricted`.")
    wip_policy: WipPolicy = Field(default="warn", description="`warn` or `block`.")
    stages: list[WorkflowTemplateStage] = Field(
        min_length=1, max_length=rules.MAX_STAGES,
        description="1-20 stages in board order, at least one of them `done`.")
    initial: int = Field(default=0, ge=0,
                         description="Index of the stage new issues start in; not closed.")
    transitions: list[WorkflowTemplateTransition] = Field(
        default_factory=list, max_length=TEMPLATE_MAX_TRANSITIONS,
        description="Allowed moves, by index. Duplicates are dropped.")
    merge_target: int | None = Field(
        default=None, ge=0,
        description="Index of the stage a merged pull request moves its issues to, or null "
                    "for a merge that only links. Never a `canceled` stage.")

    @model_validator(mode="after")
    def _a_workflow_a_space_could_publish(self) -> "TemplateDefinition":
        """The publish rules of §4.4, for a definition with no space behind it yet."""
        count = len(self.stages)

        def stage_at(index: int, what: str) -> WorkflowTemplateStage:
            if index >= count:
                raise ValueError(f"{what} is stage {index}, but the stages are numbered "
                                 f"0-{count - 1}")
            return self.stages[index]

        names = [stage.name.casefold() for stage in self.stages]
        if len(set(names)) != len(names):
            raise ValueError("two stages have the same name (ignoring case)")
        if rules.is_closed(stage_at(self.initial, "the initial stage").category):
            raise ValueError("the initial stage must be an open stage: new issues cannot "
                             "start closed")
        if not any(rules.is_completed(stage.category) for stage in self.stages):
            raise ValueError("at least one stage must be in the done category, or nothing "
                             "could ever be delivered")
        distinct: list[WorkflowTemplateTransition] = []
        pairs: set[tuple[int | None, int]] = set()
        for transition in self.transitions:
            stage_at(transition.to, "a transition's target")
            if transition.from_ is not None:
                stage_at(transition.from_, "a transition's source")
            if transition.from_ == transition.to:
                raise ValueError("a stage cannot transition to itself; moving within a stage "
                                 "is always allowed")
            if (transition.from_, transition.to) not in pairs:
                pairs.add((transition.from_, transition.to))
                distinct.append(transition)
        self.transitions = distinct
        if self.merge_target is not None and stage_at(
                self.merge_target, "the merge target").category == "canceled":
            raise ValueError("a merge cannot move work to a canceled stage")
        if self.transition_mode == "restricted":
            refs = [str(index) for index in range(count)]
            flow = [(None if source is None else str(source), str(to)) for source, to in pairs]
            reached = rules.reachable(str(self.initial), refs, flow)
            stuck = [self.stages[index].name for index in range(count)
                     if str(index) not in reached]
            if stuck:
                raise ValueError("these stages cannot be reached from the initial stage: "
                                 + ", ".join(stuck))
        return self


class WorkflowTemplateCreate(BaseModel):
    """Save a workflow as a reusable template of this workspace: a space's current workflow
    (`from_space_id`) or a definition written out (`definition`) — exactly one of them."""

    model_config = ConfigDict(extra="forbid", json_schema_extra={"examples": [
        {"name": "Payments flow", "description": "Review and QA before release.",
         "from_space_id": "<space id>"}]})

    # `workflow_templates.name` is VARCHAR(100).
    name: str = Field(min_length=1, max_length=100,
                      description="Unique among this workspace's templates, ignoring case.")
    # The charter fields' 2,000 (09-restructuring.md §3.2): a description is a paragraph.
    description: str | None = Field(default=None, max_length=2000,
                                    description="What the template is for. Plain text.")
    # `spaces.id` is CHAR(36).
    from_space_id: str | None = Field(
        default=None, max_length=36,
        description="Copy this space's current workflow. Needs workflow-edit permission on "
                    "it (admins have it everywhere).")
    definition: TemplateDefinition | None = Field(
        default=None, description="A definition written out. Admins only.")

    @field_validator("name")
    @classmethod
    def _named(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("a template needs a name")
        return value.strip()

    @model_validator(mode="after")
    def _one_source(self) -> "WorkflowTemplateCreate":
        if (self.from_space_id is None) == (self.definition is None):
            raise ValueError("give exactly one of from_space_id or definition")
        return self


# ---- editors (§4.6) ------------------------------------------------------------------------


class WorkflowEditorsUpdate(BaseModel):
    """The complete set of members granted workflow-edit permission on one space. It
    replaces the set: an id left out loses the grant."""

    model_config = ConfigDict(extra="forbid",
                              json_schema_extra={"examples": [{"user_ids": ["<user id>"]}]})

    # `users.id` is CHAR(36).
    user_ids: list[Annotated[str, StringConstraints(min_length=1, max_length=36)]] = Field(
        max_length=MAX_WORKFLOW_EDITORS,
        description="Members whose role is member or higher; at most 50. Admins and owners "
                    "can always edit and need no grant. An empty list removes every grant.")
