"""Chat conversation session and message history models."""
import uuid
from datetime import UTC, datetime

from sqlalchemy import (
    CHAR,
    JSON,
    BigInteger,
    Column,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
)

from db.session import Base


def _now() -> datetime:
    return datetime.now(UTC)


class ChatConversation(Base):
    __tablename__ = "chat_conversations"
    __table_args__ = (
        Index("ix_chat_conversations_user_workspace", "workspace_id", "user_id"),
    )

    id = Column(CHAR(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    workspace_id = Column(CHAR(36), ForeignKey("workspaces.id"), nullable=False)
    user_id = Column(CHAR(36), ForeignKey("users.id"), nullable=False)
    title = Column(String(200), nullable=False, default="New Conversation")
    created_at = Column(DateTime, default=_now, nullable=False)
    updated_at = Column(DateTime, default=_now, onupdate=_now, nullable=False)
    deleted = Column(Integer, nullable=False, default=0)


class ChatMessageRecord(Base):
    __tablename__ = "chat_messages"
    __table_args__ = (
        Index("ix_chat_messages_conv_created", "conversation_id", "created_at"),
    )

    id = Column(CHAR(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    seq = Column(BigInteger, autoincrement=True, unique=True, nullable=True)
    conversation_id = Column(CHAR(36), ForeignKey("chat_conversations.id"), nullable=False)
    role = Column(String(20), nullable=False)  # user, assistant, system, tool
    content = Column(Text, nullable=True)
    tool_calls = Column(JSON, nullable=True)
    tool_call_id = Column(String(100), nullable=True)
    name = Column(String(100), nullable=True)
    confirm_card = Column(JSON, nullable=True)
    undo_action = Column(JSON, nullable=True)
    created_at = Column(DateTime, default=_now, nullable=False)


class ChatMessageAttachment(Base):
    """File attachment (image, document, log, sheet) uploaded within a chat session."""
    __tablename__ = "chat_message_attachments"
    __table_args__ = (
        Index("ix_chat_attachments_workspace", "workspace_id"),
        Index("ix_chat_attachments_conv", "conversation_id"),
        Index("ix_chat_attachments_msg", "message_id"),
    )

    id = Column(CHAR(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    conversation_id = Column(CHAR(36), ForeignKey("chat_conversations.id"), nullable=True)
    message_id = Column(CHAR(36), ForeignKey("chat_messages.id"), nullable=True)
    workspace_id = Column(CHAR(36), ForeignKey("workspaces.id"), nullable=False)
    user_id = Column(CHAR(36), ForeignKey("users.id"), nullable=False)
    original_filename = Column(String(255), nullable=False)
    stored_path = Column(String(500), nullable=False)
    storage_provider = Column(String(20), nullable=False, default="local")
    mime_type = Column(String(100), nullable=False)
    file_size_bytes = Column(BigInteger, nullable=False, default=0)
    extracted_text = Column(Text, nullable=True)
    created_at = Column(DateTime, default=_now, nullable=False)
    deleted = Column(Integer, nullable=False, default=0)

