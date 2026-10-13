"""Saved board filter routes (docs/design/09-restructuring.md §5.4).

Thin, like every route module (CLAUDE.md §2). Views belong to the workspace, not to the list,
space or sprint they show, so they live at `/saved-views` and name their scope in the body:
one picker can then offer every view a person can open, wherever it points.
"""
from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy.orm import Session

from controllers import saved_view_controller as ctl
from db.session import get_db
from dependencies.workspace import WorkspaceAccess, as_guest, as_member

# Registers the tables on the metadata until the services behind these routes import them.
from models import saved_view as _saved_view  # noqa: F401
from schemas.errors import COMMON_ERRORS, VALIDATION_ERROR
from schemas.response import ErrorResponse, SuccessResponse
from schemas.responses.saved_views import SavedViewSummary
from schemas.saved_views import (
    MAX_REF,
    SavedViewCreate,
    SavedViewUpdate,
    ScopeKind,
    ViewType,
)

router = APIRouter(prefix="/workspaces/{workspace_id}", tags=["Views"])

NO_VIEW = {404: {"model": ErrorResponse,
                 "description": "No such workspace or view — or someone else's private view, "
                                "which is deliberately the same answer."}}
NOT_YOURS = {403: {"model": ErrorResponse,
                   "description": "A shared view you can see but may not change: only its "
                                  "owner edits it, and only its owner or an admin deletes it. "
                                  "Also: your role is below `member`."}}


@router.get(
    "/saved-views",
    response_model=SuccessResponse[list[SavedViewSummary]],
    responses={**COMMON_ERRORS, **VALIDATION_ERROR,
               404: {"model": ErrorResponse,
                     "description": "No such workspace, or you are not a member of it."}},
    summary="List the saved views you can open",
    operation_id="list_saved_views",
)
def list_views(access: WorkspaceAccess = Depends(as_guest), db: Session = Depends(get_db),
               scope_kind: ScopeKind | None = Query(None, description="Only views of this "
                                                                     "kind of scope."),
               scope_id: str | None = Query(None, max_length=MAX_REF,
                                            description="Only views of this list, space or "
                                                        "sprint."),
               view_type: ViewType | None = Query(None, description="Only board, list or "
                                                                   "backlog views.")):
    """Yours, and every view shared with the workspace, alphabetically."""
    return ctl.list_views(access, db, scope_kind=scope_kind, scope_id=scope_id,
                          view_type=view_type)


@router.post(
    "/saved-views",
    response_model=SuccessResponse[SavedViewSummary],
    status_code=201,
    responses={**COMMON_ERRORS,
               404: {"model": ErrorResponse,
                     "description": "No such workspace, or the scope names no list, space or "
                                    "sprint of it."},
               422: {"model": ErrorResponse,
                     "description": "A value did not validate — an unknown filter, too many "
                                    "assignees, a backlog view of something other than a "
                                    "space."}},
    summary="Save a view: a scope, a view type and filters",
    operation_id="create_saved_view",
)
def create_view(body: SavedViewCreate, access: WorkspaceAccess = Depends(as_member),
                db: Session = Depends(get_db)):
    """Private unless `visibility` is `workspace`. Members and above."""
    return ctl.create_view(access, body, db)


@router.get(
    "/saved-views/{view_id}",
    response_model=SuccessResponse[SavedViewSummary],
    responses={**COMMON_ERRORS, **NO_VIEW},
    summary="Get a saved view",
    operation_id="get_saved_view",
)
def get_view(view_id: str, access: WorkspaceAccess = Depends(as_guest),
             db: Session = Depends(get_db)):
    """One view you can see — what a link to a shared view opens."""
    return ctl.get_view(access, view_id, db)


@router.patch(
    "/saved-views/{view_id}",
    response_model=SuccessResponse[SavedViewSummary],
    responses={**COMMON_ERRORS, **NOT_YOURS,
               404: {"model": ErrorResponse,
                     "description": "No such workspace or view, someone else's private view, "
                                    "or a new scope that names nothing in this workspace."},
               422: {"model": ErrorResponse,
                     "description": "A value did not validate, `scope_kind` without "
                                    "`scope_id` (or the reverse), or a backlog view of "
                                    "something other than a space."}},
    summary="Change a saved view",
    operation_id="update_saved_view",
)
def update_view(view_id: str, body: SavedViewUpdate,
                access: WorkspaceAccess = Depends(as_member), db: Session = Depends(get_db)):
    """Only the fields present change. Its owner only."""
    return ctl.update_view(access, view_id, body, db)


@router.delete(
    "/saved-views/{view_id}",
    response_model=SuccessResponse[dict],
    responses={**COMMON_ERRORS, **NO_VIEW, **NOT_YOURS},
    summary="Delete a saved view",
    operation_id="delete_saved_view",
)
def delete_view(view_id: str, request: Request, access: WorkspaceAccess = Depends(as_member),
                db: Session = Depends(get_db)):
    """Its owner, or an admin when it is shared with the workspace (audited)."""
    return ctl.delete_view(access, view_id, db, request)
