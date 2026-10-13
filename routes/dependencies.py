"""Typed dependency routes: blocks, relates, duplicates, clones (09-restructuring.md §10).

Thin, like every route module (CLAUDE.md §2). A dependency is listed and created under the
issue it is read from, and deleted by its own id — the same row is reachable from both of its
issues, so it has no single parent path.
"""
from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from controllers import dependency_controller as ctl
from db.session import get_db
from dependencies.workspace import WorkspaceAccess, as_guest, as_member
from schemas.dependencies import DependencyCreate
from schemas.errors import COMMON_ERRORS
from schemas.response import ErrorResponse, SuccessResponse
from schemas.responses.dependencies import DependencySummary

router = APIRouter(prefix="/workspaces/{workspace_id}", tags=["Dependencies"])

#: The write guard's answer (services/write_guard.py), declared on every route that writes to
#: an issue. routes/releases.py uses it too.
LOCKED = {423: {"model": ErrorResponse,
                "description": "Read-only: the issue's list, folder or space is archived, or "
                               "its space is frozen while a migration runs. The message says "
                               "which and what lifts it. Reads are never refused."}}
NO_ISSUE = {404: {"model": ErrorResponse,
                  "description": "No such workspace or issue — or an issue of another "
                                 "workspace, which is deliberately the same answer."}}


@router.get(
    "/issues/{issue_id}/dependencies",
    response_model=SuccessResponse[list[DependencySummary]],
    responses={**COMMON_ERRORS, **NO_ISSUE},
    summary="List an issue's typed dependencies",
    operation_id="list_issue_dependencies",
)
def list_dependencies(issue_id: str,
                      access: WorkspaceAccess = Depends(as_guest),
                      db: Session = Depends(get_db)):
    """Both directions, read from this issue's side: what blocks it, what it blocks, what it
    relates to, duplicates and clones. Grouped by label, oldest first within one. A fixed
    number of queries, however many there are."""
    return ctl.list_dependencies(access, issue_id, db)


@router.post(
    "/issues/{issue_id}/dependencies",
    response_model=SuccessResponse[DependencySummary],
    status_code=201,
    responses={
        **COMMON_ERRORS, **LOCKED,
        404: {"model": ErrorResponse,
              "description": "No such workspace or issue, or the target id or key names no "
                             "issue of this workspace."},
        409: {"model": ErrorResponse,
              "description": "That dependency already exists — for `relates`, either way "
                             "round. `data.dependency_id` is the existing one."},
        422: {"model": ErrorResponse,
              "description": "Not exactly one of `target_issue_id` / `target_key`, an unknown "
                             "kind, a malformed key, or an issue linked to itself."},
    },
    summary="Link an issue to another issue of the workspace",
    operation_id="create_issue_dependency",
)
def create_dependency(issue_id: str, body: DependencyCreate,
                      access: WorkspaceAccess = Depends(as_member),
                      db: Session = Depends(get_db)):
    """Any two issues of the workspace, including in different spaces — that is what this is
    for. The target may be named by an old key. Records `dependency_added` on both issues."""
    return ctl.create_dependency(access, issue_id, body, db)


@router.delete(
    "/dependencies/{dependency_id}",
    response_model=SuccessResponse[dict],
    responses={**COMMON_ERRORS, **LOCKED,
               404: {"model": ErrorResponse,
                     "description": "No such workspace or dependency, or one of another "
                                    "workspace."}},
    summary="Remove a typed dependency",
    operation_id="delete_dependency",
)
def delete_dependency(dependency_id: str,
                      access: WorkspaceAccess = Depends(as_member),
                      db: Session = Depends(get_db)):
    """From both issues at once, with `dependency_removed` recorded on each."""
    return ctl.delete_dependency(access, dependency_id, db)
