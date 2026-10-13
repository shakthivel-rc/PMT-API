"""Payloads of the tenancy and hierarchy routes (`routes/workspaces.py`).

Since 2026-10-05 (docs/design/09-restructuring.md §3) every container says whether it can be
written to — `archived` on spaces, folders and lists, `frozen` on spaces — and a space carries
its owner, delivery method and a summary of its workflow, so one read of the tree is enough
for the sidebar to draw an "Archived" section, a frozen badge and the board's drag rules.
"""
from datetime import datetime

from pydantic import BaseModel, Field

from schemas.workspaces import SpaceCharter


class WorkspaceSummary(BaseModel):
    """A workspace as the switcher needs it, with the caller's role in it."""

    id: str = Field(description="Opaque id; the first path segment of every workspace route.")
    name: str = Field(description="Display name.")
    slug: str = Field(description="URL-safe, globally unique.")
    role: str = Field(description="The caller's role here: guest, member, admin or owner.")


class StatusSummary(BaseModel):
    """One workflow column."""

    id: str = Field(description="Opaque id; what `IssueMove.status_id` names. Stable across "
                                "renames and reorders.")
    name: str = Field(description="Display name, e.g. `In Review`.")
    category: str = Field(description="`todo`, `in_progress`, `done` or `canceled` — what "
                                      "reports and automations reason about, whatever the "
                                      "name. Only `done` counts as delivered.")
    position: int = Field(description="Left-to-right order on the board, ascending.")
    color: str = Field(default="neutral",
                       description="A theme tone: neutral, info, primary, success, warning or "
                                   "danger. Always drawn with the name, never alone.")
    counts_toward_wip: bool = Field(default=False,
                                    description="Whether story, task and bug issues here count "
                                                "as work in progress.")
    wip_limit: int | None = Field(default=None,
                                  description="The most WIP issues the stage should hold, or "
                                              "null for no limit.")


class PersonRef(BaseModel):
    """A person: the same two fields as `UserRef` on issue cards.

    A model of its own rather than `schemas.responses.issues.UserRef` because that module
    imports `StatusSummary` from this one, and the reverse import would be circular.
    """

    id: str = Field(description="The account's id.")
    display_name: str = Field(description="`First Last`, for avatars and bylines.")


class ListNode(BaseModel):
    """A list, as a leaf of the sidebar tree."""

    id: str = Field(description="Opaque id; the `list_id` of the board and issue routes.")
    name: str = Field(description="Display name.")
    folder_id: str | None = Field(description="Its folder, or null when it sits on the space.")
    position: int = Field(description="Order among its siblings, ascending; ties by name.")
    archived: bool = Field(description="Whether this list is archived — its own flag. A list "
                                       "in an archived folder or space is read-only too.")


class FolderNode(BaseModel):
    """A folder and the lists in it."""

    id: str = Field(description="Opaque id.")
    name: str = Field(description="Display name.")
    position: int = Field(description="Order among the space's folders, ascending; ties by "
                                      "name.")
    archived: bool = Field(description="Whether this folder is archived — its own flag. Its "
                                       "lists are read-only while it is.")
    lists: list[ListNode] = Field(description="Its lists, in position order, archived ones "
                                              "included and flagged.")


class SpaceMergeAction(BaseModel):
    """What a merged, linked pull request does to an issue of the space (§6)."""

    mode: str = Field(description="`status`: move it to `target_status_id`; `none`: only link "
                                  "the pull request.")
    target_status_id: str | None = Field(description="The stage a merge moves issues to, in "
                                                      "`status` mode. Null in `none` mode, "
                                                      "and when the target is no longer a "
                                                      "live stage — merges then fail visibly "
                                                      "rather than pick another.")


class SpaceTransition(BaseModel):
    """One allowed move of a `restricted` workflow."""

    from_status_id: str | None = Field(description="Where the move starts; null for a global "
                                                    "transition, allowed from every stage.")
    to_status_id: str = Field(description="Where it ends.")


class SpaceWorkflowSummary(BaseModel):
    """The rules of the space's workflow, as the board needs them to check a drag before it
    is sent (09-restructuring.md §4.3). Editing them is the workflow routes' job."""

    version: int = Field(description="Bumped by every publish; what a publish names as "
                                     "`base_version`.")
    transition_mode: str = Field(description="`any`: every stage reaches every other; "
                                             "`restricted`: only `transitions` are allowed.")
    initial_status_id: str | None = Field(description="Where new issues start. In "
                                                      "`restricted` mode, the only stage a "
                                                      "new issue can be created in.")
    wip_policy: str = Field(description="`warn`: a move over a stage's WIP limit succeeds and "
                                        "says so; `block`: it is refused.")
    merge_action: SpaceMergeAction = Field(description="What a merged pull request does.")
    transitions: list[SpaceTransition] = Field(
        description="The stored transitions between live stages, in board order of their "
                    "ends. Kept, and ignored, while the mode is `any`.")


