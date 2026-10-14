"""Workspace, hierarchy and membership operations.

Every function that reads tenant data takes the workspace id as its first argument after the
session and filters on it, including lookups by primary key. An id is a guessable string and
a primary-key lookup without the tenant filter is how one customer reads another's space.

Since 2026-10-05 (docs/design/09-restructuring.md §3) this module also holds the space
charter, the template a new space starts from, renaming and reparenting containers, and the
preconditions of the two write states — **archived** and **frozen**. Whether a container may
be *written to* is `services/write_guard.py`'s answer and nobody else's; what is here is the
narrower question of whether a container may *change state*: an archive needs its sprints
closed and its timers stopped first, an unfreeze needs the migration plan that froze it to be
out of the way. Those answers are functions rather than controller code because the migration
runner (09-restructuring.md §8, `container_archive`) archives containers too, and must refuse
for the same reasons.

Since 2026-10-06 a workspace can be **soft-deleted** and a member **removed** (or can leave).
Both are asked the same kind of question the archive is — what would break, or keep running
where nobody can see it — and refuse with a 409 that names what to change first:
`workspace_delete_refusal` and `member_removal_refusal`. Who may do either is a role
question, answered by `utils/membership_rules.py`.
"""
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from pydantic import ValidationError
from sqlalchemy import and_, func, or_
from sqlalchemy.orm import Session

from dependencies.workspace import WorkspaceAccess
from models.hierarchy import (
    Folder,
    ProjectList,
    Space,
    SpaceKeyAlias,
    SpaceWorkflowEditor,
    WorkflowStatus,
    WorkflowTemplate,
)
from models.issue import Issue
from models.migration import MigrationPlan
from models.reporting import ReportSchedule, SavedReport
from models.sprint import Sprint
from models.time_tracking import ActiveTimer, TimeEntry
from models.user import User
from models.vcs import VcsRepository
from models.workspace import Workspace, WorkspaceMember
from schemas.response import error_json
from schemas.workspaces import SpaceCharter
from services import issue as issue_svc
from services import sprint as sprint_svc
from services import transitions as transition_svc
from services import workflow_access, workflow_templates, write_guard
from utils import workflow_rules as rules
from utils.issue_keys import format_issue_key

#: `builtin:delivery` names the same template as `delivery`. The prefix exists so a reference
#: can say "the built-in one" unambiguously in a document that also holds template ids.
BUILTIN_PREFIX = "builtin:"

#: What `require_space_charter` asks of a new space (09-restructuring.md §3.2), in the order
#: a form shows them. Dotted names are fields of the charter document; the others are columns
#: of the space. The inventory's "charter gaps" are these, so both ask the same question.
REQUIRED_CHARTER_ITEMS: tuple[str, ...] = (
    "owner_id", "delivery_method", "charter.objective", "charter.success_measure",
    "charter.reuse_justification",
)
CHARTER_ITEM_LABELS: dict[str, str] = {
    "owner_id": "an owner",
    "delivery_method": "a delivery method (scrum or kanban)",
    "charter.objective": "the objective",
    "charter.success_measure": "the success measure",
    "charter.reuse_justification": "why an existing space will not do",
}

#: A migration plan in one of these states still owns the freeze it set: the plan says to
#: keep writes paused while a wave runs and while a failed one is repaired or reversed
#: (09-restructuring.md §8.5), so only the plan itself may lift it.
PLAN_STATES_HOLDING_FREEZE: frozenset[str] = frozenset({"running", "failed"})

#: The sprint states that can still write an issue: closing carries unfinished work over, and
#: deleting a planned sprint sends its issues to the backlog. A closed sprint writes nothing.
_OPEN_SPRINT_STATES = ("planned", "active")

#: How many issue keys a refusal names before it says "and N more". A message is read, not
#: scanned; the full list is in the refusal's `data`.
_KEYS_IN_MESSAGE = 5


@dataclass(frozen=True)
class Refusal:
    """Why a container may not change state, with the status to answer it with: 409 when the
    request conflicts with the container's state (close the sprint first), 423 when something
    above it is read-only (services/write_guard.py), 404 or 422 for a bad target."""

    status_code: int
    message: str
    data: dict | None = None

    def response(self):
        return error_json(self.status_code, self.message, self.data)


def _blocked(block: write_guard.WriteBlock | None) -> Refusal | None:
    return None if block is None else Refusal(block.status_code, block.message)


def _keys_text(keys: list[str]) -> str:
    shown = ", ".join(keys[:_KEYS_IN_MESSAGE])
    hidden = len(keys) - _KEYS_IN_MESSAGE
    return shown if hidden <= 0 else f"{shown} and {hidden} more"


def workspaces_for_user(db: Session, user_id: str) -> list[tuple[Workspace, str]]:
    rows = (
        db.query(Workspace, WorkspaceMember.role)
        .join(WorkspaceMember, WorkspaceMember.workspace_id == Workspace.id)
        .filter(WorkspaceMember.user_id == user_id, Workspace.deleted == 0)
        .order_by(Workspace.name)
        .all()
    )
    return [(workspace, role) for workspace, role in rows]


def slug_taken(db: Session, slug: str) -> bool:
    """Whether any workspace has this address — a deleted one included, deliberately: a
    soft-deleted workspace keeps its slug. `workspaces.slug` is unique, and a restore must
    find its address where it left it rather than in use by somebody else, with the old
    links pointing at them."""
    return db.query(Workspace.id).filter(Workspace.slug == slug).first() is not None


def create_workspace(db: Session, user_id: str, name: str, slug: str) -> Workspace:
    workspace = Workspace(name=name, slug=slug, created_by=user_id)
    db.add(workspace)
    db.flush()
    db.add(WorkspaceMember(workspace_id=workspace.id, user_id=user_id, role="owner"))
    return workspace


def get_workspace(db: Session, workspace_id: str) -> Workspace | None:
    return (
        db.query(Workspace)
        .filter(Workspace.id == workspace_id, Workspace.deleted == 0)
        .first()
    )


def lock_workspace(db: Session, workspace_id: str) -> Workspace | None:
    """The live workspace, its row locked until the transaction ends; None once deleted.

    Renaming and deleting the workspace take it, and so does every change to its membership,
    so those happen one at a time. Two owners demoting each other at the same moment would
    otherwise each count the other as still an owner and leave the workspace with none; and
    because nobody locks a member row without holding this first, they cannot deadlock on
    each other's member rows either. SQLite ignores FOR UPDATE; it has one writer anyway.
    """
    return (
        db.query(Workspace)
        .filter(Workspace.id == workspace_id, Workspace.deleted == 0)
        .with_for_update()
        .first()
    )


def mark_deleted(workspace: Workspace, now: datetime) -> None:
    """The soft delete. Nothing under the workspace is touched — its spaces, issues, time and
    history stay exactly as they were — and the two columns are all there is to it: every
    way in (`dependencies/workspace.workspace_access`, `workspaces_for_user`,
    `get_workspace`, `services/time_tracking.member_role`) asks for `deleted == 0`, so the
    workspace disappears from every list and URL at once, and a restore is clearing them."""
    workspace.deleted = 1
    workspace.deleted_at = now


