"""May this container be written to? The one answer for archived and frozen containers.

docs/design/09-restructuring.md §3.4. A space, folder or list can be **archived** — read-only
for good, history kept, reports still drawn — and a space can be **frozen**, read-only for
the length of a migration wave so that what the wave moves cannot change under it. Every
write to an issue, its comments, its time, its sprint membership, its dependencies and fix
versions, and every workflow publish, asks this module first; nothing else decides it, so
the rule cannot be applied in one route and forgotten in the next.

The answer is **423 Locked**, with a message that names the container, says why, and says
what would lift it. Reads are never refused.

The order of checks is the order of specificity a person reads a refusal in: the list they
were looking at, then its folder, then the space. A space that is both archived and frozen
says archived, because that is the state that outlives the freeze.

The migration runner (services/migration) is the one writer allowed into a frozen space, and
only into one frozen by its own plan: it passes `plan_id` and the freeze stops applying.
"""
from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.orm import Session

from models.hierarchy import Folder, ProjectList, Space
from models.issue import Issue
from schemas.response import error_json

LOCKED = 423


@dataclass(frozen=True)
class WriteBlock:
    """Why a write is refused. `status_code` is always 423 today; it is carried so a caller
    never hard-codes it."""

    status_code: int
    message: str

    def response(self):
        return error_json(self.status_code, self.message)


def space_block(space: Space | None, plan_id: str | None = None) -> WriteBlock | None:
    if space is None:
        return None
    if space.archived_at is not None:
        return WriteBlock(LOCKED, f"The space {space.name} is archived, so it is read-only. "
                                  "An admin can unarchive it.")
    if space.frozen_at is not None and (plan_id is None or space.frozen_by_plan_id != plan_id):
        reason = f" ({space.freeze_reason})" if space.freeze_reason else ""
        return WriteBlock(LOCKED, f"The space {space.name} is frozen while it is being "
                                  f"reorganised{reason}. Changes are paused until it is "
                                  "unfrozen.")
    return None


def _space(db: Session, workspace_id: str, space_id: str) -> Space | None:
    return (db.query(Space)
            .filter(Space.workspace_id == workspace_id, Space.id == space_id)
            .first())


def space_id_block(db: Session, workspace_id: str, space_id: str,
                   plan_id: str | None = None) -> WriteBlock | None:
    return space_block(_space(db, workspace_id, space_id), plan_id)


def list_block(db: Session, workspace_id: str, task_list: ProjectList | None,
               plan_id: str | None = None) -> WriteBlock | None:
    """The list, its folder (if any) and its space, in that order."""
    if task_list is None:
        return None
    if task_list.archived_at is not None:
        return WriteBlock(LOCKED, f"The list {task_list.name} is archived, so it is "
                                  "read-only. An admin can unarchive it.")
    if task_list.folder_id is not None:
        folder = (db.query(Folder)
                  .filter(Folder.workspace_id == workspace_id, Folder.id == task_list.folder_id)
                  .first())
        if folder is not None and folder.archived_at is not None:
            return WriteBlock(LOCKED, f"The folder {folder.name} is archived, so its lists "
                                      "are read-only. An admin can unarchive it.")
    return space_id_block(db, workspace_id, str(task_list.space_id), plan_id)


def list_id_block(db: Session, workspace_id: str, list_id: str,
                  plan_id: str | None = None) -> WriteBlock | None:
    task_list = (db.query(ProjectList)
                 .filter(ProjectList.workspace_id == workspace_id, ProjectList.id == list_id)
                 .first())
    return list_block(db, workspace_id, task_list, plan_id)


def issue_block(db: Session, issue: Issue | None, plan_id: str | None = None) -> WriteBlock | None:
    """Whether `issue` may be changed: the state of the list it lives in, and up from there."""
    if issue is None:
        return None
    return list_id_block(db, str(issue.workspace_id), str(issue.list_id), plan_id)


def folder_block(db: Session, workspace_id: str, folder: Folder | None,
                 plan_id: str | None = None) -> WriteBlock | None:
    """For creating a list in a folder, or renaming one: the folder, then its space."""
    if folder is None:
        return None
    if folder.archived_at is not None:
        return WriteBlock(LOCKED, f"The folder {folder.name} is archived, so it is "
                                  "read-only. An admin can unarchive it.")
    return space_id_block(db, workspace_id, str(folder.space_id), plan_id)
