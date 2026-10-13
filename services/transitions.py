"""The workflow as the rest of the API reads it: live stages, the initial stage, allowed moves.

docs/design/09-restructuring.md §4.3. Defining a workflow — the draft, the preview, the
publish — is `services/workflow.py`. *Obeying* one is this module, and everything that moves
an issue between stages asks it: the board move, the GitHub merge action, issue creation,
and the SPA through the transitions the tree and the board return. One answer, so the rules
cannot be enforced on the board and bypassed by the webhook.

A **live** stage is one that has not been removed (`deleted == 0`). Removed stages keep their
rows so history resolves (models/hierarchy.py), and nothing may move an issue into one.

Every query carries the workspace id, like every other service (services/workspace.py).
"""
from __future__ import annotations

from collections.abc import Iterable

from sqlalchemy.orm import Session

from models.hierarchy import Space, WorkflowStatus, WorkflowTransition
from utils import workflow_rules as rules


def live_statuses(db: Session, workspace_id: str, space_id: str) -> list[WorkflowStatus]:
    """The space's stages in board order, removed ones left out."""
    return (db.query(WorkflowStatus)
            .filter(WorkflowStatus.workspace_id == workspace_id,
                    WorkflowStatus.space_id == space_id, WorkflowStatus.deleted == 0)
            .order_by(WorkflowStatus.position, WorkflowStatus.id)
            .all())


def get_live_status(db: Session, workspace_id: str, status_id: str) -> WorkflowStatus | None:
    return (db.query(WorkflowStatus)
            .filter(WorkflowStatus.workspace_id == workspace_id,
                    WorkflowStatus.id == status_id, WorkflowStatus.deleted == 0)
            .first())


def initial_status(db: Session, workspace_id: str, space: Space) -> WorkflowStatus | None:
    """Where new issues start: the space's chosen initial stage, while it is live; else the
    first `todo` stage; else the first stage of any category — the rule a space followed
    before it could choose (and the one a space migrated without a choice still gets)."""
    statuses = live_statuses(db, workspace_id, str(space.id))
    if space.initial_status_id:
        for status in statuses:
            if status.id == space.initial_status_id:
                return status
    for status in statuses:
        if status.category == "todo":
            return status
    return statuses[0] if statuses else None


def transition_pairs(db: Session, workspace_id: str,
                     space_id: str) -> list[tuple[str | None, str]]:
    rows = (db.query(WorkflowTransition.from_status_id, WorkflowTransition.to_status_id)
            .filter(WorkflowTransition.workspace_id == workspace_id,
                    WorkflowTransition.space_id == space_id)
            .all())
    return [(from_id, to_id) for from_id, to_id in rows]


def transitions_by_space(db: Session, workspace_id: str,
                         space_ids: Iterable[str]) -> dict[str, list[tuple[str | None, str]]]:
    """Every space's transitions in one query, for the workspace tree."""
    ids = list(space_ids)
    result: dict[str, list[tuple[str | None, str]]] = {space_id: [] for space_id in ids}
    if not ids:
        return result
    rows = (db.query(WorkflowTransition.space_id, WorkflowTransition.from_status_id,
                     WorkflowTransition.to_status_id)
            .filter(WorkflowTransition.workspace_id == workspace_id,
                    WorkflowTransition.space_id.in_(ids))
            .all())
    for space_id, from_id, to_id in rows:
        result.setdefault(space_id, []).append((from_id, to_id))
    return result


def transition_problem(db: Session, workspace_id: str, space: Space,
                       from_status: WorkflowStatus, to_status: WorkflowStatus) -> str | None:
    """Why an issue in `from_status` may not move to `to_status`, or None when it may.

    Written to be shown, and it names where the issue *can* go — a refusal with no next step
    is the least useful message a board can give.
    """
    if to_status.space_id != space.id or to_status.deleted:
        return "That stage is not part of this issue's workflow."
    if from_status.id == to_status.id or space.transition_mode != "restricted":
        return None
    pairs = transition_pairs(db, workspace_id, str(space.id))
    if rules.transition_allowed("restricted", pairs, str(from_status.id), str(to_status.id)):
        return None
    statuses = live_statuses(db, workspace_id, str(space.id))
    names = {status.id: status.name for status in statuses}
    allowed = rules.destinations("restricted", [status.id for status in statuses], pairs,
                                 str(from_status.id))
    where = (f"From {from_status.name} an issue can move to: "
             f"{', '.join(names[s] for s in allowed)}." if allowed
             else f"Nothing can leave {from_status.name} in this workflow.")
    return (f"{from_status.name} → {to_status.name} is not an allowed move in this "
            f"workflow. {where}")


def allowed_destinations(db: Session, workspace_id: str, space: Space,
                         from_status_id: str) -> list[WorkflowStatus]:
    statuses = live_statuses(db, workspace_id, str(space.id))
    pairs = transition_pairs(db, workspace_id, str(space.id))
    allowed = set(rules.destinations(str(space.transition_mode),
                                     [status.id for status in statuses], pairs,
                                     from_status_id))
    return [status for status in statuses if status.id in allowed]


def create_status_problem(space: Space, status: WorkflowStatus,
                          initial: WorkflowStatus | None) -> str | None:
    """Why a new issue may not be created in `status`: in a restricted workflow every issue
    starts in the initial stage, the way Jira's does."""
    if space.transition_mode != "restricted" or initial is None or status.id == initial.id:
        return None
    return f"New issues start in {initial.name} in this workflow."


def category_of(db: Session, workspace_id: str, status_id: str | None) -> str | None:
    """A stage's category, removed stages included — history asks about those too."""
    if status_id is None:
        return None
    return (db.query(WorkflowStatus.category)
            .filter(WorkflowStatus.workspace_id == workspace_id, WorkflowStatus.id == status_id)
            .scalar())


def merge_target(db: Session, workspace_id: str, space: Space) -> WorkflowStatus | None:
    """The live stage a merged pull request moves issues to, or None (mode `none`, or a
    target that is no longer a live stage of the space — which the merge reports as a
    failure rather than guessing another, 09-restructuring.md §6.2)."""
    if space.merge_action != "status" or not space.merge_target_status_id:
        return None
    status = get_live_status(db, workspace_id, str(space.merge_target_status_id))
    if status is None or status.space_id != space.id:
        return None
    return status