# ---- payloads ---------------------------------------------------------------------------


def status_payload(status: WorkflowStatus) -> dict:
    return {"id": status.id, "name": status.name, "category": status.category,
            "position": status.position, "color": status.color or "neutral",
            "counts_toward_wip": bool(status.counts_toward_wip),
            "wip_limit": status.wip_limit}


def list_payload(item: ProjectList) -> dict:
    return {"id": item.id, "name": item.name, "folder_id": item.folder_id,
            "position": item.position, "archived": item.archived_at is not None}


def folder_payload(folder: Folder, lists: Iterable[ProjectList]) -> dict:
    return {"id": folder.id, "name": folder.name, "position": folder.position,
            "archived": folder.archived_at is not None,
            "lists": [list_payload(item) for item in lists]}


def _initial_status_id(space: Space, statuses: list[WorkflowStatus]) -> str | None:
    """`services/transitions.initial_status`, over stages the tree has already loaded.

    The same rule — the space's chosen stage while it is live, else the first `todo` stage,
    else the first stage — because the tree cannot afford that function's query per space.
    tests/integration/test_structure.py pins the two to the same answer, including for a
    space whose chosen stage was removed.
    """
    if space.initial_status_id and any(s.id == space.initial_status_id for s in statuses):
        return str(space.initial_status_id)
    for status in statuses:
        if status.category == "todo":
            return str(status.id)
    return str(statuses[0].id) if statuses else None


def workflow_payload(space: Space, statuses: list[WorkflowStatus],
                     transitions: Iterable[tuple[str | None, str]]) -> dict:
    """`SpaceWorkflowSummary`: what the board needs to check a drag before sending it.

    Transitions with an end that is no longer a live stage are left out: no move can enter a
    removed stage (services/transitions.transition_problem refuses it), so drawing one would
    only offer a drop that fails. Board order of the ends makes the list deterministic.

    The merge target reads as null when it is not a live stage of the space — the rule of
    services/transitions.merge_target, under which such a merge fails visibly (§6.2) — so
    the SPA is never handed a stage id it cannot find among `statuses`.
    """
    order = {status.id: index for index, status in enumerate(statuses)}
    pairs = sorted(
        ((source, to) for source, to in transitions
         if to in order and (source is None or source in order)),
        key=lambda pair: (-1 if pair[0] is None else order[pair[0]], order[pair[1]]),
    )
    mode = str(space.merge_action or "status")
    target = space.merge_target_status_id
    return {
        "version": int(space.workflow_version or 1),
        "transition_mode": str(space.transition_mode or "any"),
        "initial_status_id": _initial_status_id(space, statuses),
        "wip_policy": str(space.wip_policy or "warn"),
        "merge_action": {"mode": mode,
                         "target_status_id": (target if mode == "status" and target in order
                                              else None)},
        "transitions": [{"from_status_id": source, "to_status_id": to}
                        for source, to in pairs],
    }


def charter_of(space: Space) -> SpaceCharter | None:
    """The space's charter, validated on read (09-restructuring.md §3.2), or None.

    None when nothing is filled in, and when the stored document no longer validates: a bad
    document must not fail every read of the tree. Keys `SpaceCharter` does not know are
    dropped before validating rather than failing it — writes forbid them, so one can only
    come from a newer release that added a field, and rolling that release back must not
    blank every charter it wrote.
    """
    document = space.charter
    if not isinstance(document, dict):
        return None
    known = {key: value for key, value in document.items() if key in SpaceCharter.model_fields}
    try:
        charter = SpaceCharter.model_validate(known)
    except ValidationError:
        return None
    return charter if charter.filled() else None


def charter_document(charter: SpaceCharter | None) -> dict | None:
    """What `spaces.charter` stores: the filled-in fields only, or NULL for an empty one."""
    if charter is None or not charter.filled():
        return None
    return charter.model_dump(exclude_none=True)


def charter_gaps(owner_id: str | None, delivery_method: str | None,
                 charter: SpaceCharter | None) -> list[str]:
    """Which of `REQUIRED_CHARTER_ITEMS` are missing, in their order."""
    present = {
        "owner_id": bool(owner_id),
        "delivery_method": bool(delivery_method),
        "charter.objective": bool(charter and charter.objective),
        "charter.success_measure": bool(charter and charter.success_measure),
        "charter.reuse_justification": bool(charter and charter.reuse_justification),
    }
    return [item for item in REQUIRED_CHARTER_ITEMS if not present[item]]


