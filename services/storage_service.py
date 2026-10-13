"""Service managing workspace storage settings and issue attachments.

Integrates with Model Context Protocol (MCP) Cloud Storage server to
support AWS S3, Azure Blob Storage, and local filesystem fallback.
"""
import base64
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from integrations.mcp.cloud_storage_server import default_mcp_storage_server
from models.issue import Issue
from models.storage import IssueAttachment
from models.user import User
from models.workspace import Workspace
from schemas.responses.issues import UserRef
from schemas.storage import (
    AttachmentSummary,
    StorageSettings,
    StorageSettingsUpdate,
    StorageTestRequest,
    StorageTestResponse,
)
from services import issue as issue_svc

SETTINGS_KEY = "storage"
IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".bmp"}


def _is_image(filename: str, content_type: str) -> bool:
    if content_type.lower().startswith("image/"):
        return True
    ext = Path(filename).suffix.lower()
    return ext in IMAGE_EXTENSIONS


def mask_secret(secret: str | None) -> str:
    if not secret:
        return ""
    if len(secret) <= 6:
        return "******"
    return f"{secret[:3]}...{secret[-3:]}"


def raw_settings_for(workspace: Workspace | None) -> dict[str, Any]:
    """Retrieve raw unmasked storage configuration dictionary."""
    if workspace is None:
        return {}
    document = workspace.settings or {}
    if not isinstance(document, dict):
        return {}
    storage_dict = document.get(SETTINGS_KEY)
    return dict(storage_dict) if isinstance(storage_dict, dict) else {}


def settings_for(workspace: Workspace | None) -> StorageSettings:
    """Retrieve client-safe storage settings with sensitive credentials masked."""
    raw = raw_settings_for(workspace)
    provider = raw.get("provider", "local")
    is_configured = False

    if provider == "aws_s3":
        is_configured = bool(
            raw.get("aws_bucket_name")
            and raw.get("aws_access_key_id")
            and raw.get("aws_secret_access_key")
        )
    elif provider == "azure_blob":
        is_configured = bool(
            raw.get("azure_account_name")
            and raw.get("azure_account_key")
            and raw.get("azure_container_name")
        )
    else:
        is_configured = True

    return StorageSettings(
        provider=provider,
        aws_access_key_id=mask_secret(raw.get("aws_access_key_id")),
        aws_secret_access_key=mask_secret(raw.get("aws_secret_access_key")),
        aws_bucket_name=raw.get("aws_bucket_name", ""),
        aws_region=raw.get("aws_region", "us-east-1"),
        aws_endpoint_url=raw.get("aws_endpoint_url"),
        azure_account_name=raw.get("azure_account_name", ""),
        azure_account_key=mask_secret(raw.get("azure_account_key")),
        azure_container_name=raw.get("azure_container_name", ""),
        fallback_to_local=raw.get("fallback_to_local", True),
        is_configured=is_configured,
    )


def save_settings(
    workspace: Workspace, update: StorageSettingsUpdate, db: Session
) -> StorageSettings:
    """Save updated storage configuration, preserving unchanged secrets."""
    document = dict(workspace.settings or {}) if isinstance(workspace.settings, dict) else {}
    current_raw = dict(document.get(SETTINGS_KEY) or {})

    changes = {field: getattr(update, field) for field in update.model_fields_set}

    # Don't overwrite existing secrets if caller sent empty or masked values
    for secret_field in ("aws_secret_access_key", "azure_account_key"):
        if secret_field in changes:
            val = changes[secret_field]
            if not val or "..." in val or "***" in val:
                changes.pop(secret_field, None)

    current_raw.update(changes)
    document[SETTINGS_KEY] = current_raw
    workspace.settings = document
    db.commit()
    return settings_for(workspace)


def test_connection(
    workspace: Workspace | None, request: StorageTestRequest
) -> StorageTestResponse:
    """Test cloud connection with provided credentials or saved credentials."""
    raw = raw_settings_for(workspace)
    config = dict(raw)

    # Apply overrides from test request
    for field in (
        "aws_access_key_id",
        "aws_secret_access_key",
        "aws_bucket_name",
        "aws_region",
        "aws_endpoint_url",
        "azure_account_name",
        "azure_account_key",
        "azure_container_name",
    ):
        override_val = getattr(request, field, None)
        if override_val and not ("..." in override_val or "***" in override_val):
            config[field] = override_val

    result = default_mcp_storage_server.call_tool(
        "storage_test_connection", {"provider": request.provider, "config": config}
    )
    return StorageTestResponse(
        success=bool(result.get("success", False)),
        message=str(result.get("message", "")),
        provider=str(result.get("provider", request.provider)),
    )


