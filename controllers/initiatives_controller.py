"""Controller for cross-space portfolio initiatives."""
from __future__ import annotations

from sqlalchemy.orm import Session

from dependencies.workspace import WorkspaceAccess
from schemas.initiatives import (
    InitiativeCreate,
    InitiativeUpdate,
    LinkIssuesRequest,
    LinkSpacesRequest,
)
from schemas.response import SuccessResponse, error_json
from services import initiatives as svc


def list_initiatives(access: WorkspaceAccess, db: Session):
    items = svc.list_initiatives(db, access.workspace_id)
    return SuccessResponse(status_code=200, message="Initiatives retrieved", data=items)


def create_initiative(access: WorkspaceAccess, body: InitiativeCreate, db: Session):
    try:
        data = svc.create_initiative(db, access.workspace_id, body, creator_id=access.user_id)
        return SuccessResponse(status_code=201, message="Initiative created", data=data)
    except ValueError as e:
        return error_json(400, str(e))


def get_initiative(access: WorkspaceAccess, initiative_id: str, db: Session):
    try:
        data = svc.get_initiative(db, access.workspace_id, initiative_id)
        return SuccessResponse(status_code=200, message="Initiative retrieved", data=data)
    except ValueError as e:
        return error_json(404, str(e))


def update_initiative(access: WorkspaceAccess, initiative_id: str, body: InitiativeUpdate, db: Session):
    try:
        data = svc.update_initiative(db, access.workspace_id, initiative_id, body)
        return SuccessResponse(status_code=200, message="Initiative updated", data=data)
    except ValueError as e:
        return error_json(404, str(e))


def delete_initiative(access: WorkspaceAccess, initiative_id: str, db: Session):
    try:
        svc.delete_initiative(db, access.workspace_id, initiative_id)
        return SuccessResponse(status_code=200, message="Initiative deleted", data=None)
    except ValueError as e:
        return error_json(404, str(e))


def link_spaces(access: WorkspaceAccess, initiative_id: str, body: LinkSpacesRequest, db: Session):
    try:
        data = svc.link_spaces(db, access.workspace_id, initiative_id, body.space_ids)
        return SuccessResponse(status_code=200, message="Spaces linked", data=data)
    except ValueError as e:
        return error_json(404, str(e))


def unlink_space(access: WorkspaceAccess, initiative_id: str, space_id: str, db: Session):
    try:
        data = svc.unlink_space(db, access.workspace_id, initiative_id, space_id)
        return SuccessResponse(status_code=200, message="Space unlinked", data=data)
    except ValueError as e:
        return error_json(404, str(e))


def link_issues(access: WorkspaceAccess, initiative_id: str, body: LinkIssuesRequest, db: Session):
    try:
        data = svc.link_issues(db, access.workspace_id, initiative_id, body.issue_ids)
        return SuccessResponse(status_code=200, message="Issues linked", data=data)
    except ValueError as e:
        return error_json(404, str(e))


def unlink_issue(access: WorkspaceAccess, initiative_id: str, issue_id: str, db: Session):
    try:
        data = svc.unlink_issue(db, access.workspace_id, initiative_id, issue_id)
        return SuccessResponse(status_code=200, message="Issue unlinked", data=data)
    except ValueError as e:
        return error_json(404, str(e))
