"""Workspace storage settings and issue attachment routes."""
from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from controllers import storage_controller as ctl
from db.session import get_db
from dependencies.workspace import WorkspaceAccess, as_admin, as_guest, as_member
from models import storage as _storage  # noqa: F401
from schemas.errors import COMMON_ERRORS, VALIDATION_ERROR
from schemas.response import ErrorResponse, SuccessResponse
from schemas.storage import (
    AttachmentSummary,
    StorageSettings,
    StorageSettingsUpdate,
    StorageTestRequest,
    StorageTestResponse,
)

LOCKED = {
    423: {
        "model": ErrorResponse,
        "description": "The space is frozen or the issue container is archived.",
    }
}

router = APIRouter(prefix="/workspaces/{workspace_id}", tags=["Storage"])


@router.get(
    "/settings/storage",
    response_model=SuccessResponse[StorageSettings],
    responses={**COMMON_ERRORS},
    summary="Get workspace storage settings",
    operation_id="get_workspace_storage_settings",
)
def get_settings(
    access: WorkspaceAccess = Depends(as_guest), db: Session = Depends(get_db)
):
    return ctl.get_settings(access, db)


@router.patch(
    "/settings/storage",
    response_model=SuccessResponse[StorageSettings],
    responses={**COMMON_ERRORS, **VALIDATION_ERROR},
    summary="Update workspace storage settings",
    operation_id="update_workspace_storage_settings",
)
def update_settings(
    body: StorageSettingsUpdate,
    request: Request,
    access: WorkspaceAccess = Depends(as_admin),
    db: Session = Depends(get_db),
):
    return ctl.update_settings(access, body, db, request)


@router.put(
    "/settings/storage",
    response_model=SuccessResponse[StorageSettings],
    responses={**COMMON_ERRORS, **VALIDATION_ERROR},
    summary="Replace workspace storage settings",
    operation_id="replace_workspace_storage_settings",
)
def replace_settings(
    body: StorageSettingsUpdate,
    request: Request,
    access: WorkspaceAccess = Depends(as_admin),
    db: Session = Depends(get_db),
):
    return ctl.update_settings(access, body, db, request)


@router.post(
    "/settings/storage/test",
    response_model=SuccessResponse[StorageTestResponse],
    responses={**COMMON_ERRORS, **VALIDATION_ERROR},
    summary="Test cloud storage connection",
    operation_id="test_workspace_storage_connection",
)
def test_connection(
    body: StorageTestRequest,
    access: WorkspaceAccess = Depends(as_admin),
    db: Session = Depends(get_db),
):
    return ctl.test_connection(access, body, db)


@router.get(
    "/issues/{issue_id}/attachments",
    response_model=SuccessResponse[list[AttachmentSummary]],
    responses={
        **COMMON_ERRORS,
        404: {"model": ErrorResponse, "description": "Issue not found"},
    },
    summary="List issue attachments",
    operation_id="list_issue_attachments",
)
def list_attachments(
    issue_id: str,
    comment_id: str | None = None,
    access: WorkspaceAccess = Depends(as_guest),
    db: Session = Depends(get_db),
):
    return ctl.list_attachments(access, issue_id, db, comment_id=comment_id)


@router.post(
    "/issues/{issue_id}/attachments",
    response_model=SuccessResponse[AttachmentSummary],
    status_code=201,
    responses={
        **COMMON_ERRORS,
        **VALIDATION_ERROR,
        **LOCKED,
        404: {"model": ErrorResponse, "description": "Issue not found"},
    },
    summary="Upload an attachment to an issue",
    operation_id="upload_issue_attachment",
)
async def upload_attachment(
    issue_id: str,
    request: Request,
    comment_id: str | None = None,
    access: WorkspaceAccess = Depends(as_member),
    db: Session = Depends(get_db),
):
    return await ctl.upload_attachment(
        access, issue_id, request, db, comment_id=comment_id
    )


@router.get(
    "/issues/{issue_id}/attachments/{attachment_id}/download",
    responses={
        **COMMON_ERRORS,
        404: {"model": ErrorResponse, "description": "Issue or attachment not found"},
    },
    summary="Download issue attachment",
    operation_id="download_issue_attachment",
)
def download_attachment(
    issue_id: str,
    attachment_id: str,
    access: WorkspaceAccess = Depends(as_guest),
    db: Session = Depends(get_db),
):
    return ctl.download_attachment(access, issue_id, attachment_id, db)


@router.delete(
    "/issues/{issue_id}/attachments/{attachment_id}",
    response_model=SuccessResponse[dict],
    responses={
        **COMMON_ERRORS,
        **LOCKED,
        404: {"model": ErrorResponse, "description": "Issue or attachment not found"},
    },
    summary="Delete issue attachment",
    operation_id="delete_issue_attachment",
)
def delete_attachment(
    issue_id: str,
    attachment_id: str,
    access: WorkspaceAccess = Depends(as_member),
    db: Session = Depends(get_db),
):
    return ctl.delete_attachment(access, issue_id, attachment_id, db)