def upload_attachment(
    db: Session,
    workspace: Workspace,
    issue: Issue,
    uploader_id: str,
    filename: str,
    content: bytes,
    content_type: str = "application/octet-stream",
    comment_id: str | None = None,
) -> IssueAttachment:
    """Upload attachment via MCP storage server and save record."""
    raw_config = raw_settings_for(workspace)
    active_provider = raw_config.get("provider", "local")

    content_base64 = base64.b64encode(content).decode("utf-8")
    mcp_res = default_mcp_storage_server.call_tool(
        "storage_upload_file",
        {
            "workspace_id": workspace.id,
            "issue_id": issue.id,
            "filename": filename,
            "content_base64": content_base64,
            "content_type": content_type,
            "provider": active_provider,
            "config": raw_config,
        },
    )

    is_img = _is_image(filename, content_type)
    attachment = IssueAttachment(
        id=str(uuid.uuid4()),
        workspace_id=workspace.id,
        issue_id=issue.id,
        comment_id=comment_id,
        uploader_id=uploader_id,
        filename=filename,
        stored_filename=mcp_res.get("stored_filename", filename),
        file_size=mcp_res.get("file_size", len(content)),
        content_type=content_type,
        storage_provider=mcp_res.get("storage_provider", "local"),
        storage_path=mcp_res.get("storage_path", ""),
        is_image=is_img,
    )
    db.add(attachment)

    issue_svc.record_event(
        db,
        issue,
        "attachment_added",
        uploader_id,
        to_value=filename[:255],
    )
    db.commit()
    db.refresh(attachment)
    return attachment


def list_attachments(
    db: Session,
    workspace_id: str,
    issue_id: str,
    comment_id: str | None = None,
) -> list[IssueAttachment]:
    query = db.query(IssueAttachment).filter(
        IssueAttachment.workspace_id == workspace_id,
        IssueAttachment.issue_id == issue_id,
        IssueAttachment.deleted == 0,
    )
    if comment_id is not None:
        query = query.filter(IssueAttachment.comment_id == comment_id)
    return query.order_by(IssueAttachment.created_at.asc()).all()


def get_attachment(
    db: Session, workspace_id: str, attachment_id: str
) -> IssueAttachment | None:
    return (
        db.query(IssueAttachment)
        .filter(
            IssueAttachment.workspace_id == workspace_id,
            IssueAttachment.id == attachment_id,
            IssueAttachment.deleted == 0,
        )
        .first()
    )


def read_attachment_bytes(workspace: Workspace, attachment: IssueAttachment) -> bytes:
    raw_config = raw_settings_for(workspace)
    res = default_mcp_storage_server.call_tool(
        "storage_download_file",
        {
            "storage_path": attachment.storage_path,
            "provider": attachment.storage_provider,
            "config": raw_config,
        },
    )
    return res.get("content_bytes") or b""


def delete_attachment(
    db: Session,
    workspace: Workspace,
    issue: Issue,
    attachment: IssueAttachment,
    user_id: str,
) -> None:
    raw_config = raw_settings_for(workspace)
    default_mcp_storage_server.call_tool(
        "storage_delete_file",
        {
            "storage_path": attachment.storage_path,
            "provider": attachment.storage_provider,
            "config": raw_config,
        },
    )
    attachment.deleted = 1
    issue_svc.record_event(
        db,
        issue,
        "attachment_deleted",
        user_id,
        from_value=attachment.filename[:255],
    )
    db.commit()


def attachment_summary(db: Session, attachment: IssueAttachment) -> AttachmentSummary:
    user = db.query(User).filter(User.id == attachment.uploader_id).first()
    uploader_name = (
        f"{user.first_name} {user.last_name}".strip()
        if user and (user.first_name or user.last_name)
        else (user.email if user else "Unknown")
    )
    uploader = UserRef(id=attachment.uploader_id, display_name=uploader_name)
    download_url = (
        f"/api/workspaces/{attachment.workspace_id}/issues/{attachment.issue_id}"
        f"/attachments/{attachment.id}/download"
    )
    return AttachmentSummary(
        id=attachment.id,
        workspace_id=attachment.workspace_id,
        issue_id=attachment.issue_id,
        comment_id=attachment.comment_id,
        uploader=uploader,
        filename=attachment.filename,
        file_size=attachment.file_size,
        content_type=attachment.content_type,
        storage_provider=attachment.storage_provider,
        is_image=attachment.is_image,
        created_at=attachment.created_at.isoformat()
        if isinstance(attachment.created_at, datetime)
        else str(attachment.created_at),
        download_url=download_url,
    )