def space_nodes(db: Session, workspace_id: str, *, access: WorkspaceAccess | None = None,
                space_ids: Iterable[str] | None = None) -> list[dict]:
    """`SpaceNode` payloads: every live space of the workspace, or the ones in `space_ids`.

    **A fixed number of queries, whatever the number of spaces** — one per level of the tree
    (spaces, stages, folders, lists), one for every space's transitions
    (services/transitions.transitions_by_space), at most one for the caller's workflow-edit
    grants (services/workflow_access.editable_space_ids) and at most one for the owners. The
    sidebar of a workspace with forty spaces costs what one with four does;
    tests/integration/test_structure.py counts them.

    Without `access`, `can_edit_workflow` is false everywhere: the grant is about a caller.
    """
    wanted = None if space_ids is None else list(space_ids)
    if wanted is not None and not wanted:
        return []
    query = db.query(Space).filter(Space.workspace_id == workspace_id, Space.deleted == 0)
    if wanted is not None:
        query = query.filter(Space.id.in_(wanted))
    spaces = query.order_by(Space.name, Space.id).all()
    if not spaces:
        return []
    ids = [str(space.id) for space in spaces]

    def scoped(rows, column):
        # The whole-workspace tree reads each level by tenant alone; a single space's node
        # (create, PATCH, archive) narrows it to that space.
        return rows if wanted is None else rows.filter(column.in_(ids))

    # Live stages only: a removed stage keeps its row for history (models/hierarchy.py) and
    # is never a column anyone can see or drop into.
    statuses = (scoped(db.query(WorkflowStatus)
                       .filter(WorkflowStatus.workspace_id == workspace_id,
                               WorkflowStatus.deleted == 0), WorkflowStatus.space_id)
                .order_by(WorkflowStatus.position, WorkflowStatus.id).all())
    # Archived folders and lists stay in the tree, flagged, so the SPA can draw an
    # "Archived" section (09-restructuring.md §3.4). (position, name, id) is a total order:
    # positions are set as given and may tie.
    folders = (scoped(db.query(Folder)
                      .filter(Folder.workspace_id == workspace_id, Folder.deleted == 0),
                      Folder.space_id)
               .order_by(Folder.position, Folder.name, Folder.id).all())
    lists = (scoped(db.query(ProjectList)
                    .filter(ProjectList.workspace_id == workspace_id,
                            ProjectList.deleted == 0), ProjectList.space_id)
             .order_by(ProjectList.position, ProjectList.name, ProjectList.id).all())
    transitions = transition_svc.transitions_by_space(db, workspace_id, ids)
    editable = (workflow_access.editable_space_ids(db, access, ids)
                if access is not None else set())
    owners = issue_svc.users_by_id(db, (space.owner_id for space in spaces))

    statuses_by_space: dict[str, list[WorkflowStatus]] = {}
    for status in statuses:
        statuses_by_space.setdefault(str(status.space_id), []).append(status)
    folders_by_space: dict[str, list[Folder]] = {}
    for folder in folders:
        folders_by_space.setdefault(str(folder.space_id), []).append(folder)
    root_lists: dict[str, list[ProjectList]] = {}
    lists_by_folder: dict[str, list[ProjectList]] = {}
    for item in lists:
        if item.folder_id is None:
            root_lists.setdefault(str(item.space_id), []).append(item)
        else:
            lists_by_folder.setdefault(str(item.folder_id), []).append(item)

    spent_by_space: dict[str, float] = {}
    if spaces:
        space_ids = [s.id for s in spaces]
        spent_rows = (
            db.query(Issue.space_id, func.sum(TimeEntry.cost_amount))
            .join(TimeEntry, TimeEntry.issue_id == Issue.id)
            .filter(
                TimeEntry.workspace_id == workspace_id,
                TimeEntry.deleted == 0,
                Issue.deleted == 0,
                Issue.space_id.in_(space_ids),
            )
            .group_by(Issue.space_id)
            .all()
        )
        spent_by_space = {str(sid): float(spent) for sid, spent in spent_rows if spent is not None}

    nodes = []
    for space in spaces:
        space_id = str(space.id)
        stages = statuses_by_space.get(space_id, [])
        charter = charter_of(space)
        frozen = space.frozen_at is not None
        nodes.append({
            "id": space.id,
            "name": space.name,
            "key": space.key,
            "description": space.description,
            "owner": (issue_svc.user_ref(owners.get(str(space.owner_id)))
                      if space.owner_id else None),
            "delivery_method": space.delivery_method,
            "has_charter": charter is not None,
            "budget_amount": float(space.budget_amount) if space.budget_amount is not None else None,
            "budget_alert_threshold": space.budget_alert_threshold if space.budget_alert_threshold is not None else 80,
            "budget_spent": spent_by_space.get(space_id, 0.0),
            "ai_triage_enabled": space.ai_triage_enabled if space.ai_triage_enabled is not None else True,
            "ai_standup_enabled": space.ai_standup_enabled if space.ai_standup_enabled is not None else True,
            "ai_retrospective_enabled": space.ai_retrospective_enabled if space.ai_retrospective_enabled is not None else True,
            "archived": space.archived_at is not None,
            "archived_at": space.archived_at,
            "frozen": frozen,
            "freeze_reason": space.freeze_reason if frozen else None,
            "statuses": [status_payload(status) for status in stages],
            "workflow": workflow_payload(space, stages, transitions.get(space_id, [])),
            "can_edit_workflow": space_id in editable,
            "folders": [folder_payload(folder, lists_by_folder.get(str(folder.id), []))
                        for folder in folders_by_space.get(space_id, [])],
            "lists": [list_payload(item) for item in root_lists.get(space_id, [])],
        })
    return nodes


def workspace_tree(db: Session, workspace_id: str, *,
                   access: WorkspaceAccess | None = None) -> list[dict]:
    """Every live space with its workflow, folders and lists (`space_nodes`)."""
    return space_nodes(db, workspace_id, access=access)


def space_node(db: Session, access: WorkspaceAccess, space_id: str) -> dict | None:
    """One space's `SpaceNode`, the shape every route that answers with a space uses."""
    nodes = space_nodes(db, access.workspace_id, access=access, space_ids=[space_id])
    return nodes[0] if nodes else None


def key_aliases(db: Session, workspace_id: str, space_id: str) -> list[str]:
    """The keys the space used to have, oldest first."""
    rows = (db.query(SpaceKeyAlias.key)
            .filter(SpaceKeyAlias.workspace_id == workspace_id,
                    SpaceKeyAlias.space_id == space_id)
            .order_by(SpaceKeyAlias.created_at, SpaceKeyAlias.id)
            .all())
    return [str(key) for (key,) in rows]


def space_detail(db: Session, access: WorkspaceAccess, space: Space) -> dict | None:
    """`SpaceDetail`: the node, the charter and the key history."""
    node = space_node(db, access, str(space.id))
    if node is None:
        return None
    charter = charter_of(space)
    node.update({
        "charter": charter.model_dump() if charter is not None else None,
        "key_aliases": key_aliases(db, access.workspace_id, str(space.id)),
        "created_at": space.created_at,
        "frozen_at": space.frozen_at,
        "frozen_by_plan_id": space.frozen_by_plan_id,
    })
    return node


# ---- spaces -----------------------------------------------------------------------------


def get_space(db: Session, workspace_id: str, space_id: str) -> Space | None:
    return (
        db.query(Space)
        .filter(Space.workspace_id == workspace_id, Space.id == space_id, Space.deleted == 0)
        .first()
    )


def lock_space(db: Session, workspace_id: str, space_id: str) -> Space | None:
    """The space, its row locked until the transaction ends.

    Archiving, freezing and unfreezing take it, and so does starting a sprint
    (services/sprint.lock_space), so "archive refused: an active sprint" cannot be raced by
    a start landing between the check and the commit. SQLite ignores FOR UPDATE; it has one
    writer anyway.
    """
    return (
        db.query(Space)
        .filter(Space.workspace_id == workspace_id, Space.id == space_id, Space.deleted == 0)
        .with_for_update()
        .first()
    )


def space_key_taken(db: Session, workspace_id: str, key: str) -> bool:
    return (
        db.query(Space.id).filter(Space.workspace_id == workspace_id, Space.key == key).first()
        is not None
    )


def create_space(db: Session, workspace_id: str, name: str, key: str,
                 definition: dict | None = None, *, description: str | None = None,
                 owner_id: str | None = None, delivery_method: str | None = None,
                 charter: dict | None = None,
                 budget_amount: float | None = None,
                 budget_alert_threshold: int | None = 80,
                 ai_triage_enabled: bool = True,
                 ai_standup_enabled: bool = True,
                 ai_retrospective_enabled: bool = True) -> Space:
    """A space and its workflow, from a template definition (services/workflow_templates.py;
    the four-stage `default` when none is given). The definition should have been through
    `prepare_definition`; `materialize` raises ValueError for one that is out of bounds."""
    space = Space(workspace_id=workspace_id, name=name, key=key, issue_counter=0,
                  description=description, owner_id=owner_id,
                  delivery_method=delivery_method, charter=charter,
                  budget_amount=budget_amount,
                  budget_alert_threshold=budget_alert_threshold if budget_alert_threshold is not None else 80,
                  ai_triage_enabled=ai_triage_enabled,
                  ai_standup_enabled=ai_standup_enabled,
                  ai_retrospective_enabled=ai_retrospective_enabled)
    db.add(space)
    db.flush()
    workflow_templates.materialize(
        db, space,
        definition or workflow_templates.builtin_definition(
            workflow_templates.DEFAULT_TEMPLATE_KEY) or {})
    db.flush()
    return space


