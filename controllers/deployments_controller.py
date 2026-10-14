"""Controller for CI/CD deployments and issue deployment markers."""
from __future__ import annotations

from sqlalchemy.orm import Session

from dependencies.workspace import WorkspaceAccess
from schemas.deployments import DeploymentCreate
from schemas.response import SuccessResponse, error_json
from services import deployments as svc


def list_deployments(access: WorkspaceAccess, environment: str | None, limit: int, db: Session):
    items = svc.list_deployments(db, access.workspace_id, environment=environment, limit=limit)
    return SuccessResponse(status_code=200, message="Deployments retrieved", data=items)


def create_deployment(access: WorkspaceAccess, body: DeploymentCreate, db: Session):
    try:
        data = svc.create_deployment(db, access.workspace_id, body)
        return SuccessResponse(status_code=201, message="Deployment registered", data=data)
    except Exception as e:
        return error_json(400, str(e))


def get_issue_deployments(access: WorkspaceAccess, issue_id: str, db: Session):
    items = svc.get_issue_deployments(db, access.workspace_id, issue_id)
    return SuccessResponse(status_code=200, message="Issue deployments retrieved", data=items)
