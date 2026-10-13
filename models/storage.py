"""File and image attachments uploaded to issues.

Attachments support local filesystem storage as well as cloud providers
(AWS S3, Azure Blob Storage) via MCP (Model Context Protocol).
"""
import uuid
from datetime import UTC, datetime

from sqlalchemy import (
    CHAR,
    Boolean,
    Column,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
)

from db.session import Base


def _now() -> datetime:
    return datetime.now(UTC)


class IssueAttachment(Base):
    __tablename__ = "issue_attachments"
    __table_args__ = (
        Index("ix_issue_attachments_issue_created", "issue_id", "created_at"),
        Index("ix_issue_attachments_workspace", "workspace_id"),
        Index("ix_issue_attachments_comment", "comment_id"),
    )

    id = Column(CHAR(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    workspace_id = Column(CHAR(36), ForeignKey("workspaces.id"), nullable=False)
    issue_id = Column(CHAR(36), ForeignKey("issues.id"), nullable=False)
    comment_id = Column(CHAR(36), ForeignKey("issue_comments.id"), nullable=True)
    uploader_id = Column(CHAR(36), ForeignKey("users.id"), nullable=False)
    filename = Column(String(255), nullable=False)
    stored_filename = Column(String(255), nullable=False)
    file_size = Column(Integer, nullable=False, default=0)
    content_type = Column(String(100), nullable=False, default="application/octet-stream")
    storage_provider = Column(String(20), nullable=False, default="local")
    storage_path = Column(String(500), nullable=False)
    is_image = Column(Boolean, nullable=False, default=False)
    created_at = Column(DateTime, default=_now, nullable=False)
    deleted = Column(Integer, nullable=False, default=0)