def change_space_key(db: Session, space: Space, new_key: str) -> str:
    """Give `space` the key `new_key` and keep its current one resolving; returns the old key.

    The old key goes to `space_key_aliases`, which keeps `PAY-7` finding the issue now called
    `PAYM-7` (services/key_resolution.resolve_key) and keeps `PAY` reserved, so no other space
    can take it and make an old reference ambiguous (09-restructuring.md §3.5). Changing back
    to one of the space's own aliases removes that alias: the key is live again. Issue keys are
    computed from the space key, so no issue row changes.

    The caller has checked `key_resolution.space_key_reserved(..., except_space_id=space.id)`.
    """
    old_key = str(space.key)
    new_key = new_key.upper()
    if new_key == old_key:
        return old_key
    own_alias = (db.query(SpaceKeyAlias)
                 .filter(SpaceKeyAlias.workspace_id == space.workspace_id,
                         SpaceKeyAlias.space_id == space.id, SpaceKeyAlias.key == new_key)
                 .first())
    if own_alias is not None:
        db.delete(own_alias)
    kept = (db.query(SpaceKeyAlias.id)
            .filter(SpaceKeyAlias.workspace_id == space.workspace_id,
                    SpaceKeyAlias.space_id == space.id, SpaceKeyAlias.key == old_key)
            .first())
    if kept is None:
        db.add(SpaceKeyAlias(workspace_id=space.workspace_id, space_id=space.id, key=old_key))
    space.key = new_key
    db.flush()
    return old_key


def space_statuses(db: Session, workspace_id: str, space_id: str) -> list[WorkflowStatus]:
    """The space's live stages in board order. Removed stages are left out: they are kept for
    history, not drawn."""
    return (
        db.query(WorkflowStatus)
        .filter(WorkflowStatus.workspace_id == workspace_id,
                WorkflowStatus.space_id == space_id, WorkflowStatus.deleted == 0)
        .order_by(WorkflowStatus.position)
        .all()
    )


# ---- workflow templates for a new space ---------------------------------------------------


def template_definition(db: Session, workspace_id: str, ref: str) -> Any | None:
    """The definition a template reference names, or None when it names nothing here.

    `default`, `delivery`, `support` and `builtin:<key>` are the built-ins
    (services/workflow_templates.BUILTIN_TEMPLATES); anything else is the id of a live
    template of THIS workspace — another tenant's template id resolves to nothing, like any
    other id of theirs. The result is whatever is stored: `prepare_definition` decides whether
    it can be applied.

    services/workflow.py resolves the same references for the workflow routes; this one exists
    so space creation does not depend on that module (two lanes built them at once, on
    2026-10-05). They must agree, and the vocabulary is small enough to keep them agreeing.
    """
    builtin = workflow_templates.builtin_definition(ref.removeprefix(BUILTIN_PREFIX))
    if builtin is not None:
        return builtin
    if ref.startswith(BUILTIN_PREFIX):
        return None
    return (db.query(WorkflowTemplate.definition)
            .filter(WorkflowTemplate.workspace_id == workspace_id,
                    WorkflowTemplate.id == ref, WorkflowTemplate.deleted == 0)
            .scalar())


def _index(value: object, size: int) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value < size:
        return None
    return value


def prepare_definition(definition: object) -> tuple[dict | None, str | None]:
    """A template definition made ready for `workflow_templates.materialize`, or why it is not.

    Returns `(definition, None)` — names trimmed, colours defaulted, duplicate transitions
    dropped as a publish drops them — or `(None, problem)`, the problem a sentence fragment to
    follow "The workflow template cannot be applied: ".

    The checks are the publish's errors (09-restructuring.md §4.4) as they apply to a
    definition, not just the bounds `materialize` itself checks. A new space must start with a
    workflow a publish would accept: otherwise its editors could publish nothing until they
    had fixed problems they did not make. Built-ins pass; stored templates were validated when
    they were saved, so a failure here means a document that was edited or written by an
    older release — which must be a 422 naming the problem, never a 500 or a half-built space.
    """
    if not isinstance(definition, dict):
        return None, "it is not a workflow definition"
    stages = definition.get("stages")
    if not isinstance(stages, list) or not stages:
        return None, "it has no stages"
    if len(stages) > rules.MAX_STAGES:
        return None, (f"it has {len(stages)} stages and a workflow holds at most "
                      f"{rules.MAX_STAGES}")
    clean: list[dict[str, Any]] = []
    names: set[str] = set()
    for number, stage in enumerate(stages, start=1):
        if not isinstance(stage, dict):
            return None, f"stage {number} is not a stage"
        raw_name = stage.get("name")
        name = raw_name.strip() if isinstance(raw_name, str) else ""
        if not name or len(name) > rules.MAX_STAGE_NAME:
            return None, (f"stage {number} needs a name of 1 to {rules.MAX_STAGE_NAME} "
                          "characters")
        if name.casefold() in names:
            return None, f"two stages are called {name}"
        names.add(name.casefold())
        category = stage.get("category")
        if category not in rules.STATUS_CATEGORIES:
            return None, f"the stage {name} has no valid reporting category"
        color = stage.get("color") or rules.DEFAULT_COLOR_BY_CATEGORY[category]
        if color not in rules.STAGE_COLORS:
            return None, f"the stage {name} has an unknown colour"
        # Unsaid means what `materialize` assumes: active stages count, the others do not.
        counts = stage.get("counts_toward_wip")
        if counts is None:
            counts = category == "in_progress"
        if not isinstance(counts, bool):
            return None, f"the stage {name} does not say whether it counts toward WIP"
        limit = stage.get("wip_limit")
        if limit is not None and (isinstance(limit, bool) or not isinstance(limit, int)
                                  or not 1 <= limit <= rules.MAX_WIP_LIMIT):
            return None, f"the stage {name} has a WIP limit outside 1-{rules.MAX_WIP_LIMIT}"
        clean.append({"name": name, "category": category, "color": color,
                      "counts_toward_wip": counts, "wip_limit": limit})
    if not any(stage["category"] == rules.COMPLETED_CATEGORY for stage in clean):
        return None, "it has no completed stage, so nothing could ever be delivered"
    mode = definition.get("transition_mode", "any")
    if mode not in rules.TRANSITION_MODES:
        return None, "its transition mode is neither any nor restricted"
    policy = definition.get("wip_policy", "warn")
    if policy not in rules.WIP_POLICIES:
        return None, "its WIP policy is neither warn nor block"
    size = len(clean)
    initial = _index(definition.get("initial", 0), size)
    if initial is None:
        return None, "its initial stage is not one of its stages"
    if rules.is_closed(clean[initial]["category"]):
        return None, f"new issues would start in {clean[initial]['name']}, a closed stage"
    target = None
    if definition.get("merge_target") is not None:
        target = _index(definition.get("merge_target"), size)
        if target is None:
            return None, "its merge target is not one of its stages"
        if clean[target]["category"] == "canceled":
            return None, "a merged pull request would move work to a canceled stage"
    raw_transitions = definition.get("transitions") or []
    if not isinstance(raw_transitions, list):
        return None, "its transitions are not a list"
    pairs: list[tuple[int | None, int]] = []
    for transition in raw_transitions:
        if not isinstance(transition, dict):
            return None, "a transition is not a pair of stages"
        source_ref = transition.get("from")
        source = None if source_ref is None else _index(source_ref, size)
        to = _index(transition.get("to"), size)
        if to is None or (source_ref is not None and source is None):
            return None, "a transition names a stage the template does not have"
        if source == to:
            return None, f"a transition goes from {clean[to]['name']} to itself"
        if (source, to) not in pairs:
            pairs.append((source, to))
    if mode == "restricted":
        reached = rules.reachable(
            str(initial), [str(index) for index in range(size)],
            [(None if source is None else str(source), str(to)) for source, to in pairs])
        stranded = [stage["name"] for index, stage in enumerate(clean)
                    if str(index) not in reached]
        if stranded:
            return None, (f"{', '.join(stranded)} cannot be reached from "
                          f"{clean[initial]['name']}")
    return {"transition_mode": mode, "wip_policy": policy, "stages": clean,
            "initial": initial, "merge_target": target,
            "transitions": [{"from": source, "to": to} for source, to in pairs]}, None


