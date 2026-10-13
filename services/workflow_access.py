"""Who may edit a space's workflow (docs/design/09-restructuring.md §4.6).

The restructuring plan asks for two things at once: "restrict editing to users with
workflow-edit permission", and let "authorized users customize their Space's workflow
without requiring administrator intervention for each change". The answer is a grant: admins
and owners may edit every space's workflow, and an admin may grant a member edit rights on
one space (`space_workflow_editors`) — once — after which that member changes it freely.

Guests never edit, even when granted: a guest is read-only everywhere else, and a workflow
publish moves other people's issues.

Everyone who may move issues keeps executing whatever transitions the workflow allows; this
module only decides who may change the workflow itself.
"""
from __future__ import annotations

from sqlalchemy.orm import Session

from dependencies.workspace import WorkspaceAccess
from models.hierarchy import SpaceWorkflowEditor


def editor_space_ids(db: Session, workspace_id: str, user_id: str) -> set[str]:
    rows = (db.query(SpaceWorkflowEditor.space_id)
            .filter(SpaceWorkflowEditor.workspace_id == workspace_id,
                    SpaceWorkflowEditor.user_id == user_id)
            .all())
    return {space_id for (space_id,) in rows}


def can_edit_workflow(db: Session, access: WorkspaceAccess, space_id: str) -> bool:
    if access.at_least("admin"):
        return True
    if not access.at_least("member"):
        return False
    return space_id in editor_space_ids(db, access.workspace_id, access.user_id)


def editable_space_ids(db: Session, access: WorkspaceAccess,
                       space_ids: list[str]) -> set[str]:
    """Of `space_ids`, the ones the caller may edit — one query, for the workspace tree."""
    if access.at_least("admin"):
        return set(space_ids)
    if not access.at_least("member"):
        return set()
    return editor_space_ids(db, access.workspace_id, access.user_id) & set(space_ids)
