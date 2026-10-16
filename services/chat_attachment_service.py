"""Service managing chat attachments (images, documents, logs, sheets) for AI Chat."""
from __future__ import annotations

import base64
import io
import uuid
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy.orm import Session

from integrations.mcp.cloud_storage_server import default_mcp_storage_server
from models.chat_session import ChatMessageAttachment
from models.workspace import Workspace
from schemas.chat import ChatAttachmentSummary
from services import storage_service as storage_svc

ALLOWED_IMAGE_MIMES = {
    "image/png": {".png"},
    "image/jpeg": {".jpg", ".jpeg"},
    "image/webp": {".webp"},
    "image/gif": {".gif"},
    "image/svg+xml": {".svg"},
}

ALLOWED_DOC_MIMES = {
    "application/pdf": {".pdf"},
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": {".docx"},
    "text/plain": {".txt", ".log"},
    "text/markdown": {".md", ".markdown"},
    "text/csv": {".csv"},
    "application/json": {".json"},
}

MAX_IMAGE_SIZE_BYTES = 15 * 1024 * 1024  # 15 MB
MAX_DOC_SIZE_BYTES = 30 * 1024 * 1024    # 30 MB

MAGIC_SIGNATURES: list[tuple[bytes, str]] = [
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"GIF87a", "image/gif"),
    (b"GIF89a", "image/gif"),
    (b"%PDF-", "application/pdf"),
    (b"PK\x03\x04", "application/vnd.openxmlformats-officedocument.wordprocessingml.document"),
]


def _now() -> datetime:
    return datetime.now(UTC)


def validate_and_normalize_mime(filename: str, content: bytes, declared_mime: str) -> str:
    """Validate declared MIME against file extension and magic signature."""
    ext = Path(filename).suffix.lower()

    # Detect via magic signature
    detected_mime: str | None = None
    for sig, sig_mime in MAGIC_SIGNATURES:
        if content.startswith(sig):
            detected_mime = sig_mime
            break

    # Check WEBP signature: RIFF....WEBP
    if len(content) >= 12 and content[:4] == b"RIFF" and content[8:12] == b"WEBP":
        detected_mime = "image/webp"

    # SVG detection
    if ext == ".svg" or "svg" in declared_mime.lower():
        sample = content[:500].decode("utf-8", errors="ignore").lower()
        if "<svg" in sample or "<?xml" in sample:
            detected_mime = "image/svg+xml"

    # Pure text detection
    if detected_mime is None and ext in {".txt", ".log", ".md", ".markdown", ".csv", ".json"}:
        try:
            content[:1024].decode("utf-8")
            if ext == ".csv":
                detected_mime = "text/csv"
            elif ext == ".json":
                detected_mime = "application/json"
            elif ext in {".md", ".markdown"}:
                detected_mime = "text/markdown"
            else:
                detected_mime = "text/plain"
        except UnicodeDecodeError:
            pass

    resolved_mime = detected_mime or declared_mime.lower().split(";")[0].strip()

    # Verify against allowed sets
    if resolved_mime in ALLOWED_IMAGE_MIMES:
        if len(content) > MAX_IMAGE_SIZE_BYTES:
            max_mb = MAX_IMAGE_SIZE_BYTES // (1024 * 1024)
            raise ValueError(f"Image exceeds maximum size of {max_mb}MB")
        return resolved_mime

    if resolved_mime in ALLOWED_DOC_MIMES:
        if len(content) > MAX_DOC_SIZE_BYTES:
            max_mb = MAX_DOC_SIZE_BYTES // (1024 * 1024)
            raise ValueError(f"Document exceeds maximum size of {max_mb}MB")
        return resolved_mime

    # Check fallback by extension
    for mime, exts in ALLOWED_IMAGE_MIMES.items():
        if ext in exts:
            if len(content) > MAX_IMAGE_SIZE_BYTES:
                raise ValueError("Image exceeds maximum size of 15MB")
            return mime

    for mime, exts in ALLOWED_DOC_MIMES.items():
        if ext in exts:
            if len(content) > MAX_DOC_SIZE_BYTES:
                raise ValueError("Document exceeds maximum size of 30MB")
            return mime

    raise ValueError(f"Unsupported file type: {filename} ({resolved_mime})")