# ---- folders and lists ------------------------------------------------------------------


def get_folder(db: Session, workspace_id: str, folder_id: str) -> Folder | None:
    return (
        db.query(Folder)
        .filter(Folder.workspace_id == workspace_id, Folder.id == folder_id,
                Folder.deleted == 0)
        .first()
    )


def _next_position(db: Session, column, *filters) -> int:
    current = db.query(func.max(column)).filter(*filters).scalar()
    return 0 if current is None else int(current) + 1


def create_folder(db: Session, workspace_id: str, space_id: str, name: str) -> Folder:
    position = _next_position(db, Folder.position, Folder.space_id == space_id,
                              Folder.deleted == 0)
    folder = Folder(workspace_id=workspace_id, space_id=space_id, name=name, position=position)
    db.add(folder)
    db.flush()
    return folder


def folder_lists(db: Session, workspace_id: str, folder_id: str) -> list[ProjectList]:
    """A folder's live lists, in the tree's order."""
    return (
        db.query(ProjectList)
        .filter(ProjectList.workspace_id == workspace_id, ProjectList.folder_id == folder_id,
                ProjectList.deleted == 0)
        .order_by(ProjectList.position, ProjectList.name, ProjectList.id)
        .all()
    )


def get_list(db: Session, workspace_id: str, list_id: str) -> ProjectList | None:
    return (
        db.query(ProjectList)
        .filter(ProjectList.workspace_id == workspace_id, ProjectList.id == list_id,
                ProjectList.deleted == 0)
        .first()
    )


def create_list(db: Session, workspace_id: str, space_id: str, folder_id: str | None,
                name: str) -> ProjectList:
    position = _next_position(db, ProjectList.position, ProjectList.space_id == space_id,
                              ProjectList.folder_id == folder_id, ProjectList.deleted == 0)
    item = ProjectList(workspace_id=workspace_id, space_id=space_id, folder_id=folder_id,
                       name=name, position=position)
    db.add(item)
    db.flush()
    return item


def list_move_target(db: Session, workspace_id: str, task_list: ProjectList,
                     folder_id: str | None,
                     plan_id: str | None = None) -> tuple[Folder | None, Refusal | None]:
    """The folder a list may move to — one of its own space, writable — or the refusal.

    `folder_id` None is the space root, always a valid place. Another space's folder is a 422
    and not a 404: the folder exists and is the caller's, it is the move that is wrong —
    moving a list to another space re-keys its issues, which is a transfer (§3.5).
    """
    if folder_id is None:
        return None, None
    folder = get_folder(db, workspace_id, folder_id)
    if folder is None:
        return None, Refusal(404, "Folder not found")
    if folder.space_id != task_list.space_id:
        return None, Refusal(422, "A list can only move to a folder of its own space. Moving "
                                  "it to another space changes its issues' keys, so it is a "
                                  "transfer of its issues.")
    refusal = _blocked(write_guard.folder_block(db, workspace_id, folder, plan_id))
    return (None, refusal) if refusal is not None else (folder, None)


def move_list(db: Session, workspace_id: str, task_list: ProjectList, folder_id: str | None,
              position: int | None) -> None:
    """Put `task_list` under `folder_id` (None: the space root), at `position`.

    A list that changes place without a position goes to the bottom of its new place — where
    a new list would go — rather than keeping a number that meant something among its old
    siblings. Positions are set as given otherwise: nothing else is renumbered, and the tree
    breaks ties by name.
    """
    if folder_id != task_list.folder_id:
        task_list.folder_id = folder_id
        if position is None:
            position = _next_position(db, ProjectList.position,
                                      ProjectList.workspace_id == workspace_id,
                                      ProjectList.space_id == task_list.space_id,
                                      ProjectList.folder_id == folder_id,
                                      ProjectList.deleted == 0,
                                      ProjectList.id != task_list.id)
    if position is not None:
        task_list.position = position
    db.flush()


# ---- write states: archive and freeze (09-restructuring.md §3.4) ---------------------------


def running_timer_keys(db: Session, workspace_id: str, *, space_id: str | None = None,
                       folder_id: str | None = None, list_id: str | None = None,
                       user_id: str | None = None) -> list[str]:
    """Keys of the live issues in a container that someone has a timer running on.

    Archiving refuses while there are any: stopping a timer writes a time entry, and once the
    container is read-only that write would be refused — the time would be lost.
    A timer on a deleted issue is left out; stopping it discards it anyway
    (services/time_tracking.finish_timer).

    With no container, the whole workspace (deleting it); with `user_id`, only that
    person's timer (removing them from it).
    """
    query = (db.query(Space.key, Issue.number)
             .select_from(ActiveTimer)
             .join(Issue, Issue.id == ActiveTimer.issue_id)
             .join(Space, Space.id == Issue.space_id)
             .filter(ActiveTimer.workspace_id == workspace_id,
                     Issue.workspace_id == workspace_id, Issue.deleted == 0))
    if space_id is not None:
        query = query.filter(Issue.space_id == space_id)
    if list_id is not None:
        query = query.filter(Issue.list_id == list_id)
    if folder_id is not None:
        query = (query.join(ProjectList, ProjectList.id == Issue.list_id)
                 .filter(ProjectList.folder_id == folder_id))
    if user_id is not None:
        query = query.filter(ActiveTimer.user_id == user_id)
    rows = query.order_by(Space.key, Issue.number).distinct().all()
    return [format_issue_key(str(key), int(number)) for key, number in rows]


