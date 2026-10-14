"""Routes for cross-space portfolio initiatives (Epics 2.0)."""
from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from controllers import initiatives_controller as controller
from db.session import get_db
from dependencies.workspace import WorkspaceAccess, as_member
from routes.workspaces import NOT_A_MEMBER
from schemas.errors import COMMON_ERRORS
from schemas.initiatives import (
    InitiativeCreate,
    InitiativeDetailResponse,
    InitiativeResponse,
    InitiativeUpdate,
    LinkIssuesRequest,
    LinkSpacesRequest,
)
from schemas.response import ErrorResponse, SuccessResponse

router = APIRouter(prefix="/workspaces/{workspace_id}/initiatives", tags=["Initiatives"])


@router.get(
    "",
    response_model=SuccessResponse[list[InitiativeResponse]],
    responses={**COMMON_ERRORS, **NOT_A_MEMBER},
    summary="List portfolio initiatives in workspace",
    operation_id="list_initiatives",
)
def list_initiatives(
    access: WorkspaceAccess = Depends(as_member),
    db: Session = Depends(get_db),
):
    """Returns all non-deleted portfolio initiatives in the workspace with progress rollups."""
    return controller.list_initiatives(access=access, db=db)


@router.post(
    "",
    response_model=SuccessResponse[InitiativeDetailResponse],
    status_code=201,
    responses={
        **COMMON_ERRORS,
        **NOT_A_MEMBER,
        400: {"model": ErrorResponse, "description": "Duplicate key or validation failure"},
    },
    summary="Create a new portfolio initiative",
    operation_id="create_initiative",
)
def create_initiative(
    body: InitiativeCreate,
    access: WorkspaceAccess = Depends(as_member),
    db: Session = Depends(get_db),
):
    """Creates a workspace-level portfolio initiative uniting spaces and epics."""
    return controller.create_initiative(access=access, body=body, db=db)


@router.get(
    "/{initiative_id}",
    response_model=SuccessResponse[InitiativeDetailResponse],
    responses={**COMMON_ERRORS, **NOT_A_MEMBER, 404: {"model": ErrorResponse, "description": "Initiative not found"}},
    summary="Get initiative details, contributing spaces, and linked issues",
    operation_id="get_initiative",
)
def get_initiative(
    initiative_id: str,
    access: WorkspaceAccess = Depends(as_member),
    db: Session = Depends(get_db),
):
    """Returns initiative details including progress rollup, budget burn, and linked issues."""
    return controller.get_initiative(access=access, initiative_id=initiative_id, db=db)


@router.patch(
    "/{initiative_id}",
    response_model=SuccessResponse[InitiativeDetailResponse],
    responses={**COMMON_ERRORS, **NOT_A_MEMBER, 404: {"model": ErrorResponse, "description": "Initiative not found"}},
    summary="Update portfolio initiative attributes",
    operation_id="update_initiative",
)
def update_initiative(
    initiative_id: str,
    body: InitiativeUpdate,
    access: WorkspaceAccess = Depends(as_member),
    db: Session = Depends(get_db),
):
    """Updates status, title, description, budget, or target dates for an initiative."""
    return controller.update_initiative(access=access, initiative_id=initiative_id, body=body, db=db)


@router.delete(
    "/{initiative_id}",
    response_model=SuccessResponse[None],
    responses={**COMMON_ERRORS, **NOT_A_MEMBER, 404: {"model": ErrorResponse, "description": "Initiative not found"}},
    summary="Soft-delete a portfolio initiative",
    operation_id="delete_initiative",
)
def delete_initiative(
    initiative_id: str,
    access: WorkspaceAccess = Depends(as_member),
    db: Session = Depends(get_db),
):
    """Soft-deletes a portfolio initiative without deleting underlying spaces or issues."""
    return controller.delete_initiative(access=access, initiative_id=initiative_id, db=db)


@router.post(
    "/{initiative_id}/spaces",
    response_model=SuccessResponse[InitiativeDetailResponse],
    responses={**COMMON_ERRORS, **NOT_A_MEMBER, 404: {"model": ErrorResponse, "description": "Initiative not found"}},
    summary="Link contributing spaces to an initiative",
    operation_id="link_initiative_spaces",
)
def link_spaces(
    initiative_id: str,
    body: LinkSpacesRequest,
    access: WorkspaceAccess = Depends(as_member),
    db: Session = Depends(get_db),
):
    """Links contributing spaces to the initiative."""
    return controller.link_spaces(access=access, initiative_id=initiative_id, body=body, db=db)


@router.delete(
    "/{initiative_id}/spaces/{space_id}",
    response_model=SuccessResponse[InitiativeDetailResponse],
    responses={**COMMON_ERRORS, **NOT_A_MEMBER, 404: {"model": ErrorResponse, "description": "Initiative not found"}},
    summary="Unlink a contributing space from an initiative",
    operation_id="unlink_initiative_space",
)
def unlink_space(
    initiative_id: str,
    space_id: str,
    access: WorkspaceAccess = Depends(as_member),
    db: Session = Depends(get_db),
):
    """Removes a contributing space link from the initiative."""
    return controller.unlink_space(access=access, initiative_id=initiative_id, space_id=space_id, db=db)


@router.post(
    "/{initiative_id}/issues",
    response_model=SuccessResponse[InitiativeDetailResponse],
    responses={**COMMON_ERRORS, **NOT_A_MEMBER, 404: {"model": ErrorResponse, "description": "Initiative not found"}},
    summary="Link epics or issues to an initiative",
    operation_id="link_initiative_issues",
)
def link_issues(
    initiative_id: str,
    body: LinkIssuesRequest,
    access: WorkspaceAccess = Depends(as_member),
    db: Session = Depends(get_db),
):
    """Links issues or epics across spaces to the initiative."""
    return controller.link_issues(access=access, initiative_id=initiative_id, body=body, db=db)


@router.delete(
    "/{initiative_id}/issues/{issue_id}",
    response_model=SuccessResponse[InitiativeDetailResponse],
    responses={**COMMON_ERRORS, **NOT_A_MEMBER, 404: {"model": ErrorResponse, "description": "Initiative not found"}},
    summary="Unlink an issue from an initiative",
    operation_id="unlink_initiative_issue",
)
def unlink_issue(
    initiative_id: str,
    issue_id: str,
    access: WorkspaceAccess = Depends(as_member),
    db: Session = Depends(get_db),
):
    """Removes an issue link from the initiative."""
    return controller.unlink_issue(access=access, initiative_id=initiative_id, issue_id=issue_id, db=db)