def extract_text_from_content(filename: str, content: bytes, mime_type: str) -> str | None:
    """Extract plain text or structured excerpt from uploaded document."""
    ext = Path(filename).suffix.lower()

    if mime_type in {"text/plain", "text/markdown", "text/csv", "application/json"} or ext in {
        ".txt", ".log", ".md", ".csv", ".json"
    }:
        try:
            text = content.decode("utf-8", errors="replace")
            return text[:200000]  # Store up to 200k characters
        except Exception:
            return None

    if mime_type == "application/pdf" or ext == ".pdf":
        try:
            import pypdf
            reader = pypdf.PdfReader(io.BytesIO(content))
            pages_text: list[str] = []
            for page in reader.pages[:50]:  # Up to 50 pages
                extracted = page.extract_text()
                if extracted:
                    pages_text.append(extracted)
            if pages_text:
                full_text = "\n\n".join(pages_text)
                return full_text[:200000]
        except Exception:
            return None

    return None


def upload_chat_attachment(
    db: Session,
    workspace: Workspace,
    user_id: str,
    filename: str,
    content: bytes,
    declared_mime: str,
    conversation_id: str | None = None,
) -> ChatMessageAttachment:
    """Validate, persist, and record a chat message attachment."""
    mime_type = validate_and_normalize_mime(filename, content, declared_mime)
    extracted_text = extract_text_from_content(filename, content, mime_type)

    raw_config = storage_svc.raw_settings_for(workspace)
    active_provider = raw_config.get("provider", "local")

    content_b64 = base64.b64encode(content).decode("ascii")
    mcp_res = default_mcp_storage_server.call_tool(
        "storage_upload_file",
        {
            "workspace_id": workspace.id,
            "filename": filename,
            "content_base64": content_b64,
            "content_type": mime_type,
            "provider": active_provider,
            "config": raw_config,
        },
    )

    attachment = ChatMessageAttachment(
        id=str(uuid.uuid4()),
        conversation_id=conversation_id,
        message_id=None,
        workspace_id=workspace.id,
        user_id=user_id,
        original_filename=filename,
        stored_path=mcp_res.get("storage_path", ""),
        storage_provider=mcp_res.get("storage_provider", active_provider),
        mime_type=mime_type,
        file_size_bytes=len(content),
        extracted_text=extracted_text,
        created_at=_now(),
        deleted=0,
    )
    db.add(attachment)
    db.commit()
    db.refresh(attachment)
    return attachment


def get_chat_attachment(
    db: Session,
    workspace_id: str,
    attachment_id: str,
) -> ChatMessageAttachment | None:
    return (
        db.query(ChatMessageAttachment)
        .filter(
            ChatMessageAttachment.workspace_id == workspace_id,
            ChatMessageAttachment.id == attachment_id,
            ChatMessageAttachment.deleted == 0,
        )
        .first()
    )


def read_chat_attachment_bytes(workspace: Workspace, attachment: ChatMessageAttachment) -> bytes:
    raw_config = storage_svc.raw_settings_for(workspace)
    res = default_mcp_storage_server.call_tool(
        "storage_download_file",
        {
            "storage_path": attachment.stored_path,
            "provider": attachment.storage_provider,
            "config": raw_config,
        },
    )
    return res.get("content_bytes") or b""


def link_attachments(
    db: Session,
    attachment_ids: list[str],
    conversation_id: str,
    message_id: str,
) -> list[ChatMessageAttachment]:
    """Associate staged attachments to a conversation and message record."""
    if not attachment_ids:
        return []
    attachments = (
        db.query(ChatMessageAttachment)
        .filter(
            ChatMessageAttachment.id.in_(attachment_ids),
            ChatMessageAttachment.deleted == 0,
        )
        .all()
    )
    for att in attachments:
        att.conversation_id = conversation_id
        att.message_id = message_id
    db.commit()
    return attachments


def get_attachments_for_message(
    db: Session,
    message_id: str,
) -> list[ChatMessageAttachment]:
    return (
        db.query(ChatMessageAttachment)
        .filter(
            ChatMessageAttachment.message_id == message_id,
            ChatMessageAttachment.deleted == 0,
        )
        .order_by(ChatMessageAttachment.created_at.asc())
        .all()
    )


def attachment_to_summary(
    workspace_id: str,
    attachment: ChatMessageAttachment,
) -> ChatAttachmentSummary:
    preview_url = f"/api/workspaces/{workspace_id}/chat/attachments/{attachment.id}"
    return ChatAttachmentSummary(
        id=attachment.id,
        filename=attachment.original_filename,
        mime_type=attachment.mime_type,
        file_size_bytes=attachment.file_size_bytes,
        preview_url=preview_url,
        created_at=attachment.created_at.isoformat() if attachment.created_at else None,
    )