def _sprint_held_issues(db: Session, workspace_id: str, space: Space, *,
                        folder_id: str | None = None,
                        list_id: str | None = None) -> tuple[list[str], list[str]]:
    """Keys of a folder's or list's live issues in a sprint that is not closed, and the names
    of those sprints."""
    query = (db.query(Issue.number, Sprint.name)
             .join(Sprint, Sprint.id == Issue.sprint_id)
             .filter(Issue.workspace_id == workspace_id, Issue.space_id == space.id,
                     Issue.deleted == 0, Sprint.workspace_id == workspace_id,
                     Sprint.deleted == 0, Sprint.state.in_(_OPEN_SPRINT_STATES)))
    if list_id is not None:
        query = query.filter(Issue.list_id == list_id)
    if folder_id is not None:
        query = (query.join(ProjectList, ProjectList.id == Issue.list_id)
                 .filter(ProjectList.folder_id == folder_id))
    rows = query.order_by(Issue.number).all()
    keys = [format_issue_key(str(space.key), int(number)) for number, _name in rows]
    sprints = sorted({str(name) for _number, name in rows})
    return keys, sprints


def _timer_refusal(keys: list[str], name: str, done: str = "archived") -> Refusal:
    if len(keys) == 1:
        message = (f"A timer is running on {keys[0]}. It has to be stopped before {name} can "
                   f"be {done}.")
    else:
        message = (f"Timers are running on {_keys_text(keys)}. They have to be stopped "
                   f"before {name} can be {done}.")
    return Refusal(409, message, {"issue_keys": keys})


def space_archive_refusal(db: Session, workspace_id: str, space: Space,
                          plan_id: str | None = None) -> Refusal | None:
    """Why `space` cannot be archived now, or None (09-restructuring.md §3.4).

    * Already archived.
    * Frozen: a migration wave is working on it. The plan that froze it (`plan_id`) may
      archive it as one of its steps — that is the plan's `container_archive` operation.
    * An active sprint: an archived space can never close it.
    * A running timer on one of its issues.
    """
    if space.archived_at is not None:
        return Refusal(409, f"{space.name} is already archived.")
    if space.frozen_at is not None and (plan_id is None or space.frozen_by_plan_id != plan_id):
        return Refusal(409, f"{space.name} is frozen while it is being reorganised. Unfreeze "
                            "it before archiving it.")
    sprint = sprint_svc.active_sprint(db, workspace_id, str(space.id))
    if sprint is not None:
        return Refusal(409, f"Close {sprint.name} before archiving {space.name}.",
                       {"sprint_id": sprint.id})
    keys = running_timer_keys(db, workspace_id, space_id=str(space.id))
    return _timer_refusal(keys, space.name) if keys else None


def _holder_block(db: Session, workspace_id: str, container: Folder | ProjectList,
                  plan_id: str | None) -> Refusal | None:
    """The write state of what holds a folder or a list — a list's folder, then the space.

    For changing the container's *own* state: archiving and unarchiving a list inside an
    archived folder or space, or a frozen space, is a write there like any other (423), and
    the way out is to unarchive or unfreeze from the top down. The container's own flag is
    the thing being changed, so it is deliberately not asked.
    """
    if isinstance(container, ProjectList) and container.folder_id is not None:
        # Unfiltered by `deleted`, as services/write_guard.list_block reads it.
        folder = (db.query(Folder)
                  .filter(Folder.workspace_id == workspace_id, Folder.id == container.folder_id)
                  .first())
        if folder is not None:
            return _blocked(write_guard.folder_block(db, workspace_id, folder, plan_id))
    return _blocked(write_guard.space_id_block(db, workspace_id, str(container.space_id),
                                               plan_id))


def container_archive_refusal(db: Session, workspace_id: str,
                              container: Folder | ProjectList,
                              plan_id: str | None = None) -> Refusal | None:
    """Why a folder or list cannot be archived now, or None.

    * Already archived (409).
    * What holds it is read-only (423, services/write_guard.py).
    * One of its issues is in a sprint that is not closed (409). Closing that sprint carries
      unfinished issues over, and deleting a planned one sends its issues to the backlog:
      both write the issue, which an archived list must refuse — so the sprint could neither
      be closed cleanly nor keep the archive read-only. The space's own archive asks only for
      its active sprint, because a planned sprint of an archived space never starts.
    * A running timer on one of its issues (409).
    """
    kind = "folder" if isinstance(container, Folder) else "list"
    if container.archived_at is not None:
        return Refusal(409, f"The {kind} {container.name} is already archived.")
    held = _holder_block(db, workspace_id, container, plan_id)
    if held is not None:
        return held
    space = (db.query(Space)
             .filter(Space.workspace_id == workspace_id, Space.id == container.space_id)
             .first())
    if space is None:
        return None
    scope = ({"folder_id": str(container.id)} if isinstance(container, Folder)
             else {"list_id": str(container.id)})
    keys, sprints = _sprint_held_issues(db, workspace_id, space, **scope)
    if keys:
        issues = f"{_keys_text(keys)} {'is' if len(keys) == 1 else 'are'}"
        them = "it" if len(keys) == 1 else "them"
        if len(sprints) == 1:
            message = (f"{issues} in {sprints[0]}, which is not closed. Close {sprints[0]} or "
                       f"take {them} out of it before archiving {container.name}.")
        else:
            message = (f"{issues} in sprints that are not closed ({', '.join(sprints)}). "
                       f"Close them or take the issues out of them before archiving "
                       f"{container.name}.")
        return Refusal(409, message, {"issue_keys": keys, "sprints": sprints})
    timers = running_timer_keys(db, workspace_id, **scope)
    return _timer_refusal(timers, container.name) if timers else None


def container_unarchive_refusal(db: Session, workspace_id: str,
                                container: Folder | ProjectList,
                                plan_id: str | None = None) -> Refusal | None:
    """Why a folder or list cannot be unarchived now, or None: it is not archived (409), or
    what holds it is read-only (423) — unarchive the folder or space first."""
    kind = "folder" if isinstance(container, Folder) else "list"
    if container.archived_at is None:
        return Refusal(409, f"The {kind} {container.name} is not archived.")
    return _holder_block(db, workspace_id, container, plan_id)


def set_archived(container: Space | Folder | ProjectList, user_id: str,
                 now: datetime) -> None:
    container.archived_at = now
    container.archived_by = user_id


def clear_archived(container: Space | Folder | ProjectList) -> None:
    container.archived_at = None
    container.archived_by = None


def freeze_refusal(space: Space) -> Refusal | None:
    """Why `space` cannot be frozen: archived (it is already read-only for good) or frozen."""
    if space.archived_at is not None:
        return Refusal(409, f"{space.name} is archived, so it is already read-only. Unarchive "
                            "it before freezing it.")
    if space.frozen_at is not None:
        reason = f" ({space.freeze_reason})" if space.freeze_reason else ""
        return Refusal(409, f"{space.name} is already frozen{reason}.")
    return None


