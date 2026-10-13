"""Payloads of the workflow routes (`routes/workflow.py`, docs/design/09-restructuring.md §4).

`WorkflowDefinition` is the workflow as it is; `WorkflowPreview` is what publishing a draft
would do to it — the plan's "preview before publishing": the proposed board and flow, the
affected issues, the reporting changes and the integration, each problem with a code the SPA
can place beside the field it is about.
"""
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from schemas.responses.issues import UserRef
from schemas.workflow import TemplateDefinition


class WorkflowStage(BaseModel):
    """One live stage, with what it holds now."""

    id: str = Field(description="Stable across renames and reorders; what issues point at.")
    name: str = Field(description="Display name.")
    category: str = Field(description="todo, in_progress, done or canceled. Only `done` counts "
                                      "as delivered.")
    color: str = Field(description="A theme tone, always drawn with the name.")
    position: int = Field(description="Left-to-right order on the board, ascending.")
    counts_toward_wip: bool = Field(description="Whether its story, task and bug issues count "
                                                "as work in progress.")
    wip_limit: int | None = Field(description="The most work items it should hold, or null.")
    issue_count: int = Field(description="Live issues in it now, of every type.")
    wip_count: int = Field(description="Of those, the story, task and bug issues: what a WIP "
                                       "limit is compared with (epics and subtasks are not "
                                       "work items of their own).")


class WorkflowTransitionSummary(BaseModel):
    """One allowed move of a `restricted` workflow."""

    id: str = Field(description="Opaque id.")
    from_status_id: str | None = Field(description="The stage it leaves; null for a global "
                                                   "transition, allowed from every stage.")
    to_status_id: str = Field(description="The stage it enters.")


class WorkflowMergeAction(BaseModel):
    """What a merged, linked pull request does."""

    mode: str = Field(description="`status`: move the issue to `target_status_id`. `none`: "
                                  "only link it.")
    target_status_id: str | None = Field(
        description="The live stage merges move work to; null with `none` — and null with "
                    "`status` only when the target is no longer a live stage, in which case "
                    "merges fail visibly until a workflow is published with a new one.")


class WorkflowEditor(BaseModel):
    """A member granted workflow-edit permission on the space."""

    user: UserRef = Field(description="Who.")
    role: str = Field(description="Their workspace role now. A grant to someone who has "
                                  "since become a guest has no effect: guests never edit.")


class WorkflowDefinition(BaseModel):
    """A space's workflow, as the editor starts from it."""

    space_id: str = Field(description="The space it belongs to.")
    version: int = Field(description="Send it back as `base_version` when publishing.")
    transition_mode: str = Field(description="`any` or `restricted`.")
    initial_status_id: str | None = Field(description="The stage new issues start in.")
    wip_policy: str = Field(description="`warn` or `block`: what a move over a WIP limit "
                                        "does.")
    merge_action: WorkflowMergeAction = Field(description="The GitHub merge action.")
    stages: list[WorkflowStage] = Field(description="Live stages, in board order.")
    transitions: list[WorkflowTransitionSummary] = Field(
        description="The stored transitions. Consulted only in `restricted` mode, and kept "
                    "in `any` mode so switching back restores them.")
    can_edit: bool = Field(description="Whether YOU may change this workflow (admins and "
                                       "owners always; members when granted).")
    editors: list[WorkflowEditor] = Field(
        description="Members granted workflow-edit permission on this space. Admins and "
                    "owners can always edit and are not listed.")


# ---- the preview ---------------------------------------------------------------------------


class WorkflowProblem(BaseModel):
    """An error (publishing is refused while any exists) or a warning."""

    code: str = Field(description="Stable, from the tables of 09-restructuring.md §4.4: "
                                  "e.g. `unreachable_stage`, `wip_over_limit`.")
    message: str = Field(description="One sentence for the person editing, naming the "
                                     "stages by name.")
    refs: list[str] = Field(description="The stage refs (draft refs or removed stage ids) "
                                        "the problem is about, for the editor to highlight.")


class WorkflowPreviewStage(BaseModel):
    """A stage of the draft, resolved."""

    ref: str = Field(description="As in the draft.")
    id: str | None = Field(description="The existing stage it keeps, or null for a new one.")
    name: str = Field(description="Trimmed, as it will be stored.")
    category: str = Field(description="As it will be stored.")
    color: str = Field(description="As it will be stored, defaults applied.")
    position: int = Field(description="Its board position after publishing, from 0.")
    counts_toward_wip: bool = Field(description="As it will be stored, defaults applied.")
    wip_limit: int | None = Field(description="As it will be stored.")
    change: str = Field(description="`added`, `renamed`, `updated` (category, colour, WIP "
                                    "settings, or its place in the order) or `unchanged`.")
    previous_name: str | None = Field(description="The name it had, when it is renamed.")
    previous_category: str | None = Field(description="The category it had, when that "
                                                      "changes.")
    issue_count: int = Field(description="Live issues in it now (0 for a new stage); those "
                                         "moving in are counted under `removed`.")


