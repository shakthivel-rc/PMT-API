import asyncio
import base64
import email
import json
from urllib.parse import quote

from fastapi import Request, Response
from sqlalchemy.orm import Session

from dependencies.workspace import WorkspaceAccess
from models.workspace import Workspace
from schemas.response import SuccessResponse, error_json
from schemas.storage import (
    StorageSettingsUpdate,
    StorageTestRequest,
)
from services import issue as issue_svc
from services import storage_service as storage_svc
from services import write_guard

_NOT_FOUND = "Issue not found"
_ATTACHMENT_NOT_FOUND = "Attachment not found"


def _workspace(db: Session, workspace_id: str) -> Workspace:
    return db.query(Workspace).filter(Workspace.id == workspace_id).first()


def get_settings(access: WorkspaceAccess, db: Session):
    ws = _workspace(db, access.workspace_id)
    settings = storage_svc.settings_for(ws)
    return SuccessResponse(
        status_code=200, message="Storage settings fetched", data=settings.model_dump()
    )


def update_settings(
    access: WorkspaceAccess, body: StorageSettingsUpdate, db: Session, request: Request
):
    ws = _workspace(db, access.workspace_id)
    if ws is None:
        return error_json(404, "Workspace not found")
    updated = storage_svc.save_settings(ws, body, db)
    return SuccessResponse(
        status_code=200, message="Storage settings updated", data=updated.model_dump()
    )


def test_connection(access: WorkspaceAccess, body: StorageTestRequest, db: Session):
    ws = _workspace(db, access.workspace_id)
    res = storage_svc.test_connection(ws, body)
    return SuccessResponse(
        status_code=200, message="Connection test completed", data=res.model_dump()
    )


def list_attachments(
    access: WorkspaceAccess,
    issue_id: str,
    db: Session,
    comment_id: str | None = None,
):
    issue = issue_svc.get_issue(db, access.workspace_id, issue_id)
    if issue is None:
        return error_json(404, _NOT_FOUND)
    items = storage_svc.list_attachments(
        db, access.workspace_id, issue_id, comment_id=comment_id
    )
    summaries = [storage_svc.attachment_summary(db, a).model_dump() for a in items]
    return SuccessResponse(status_code=200, message="Attachments fetched", data=summaries)


async def upload_attachment(
    access: WorkspaceAccess,
    issue_id: str,
    request: Request,
    db: Session,
    comment_id: str | None = None,
):
    issue = issue_svc.get_issue(db, access.workspace_id, issue_id)
    if issue is None:
        return error_json(404, _NOT_FOUND)

    block = write_guard.issue_block(db, issue)
    if block is not None:
        return block.response()

    if not comment_id:
        comment_id = request.query_params.get("comment_id")

    raw_body = await request.body()
    content_type_hdr = request.headers.get("content-type", "")

    filename = ""
    file_bytes = b""
    content_type = "application/octet-stream"

    if "application/json" in content_type_hdr.lower():
        try:
            parsed = json.loads(raw_body)
            filename = parsed.get("filename", "upload.bin")
            content_type = parsed.get("content_type", "application/octet-stream")
            b64_data = parsed.get("content_base64", "")
            file_bytes = base64.b64decode(b64_data)
            if not comment_id:
                comment_id = parsed.get("comment_id")
        except Exception as exc:
            return error_json(422, f"Invalid JSON base64 upload payload: {exc}")
    elif "multipart/form-data" in content_type_hdr.lower():
        try:
            header_bytes = f"Content-Type: {content_type_hdr}\r\n\r\n".encode()
            msg = email.message_from_bytes(header_bytes + raw_body)
            for part in msg.walk():
                part_filename = part.get_filename()
                if part_filename:
                    filename = part_filename
                    content_type = part.get_content_type() or "application/octet-stream"
                    file_bytes = part.get_payload(decode=True) or b""
                    break
        except Exception as exc:
            return error_json(422, f"Failed to parse multipart upload: {exc}")

    if not filename or not file_bytes:
        return error_json(422, "No valid file data received in upload request.")

    def _do_upload():
        if comment_id:
            from models.issue import IssueComment

            comment = (
                db.query(IssueComment)
                .filter(
                    IssueComment.id == comment_id,
                    IssueComment.issue_id == issue.id,
                    IssueComment.workspace_id == access.workspace_id,
                    IssueComment.deleted == 0,
                )
                .first()
            )
            if comment is None:
                return error_json(404, "Comment not found")

        ws = _workspace(db, access.workspace_id)
        attachment = storage_svc.upload_attachment(
            db=db,
            workspace=ws,
            issue=issue,
            uploader_id=access.user_id,
            filename=filename,
            content=file_bytes,
            content_type=content_type,
            comment_id=comment_id,
        )

        summary = storage_svc.attachment_summary(db, attachment)
        return SuccessResponse(
            status_code=201, message="Attachment uploaded", data=summary.model_dump()
        )

    return await asyncio.to_thread(_do_upload)


def download_attachment(
    access: WorkspaceAccess, issue_id: str, attachment_id: str, db: Session
):
    issue = issue_svc.get_issue(db, access.workspace_id, issue_id)
    if issue is None:
        return error_json(404, _NOT_FOUND)

    attachment = storage_svc.get_attachment(db, access.workspace_id, attachment_id)
    if attachment is None:
        return error_json(404, _ATTACHMENT_NOT_FOUND)

    ws = _workspace(db, access.workspace_id)
    file_bytes = storage_svc.read_attachment_bytes(ws, attachment)

    safe_filename = quote(attachment.filename)
    is_previewable = (
        attachment.is_image
        or attachment.content_type == "application/pdf"
        or attachment.filename.lower().endswith(".pdf")
    )
    disposition = (
        f'inline; filename="{safe_filename}"'
        if is_previewable
        else f'attachment; filename="{safe_filename}"'
    )
    headers = {
        "Content-Disposition": disposition,
        "Content-Length": str(len(file_bytes)),
    }
    if is_previewable:
        headers["X-Frame-Options"] = "SAMEORIGIN"
        headers["Content-Security-Policy"] = (
            "default-src 'self' blob: data:; style-src 'unsafe-inline'; object-src 'self' blob:;"
        )
    return Response(
        content=file_bytes,
        media_type=attachment.content_type,
        headers=headers,
    )


def delete_attachment(
    access: WorkspaceAccess, issue_id: str, attachment_id: str, db: Session
):
    issue = issue_svc.get_issue(db, access.workspace_id, issue_id)
    if issue is None:
        return error_json(404, _NOT_FOUND)

    block = write_guard.issue_block(db, issue)
    if block is not None:
        return block.response()

    attachment = storage_svc.get_attachment(db, access.workspace_id, attachment_id)
    if attachment is None:
        return error_json(404, _ATTACHMENT_NOT_FOUND)

    ws = _workspace(db, access.workspace_id)
    storage_svc.delete_attachment(db, ws, issue, attachment, access.user_id)
    return SuccessResponse(
        status_code=200, message="Attachment deleted", data={"deleted": True}
    )