def freeze(space: Space, user_id: str, reason: str, now: datetime,
           plan_id: str | None = None) -> None:
    """Make `space` read-only until it is unfrozen. `plan_id` is the migration plan freezing
    it (its runner is then the one writer allowed in, services/write_guard.space_block)."""
    space.frozen_at = now
    space.frozen_by = user_id
    space.freeze_reason = reason
    space.frozen_by_plan_id = plan_id


def unfreeze_refusal(db: Session, workspace_id: str, space: Space) -> Refusal | None:
    """Why `space` cannot be unfrozen by hand: it is not frozen, or a migration plan froze it
    and is `running` or `failed` (`PLAN_STATES_HOLDING_FREEZE`)."""
    if space.frozen_at is None:
        return Refusal(409, f"{space.name} is not frozen.")
    if not space.frozen_by_plan_id:
        return None
    plan = (db.query(MigrationPlan)
            .filter(MigrationPlan.workspace_id == workspace_id,
                    MigrationPlan.id == space.frozen_by_plan_id)
            .first())
    if plan is None or plan.status not in PLAN_STATES_HOLDING_FREEZE:
        return None
    if plan.status == "running":
        what = ("is running. Let it finish, or roll it back, before unfreezing "
                f"{space.name}.")
    else:
        what = ("failed. Fix the failed item and execute the plan again, or roll it back, "
                f"before unfreezing {space.name}.")
    return Refusal(409, f"{space.name} is frozen by the migration plan {plan.name}, which "
                        f"{what}", {"plan_id": plan.id, "plan_status": plan.status})


def unfreeze(space: Space) -> None:
    space.frozen_at = None
    space.frozen_by = None
    space.freeze_reason = None
    space.frozen_by_plan_id = None


# ---- deleting a workspace ---------------------------------------------------------------


def workspace_delete_refusal(db: Session, workspace: Workspace) -> Refusal | None:
    """Why `workspace` cannot be deleted now, or None.

    The archive's question (`space_archive_refusal`) asked of the whole workspace: what would
    keep running where nobody can see it. Deleted, the workspace is unreachable for everyone,
    its owner included, so anything still in motion inside it could never be stopped.

    * **A migration plan that is running.** Its runner writes into the workspace wave by
      wave — the reason archiving refuses a frozen space. Deleted under it, the wave would be
      half applied and nobody could reconcile it or roll it back. A plan that froze a space
      and then *failed* runs nothing, so it does not hold the delete up.
    * **A running timer on one of its issues**, `data.issue_keys`. Afterwards its owner
      could not see it (`GET /timer` hides a timer in a workspace they cannot reach) and
      stopping it would discard the time (`services/time_tracking.finish_timer`).

    Not refused, unlike archiving, because nothing runs: an **active sprint** (nothing closes
    a sprint by itself; a restore gives it back as it was), a **frozen space** (its deferred
    GitHub deliveries are held, not applied, and the webhook ignores a deleted workspace's
    repositories), **report schedules** (they send only to members of a live workspace —
    `services/reports/scheduler.enqueue_for` asks `member_role`, which a deleted workspace
    answers with nobody), and **queued report jobs** (the worker fails them for the same
    reason).
    """
    plan = (db.query(MigrationPlan)
            .filter(MigrationPlan.workspace_id == workspace.id,
                    MigrationPlan.status == "running")
            .order_by(MigrationPlan.created_at, MigrationPlan.id)
            .first())
    if plan is not None:
        return Refusal(409, f"The migration plan {plan.name} is running in {workspace.name}. "
                            "Let it finish, or roll it back, before deleting the workspace.",
                       {"plan_id": plan.id, "plan_status": plan.status})
    keys = running_timer_keys(db, str(workspace.id))
    return _timer_refusal(keys, str(workspace.name), "deleted") if keys else None


# ---- members and repositories -----------------------------------------------------------


def members(db: Session, workspace_id: str) -> list[tuple[WorkspaceMember, User]]:
    rows = (
        db.query(WorkspaceMember, User)
        .join(User, User.id == WorkspaceMember.user_id)
        .filter(WorkspaceMember.workspace_id == workspace_id, User.deleted == 0)
        .order_by(User.first_name, User.last_name)
        .all()
    )
    return [(member, user) for member, user in rows]


def member_payload(member: WorkspaceMember, user: User) -> dict:
    return {"user_id": user.id, "email": user.email, "first_name": user.first_name,
            "last_name": user.last_name, "role": member.role}


def is_member(db: Session, workspace_id: str, user_id: str) -> bool:
    return (
        db.query(WorkspaceMember.id)
        .filter(WorkspaceMember.workspace_id == workspace_id, WorkspaceMember.user_id == user_id)
        .first()
        is not None
    )


def lock_member(db: Session, workspace_id: str,
                user_id: str) -> tuple[WorkspaceMember, User] | None:
    """A member whose account is live, their membership row locked until the transaction
    ends; None for anyone else.

    "A member" is what `members` lists. A soft-deleted account keeps its row but is not
    listed and cannot sign in, so it is not offered for a role change or a removal and
    answers 404 like any other non-member. Locked rather than read because under MySQL's
    REPEATABLE READ a plain read sees the snapshot of the request's first query, and would
    miss a role change committed since — by the very request this one queued behind in
    `lock_workspace`. Callers hold that lock first.
    """
    row = (db.query(WorkspaceMember, User)
           .join(User, User.id == WorkspaceMember.user_id)
           .filter(WorkspaceMember.workspace_id == workspace_id,
                   WorkspaceMember.user_id == user_id, User.deleted == 0)
           .with_for_update(of=WorkspaceMember)
           .first())
    return (row[0], row[1]) if row is not None else None


def live_owner_count(db: Session, workspace_id: str) -> int:
    """How many owners the workspace has whose accounts are live — the count the last-owner
    rule asks for (utils/membership_rules.py says why only those count). A locking read for
    the reason `lock_member` gives; the rows are counted here rather than with COUNT(*),
    which PostgreSQL will not combine with FOR UPDATE."""
    rows = (db.query(WorkspaceMember.id)
            .join(User, User.id == WorkspaceMember.user_id)
            .filter(WorkspaceMember.workspace_id == workspace_id,
                    WorkspaceMember.role == "owner", User.deleted == 0)
            .with_for_update(of=WorkspaceMember)
            .all())
    return len(rows)


def owned_spaces(db: Session, workspace_id: str, user_id: str) -> list[Space]:
    """The spaces `user_id` owns that are still in use — live and not archived — by key."""
    return (db.query(Space)
            .filter(Space.workspace_id == workspace_id, Space.owner_id == user_id,
                    Space.deleted == 0, Space.archived_at.is_(None))
            .order_by(Space.key)
            .all())


