"""Routes for CI/CD deployments and issue deployment markers."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from controllers import deployments_controller as controller
from db.session import get_db
from dependencies.workspace import WorkspaceAccess, as_member
from routes.workspaces import NOT_A_MEMBER
from schemas.deployments import DeploymentCreate, DeploymentResponse, IssueDeploymentSummary
from schemas.errors import COMMON_ERRORS
from schemas.response import ErrorResponse, SuccessResponse

router = APIRouter(prefix="/workspaces/{workspace_id}/deployments", tags=["Deployments"])
issue_deployments_router = APIRouter(prefix="/workspaces/{workspace_id}/issues", tags=["Deployments"])


@router.get(
    "",
    response_model=SuccessResponse[list[DeploymentResponse]],
    responses={**COMMON_ERRORS, **NOT_A_MEMBER},
    summary="List deployments in workspace",
    operation_id="list_deployments",
)
def list_deployments(
    environment: str | None = Query(None, description="Filter by environment (development, staging, production, canary)"),
    limit: int = Query(50, ge=1, le=100, description="Max deployments to return"),
    access: WorkspaceAccess = Depends(as_member),
    db: Session = Depends(get_db),
):
    """Returns past and active deployments in the workspace."""
    return controller.list_deployments(access=access, environment=environment, limit=limit, db=db)


@router.post(
    "",
    response_model=SuccessResponse[DeploymentResponse],
    status_code=201,
    responses={
        **COMMON_ERRORS,
        **NOT_A_MEMBER,
        400: {"model": ErrorResponse, "description": "Validation error"},
    },
    summary="Register a CI/CD deployment",
    operation_id="create_deployment",
)
def create_deployment(
    body: DeploymentCreate,
    access: WorkspaceAccess = Depends(as_member),
    db: Session = Depends(get_db),
):
    """Registers a deployment event and automatically links shipped issue keys."""
    return controller.create_deployment(access=access, body=body, db=db)


@issue_deployments_router.get(
    "/{issue_id}/deployments",
    response_model=SuccessResponse[list[IssueDeploymentSummary]],
    responses={**COMMON_ERRORS, **NOT_A_MEMBER},
    summary="Get deployment history and environment markers for an issue",
    operation_id="get_issue_deployments",
)
def get_issue_deployments(
    issue_id: str,
    access: WorkspaceAccess = Depends(as_member),
    db: Session = Depends(get_db),
):
    """Returns all deployments that include this issue, ordered by deployment time."""
    return controller.get_issue_deployments(access=access, issue_id=issue_id, db=db)