class SpaceNode(BaseModel):
    """A space with its workflow, its write state and everything under it."""

    id: str = Field(description="Opaque id.")
    name: str = Field(description="Display name.")
    key: str = Field(description="Issue-key prefix: `PAY` in `PAY-142`.")
    description: str | None = Field(description="What the space is for, as prose.")
    owner: PersonRef | None = Field(description="The accountable owner, or null when none is "
                                                "set.")
    delivery_method: str | None = Field(description="`scrum`, `kanban`, or null when not "
                                                    "chosen.")
    has_charter: bool = Field(description="Whether any field of the creation checklist is "
                                          "filled in. The checklist itself is on the space "
                                          "detail.")
    archived: bool = Field(description="Read-only for good, history kept: nothing in it can "
                                       "be changed until an admin unarchives it.")
    archived_at: datetime | None = Field(description="When it was archived, UTC.")
    frozen: bool = Field(description="Read-only for now, while it is being reorganised.")
    freeze_reason: str | None = Field(description="Why it is frozen, while it is.")
    statuses: list[StatusSummary] = Field(description="The workflow's live stages, in board "
                                                      "order.")
    workflow: SpaceWorkflowSummary = Field(description="The workflow's rules.")
    can_edit_workflow: bool = Field(description="Whether the caller may change the workflow: "
                                                "admins and owners everywhere, members the "
                                                "space's admins granted it to. Permission "
                                                "only — an archived or frozen space refuses "
                                                "every publish whatever this says.")
    folders: list[FolderNode] = Field(description="Folders, in position order.")
    lists: list[ListNode] = Field(description="Lists that sit directly on the space.")


class SpaceDetail(SpaceNode):
    """A space with its creation checklist and the keys it used to have."""

    charter: SpaceCharter | None = Field(
        description="The creation checklist, every field present (null when blank), or null "
                    "when none was written — or when the stored document no longer "
                    "validates.")
    key_aliases: list[str] = Field(description="The space's earlier keys, oldest first. "
                                               "Issue keys with them still resolve here, and "
                                               "no other space can take them.")
    created_at: datetime = Field(description="UTC.")
    frozen_at: datetime | None = Field(description="When it was frozen, UTC, while it is.")
    frozen_by_plan_id: str | None = Field(
        description="The migration plan that froze it, or null for a freeze an admin set. "
                    "While that plan is running or failed, the space cannot be unfrozen by "
                    "hand.")


class DeferredReplaySummary(BaseModel):
    """What happened to the workspace's deferred GitHub deliveries when a space was
    unfrozen (09-restructuring.md §6.3)."""

    replayed: int = Field(description="Applied now, oldest first.")
    still_deferred: int = Field(description="Still waiting: they name issues in a space that "
                                            "is still frozen.")
    failed: int = Field(description="Raised when applied; kept for an admin to look at.")


class SpaceUnfreezeResult(BaseModel):
    """The unfrozen space, and the deliveries its unfreezing replayed."""

    space: SpaceNode = Field(description="The space, writable again.")
    replay: DeferredReplaySummary | None = Field(
        description="The replay of deferred GitHub deliveries. Null only when the replay "
                    "itself could not run; the space is unfrozen either way, and the "
                    "deliveries stay deferred.")


class WorkspaceTree(BaseModel):
    """A workspace and its whole hierarchy — one read draws the sidebar."""

    id: str = Field(description="Opaque id.")
    name: str = Field(description="Display name.")
    slug: str = Field(description="URL-safe, globally unique.")
    role: str = Field(description="The caller's role here.")
    spaces: list[SpaceNode] = Field(description="Spaces, alphabetical, archived ones included "
                                                "and flagged.")


class MemberSummary(BaseModel):
    """A member of a workspace."""

    user_id: str = Field(description="The account's id; what `assignee_id` takes.")
    email: str = Field(description="Sign-in address.")
    first_name: str = Field(description="Given name.")
    last_name: str = Field(description="Family name.")
    role: str = Field(description="guest, member, admin or owner.")


class RepositorySummary(BaseModel):
    """A repository whose webhook deliveries this workspace accepts."""

    id: str = Field(description="Opaque id.")
    provider: str = Field(description="`github` in Phase 1.")
    full_name: str = Field(description="`owner/name`.")
    created_at: datetime = Field(description="When it was linked, UTC.")