def schedules_tied_to(db: Session, workspace_id: str,
                      user_id: str) -> list[tuple[ReportSchedule, SavedReport]]:
    """The enabled report schedules that would stop working without `user_id`: the ones they
    set up, which render as them, and the ones on their private reports, which nobody else
    can open to stop. Disabled and deleted schedules run nothing and are left out."""
    return (db.query(ReportSchedule, SavedReport)
            .join(SavedReport, SavedReport.id == ReportSchedule.saved_report_id)
            .filter(ReportSchedule.workspace_id == workspace_id,
                    SavedReport.workspace_id == workspace_id,
                    ReportSchedule.deleted == 0, ReportSchedule.enabled.is_(True),
                    SavedReport.deleted == 0,
                    or_(ReportSchedule.created_by == user_id,
                        and_(SavedReport.owner_id == user_id,
                             SavedReport.visibility == "private")))
            .order_by(SavedReport.name, ReportSchedule.created_at, ReportSchedule.id)
            .all())


def _in_words(labels: list[str]) -> str:
    """`A`, `A and B`, `A, B and C` — the first `_KEYS_IN_MESSAGE`, then "and N more"."""
    shown = labels[:_KEYS_IN_MESSAGE]
    hidden = len(labels) - len(shown)
    if hidden > 0:
        return f"{', '.join(shown)} and {hidden} more"
    return shown[0] if len(shown) == 1 else f"{', '.join(shown[:-1])} and {shown[-1]}"


def member_removal_refusal(db: Session, workspace_id: str, user: User,
                           caller_id: str) -> Refusal | None:
    """Why `user` cannot leave the workspace, or be removed from it, yet — every reason at
    once, each saying what to change first — or None.

    The invariants that assume membership, as searched for on 2026-10-06. **Refused**,
    because the removal would break them:

    * **A space they own**, `data.spaces`. `owner_id` must be a member of the workspace
      (the controller's `_OWNER_NOT_MEMBER`), and under `require_space_charter` the owner is
      a checklist item that may not be cleared, so the space needs another owner rather than
      none. An **archived** space is left out: it is read-only for good, its owner is the
      record of who was accountable, and changing it would mean unarchiving and archiving it
      again — which rewrites when, and by whom, it was archived.
    * **Their running timer here**, `data.issue_keys`. Afterwards `GET /timer` hides it and
      stopping it discards the time (services/time_tracking.finish_timer). Only they can stop
      or discard it — as only its owner can stop a timer that holds up an archive.
    * **Report schedules that need them**, `data.schedules` (`schedules_tied_to`). The
      worker fails every job whose requester is no longer a member, so a schedule they set
      up would stop reaching anyone. Disabling or deleting it is enough. A schedule on a
      report the caller cannot see is counted, not named: a private report's name is its
      owner's business.

    **Kept as history** — "assigned issues stay assigned": the issues they are assigned to
    (epics they own included), reported, commented on or flagged blocked; the time they
    logged; the sprints, templates, workflow revisions, repositories and migration plans
    they created, and the migration-register items they own (nothing reads that owner
    yet); their saved views and reports — a shared one stays an admin's to delete.
    Schedules that only *send to* them need nothing: the scheduler and the worker skip a
    recipient who is no longer a member. Their workflow-edit grants are not kept — the
    controller revokes them with the membership (`revoke_workflow_grants`).
    """
    user_id = str(user.id)
    spaces = owned_spaces(db, workspace_id, user_id)
    timer_keys = running_timer_keys(db, workspace_id, user_id=user_id)
    schedules = schedules_tied_to(db, workspace_id, user_id)
    if not (spaces or timer_keys or schedules):
        return None
    leaving = user_id == caller_id
    they, their = ("You", "Your") if leaving else ("They", "Their")
    reasons: list[str] = []
    data: dict[str, Any] = {}
    if spaces:
        keys = [str(space.key) for space in spaces]
        one = len(keys) == 1
        reasons.append(f"{they} own {'the space' if one else 'the spaces'} {_in_words(keys)}, "
                       f"and a space's owner has to be a member: give "
                       f"{'it' if one else 'them'} another owner.")
        data["spaces"] = [{"id": space.id, "key": space.key, "name": space.name}
                          for space in spaces]
    if timer_keys:
        reasons.append(f"{their} timer is running on {timer_keys[0]}: "
                       + ("stop or discard it." if leaving else
                          "they have to stop or discard it, or the time on it is lost."))
        data["issue_keys"] = timer_keys
    if schedules:
        named: list[str] = []
        unnamed = 0
        listed: list[dict[str, Any]] = []
        for schedule, report in schedules:
            seen = report.visibility == "workspace" or report.owner_id == caller_id
            if seen and report.name not in named:
                named.append(str(report.name))
            unnamed += 0 if seen else 1
            listed.append({"id": schedule.id, "saved_report_id": report.id,
                           "saved_report_name": report.name if seen else None})
        if unnamed:
            named.append(f"{unnamed} on a private report" if unnamed == 1
                         else f"{unnamed} on private reports")
        if leaving:
            remedy = "delete or disable them, or ask someone who is staying to set them up again."
        else:
            remedy = "delete or disable them, or set them up again as someone who is staying."
        reasons.append(f"{their} report schedules would stop without "
                       f"{'you' if leaving else 'them'} ({_in_words(named)}): {remedy}")
        data["schedules"] = listed
    lead = ("You cannot leave this workspace yet." if leaving
            else f"{issue_svc.display_name(user)} cannot be removed from this workspace yet.")
    return Refusal(409, " ".join([lead, *reasons]), data)


def revoke_workflow_grants(db: Session, workspace_id: str, user_id: str) -> int:
    """Delete `user_id`'s workflow-edit grants in the workspace; returns how many.

    A grant is a permission given to a member, not a record of something they did — the
    audit trail keeps who granted what. Left behind, it would be hidden while they are gone
    (services/workflow.editor_payloads lists members only) and would quietly come back if
    they were ever added again, which nobody re-adding them would expect.
    """
    return (db.query(SpaceWorkflowEditor)
            .filter(SpaceWorkflowEditor.workspace_id == workspace_id,
                    SpaceWorkflowEditor.user_id == user_id)
            .delete(synchronize_session=False))


def active_user_by_email(db: Session, email: str) -> User | None:
    return (
        db.query(User)
        .filter(func.lower(User.email) == email.strip().lower(), User.deleted == 0)
        .first()
    )


def repositories(db: Session, workspace_id: str) -> list[VcsRepository]:
    return (
        db.query(VcsRepository)
        .filter(VcsRepository.workspace_id == workspace_id)
        .order_by(VcsRepository.full_name)
        .all()
    )


def repository_by_name(db: Session, provider: str, full_name: str) -> VcsRepository | None:
    return (
        db.query(VcsRepository)
        .filter(VcsRepository.provider == provider,
                func.lower(VcsRepository.full_name) == full_name.lower())
        .first()
    )


def repository_payload(repo: VcsRepository) -> dict:
    return {"id": repo.id, "provider": repo.provider, "full_name": repo.full_name,
            "created_at": repo.created_at}