class WorkflowPreviewRemoval(BaseModel):
    """A live stage the draft leaves out: it is soft-deleted, its history kept."""

    id: str = Field(description="The stage.")
    name: str = Field(description="Its name now.")
    category: str = Field(description="Its category, which its history keeps.")
    issue_count: int = Field(description="Live issues that will move out of it.")
    replacement_ref: str | None = Field(description="Where they move, as a draft ref.")
    replacement_name: str | None = Field(description="That stage's name, when the ref "
                                                     "resolves.")


class WorkflowCategoryChange(BaseModel):
    """A kept stage whose reporting category changes. Its current issues AND its history are
    re-labelled in every report, because history stores stage ids (§4.2)."""

    id: str = Field(description="The stage.")
    name: str = Field(description="Its name in the draft.")
    from_category: str = Field(description="Before.")
    to_category: str = Field(description="After.")
    issue_count: int = Field(description="Live issues in it now.")


class WorkflowPreviewMoves(BaseModel):
    """The issues that move out of removed stages."""

    total: int = Field(description="How many.")
    into_closed: int = Field(description="Of those, how many land in a closed stage.")
    in_active_sprint: int = Field(description="Of those, how many are in the active sprint.")


class WorkflowPreviewTransition(BaseModel):
    """A transition as it will be stored, with names."""

    model_config = ConfigDict(validate_by_name=True)

    from_: str | None = Field(alias="from", description="Draft ref, or null for any stage.")
    to: str = Field(description="Draft ref.")
    from_name: str | None = Field(description="Its name; null for any stage.")
    to_name: str | None = Field(description="Its name.")


class WorkflowPreviewIntegration(BaseModel):
    """The GitHub merge action, revalidated against the draft (09-restructuring.md §6)."""

    mode: str = Field(description="`status` or `none`.")
    target_ref: str | None = Field(description="The target, as a draft ref.")
    target_name: str | None = Field(description="Its name, when it resolves.")
    valid: bool = Field(description="False when the target would be refused.")
    message: str = Field(description="What a merge will do, in a sentence.")
    unreachable_from: list[str] = Field(
        description="Names of the open stages from which no transition leads to the target "
                    "(`restricted` mode): a merge of an issue there fails visibly instead of "
                    "moving it — never a fallback to another stage.")


class WorkflowPreview(BaseModel):
    """Everything publishing the draft would do, and everything wrong with it."""

    valid: bool = Field(description="No errors: the draft can be published.")
    errors: list[WorkflowProblem] = Field(description="Publishing is refused while any exists.")
    warnings: list[WorkflowProblem] = Field(description="Worth reading; nothing is refused.")
    stages: list[WorkflowPreviewStage] = Field(description="The draft's stages, in board "
                                                           "order, resolved.")
    removed: list[WorkflowPreviewRemoval] = Field(description="Stages being removed.")
    category_changes: list[WorkflowCategoryChange] = Field(
        description="Kept stages whose category changes.")
    moves: WorkflowPreviewMoves = Field(description="Issues that move.")
    transitions: list[WorkflowPreviewTransition] = Field(
        description="The transitions that would be stored: duplicates dropped.")
    integration: WorkflowPreviewIntegration = Field(description="The merge action.")
    affected_issue_count: int = Field(
        description="How many issues will move. Publishing must confirm this number "
                    "(`confirmed_affected_issues`).")
    requires_reporting_ack: bool = Field(
        description="A stage holding issues changes category: publishing needs "
                    "`acknowledge_reporting_changes: true`.")


class WorkflowPublishResult(BaseModel):
    """What publishing did."""

    workflow: WorkflowDefinition = Field(description="The workflow, as it is now.")
    moved_issues: int = Field(description="How many issues moved out of removed stages, each "
                                          "with a `status_changed` event by you.")
    version: int = Field(description="The new workflow version.")


# ---- templates -----------------------------------------------------------------------------


class WorkflowTemplateSummary(BaseModel):
    """A built-in template or one of this workspace's own."""

    id: str = Field(description="`builtin:<key>` for a built-in; an opaque id otherwise.")
    key: str | None = Field(description="The built-in's key (`default`, `delivery`, "
                                        "`support`), or null.")
    name: str = Field(description="Display name.")
    description: str | None = Field(description="What it is for.")
    builtin: bool = Field(description="Built-ins belong to every workspace and cannot be "
                                      "deleted.")
    created_by: UserRef | None = Field(description="Who saved it; null for a built-in.")
    created_at: datetime | None = Field(description="When, UTC; null for a built-in.")
    definition: TemplateDefinition = Field(description="Stages by position; initial, "
                                                       "transitions and merge target as "
                                                       "indexes.")


class WorkflowTemplateDeleted(BaseModel):
    """A template was deleted. Spaces created from it are unaffected: a template is copied
    into a space, never linked to it."""

    id: str = Field(description="The template.")
