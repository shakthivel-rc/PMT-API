"""In-app notifications for users (assignment, status changes, mentions, system)."""
import uuid
from datetime import UTC, datetime

from sqlalchemy import (
    BOOLEAN,
    CHAR,
    Column,
    DateTime,
    ForeignKey,
    Index,
    String,
    Text,
)

from db.session import Base


def _now() -> datetime:
    return datetime.now(UTC)


class Notification(Base):
    __tablename__ = "notifications"
    __table_args__ = (
        Index("ix_notifications_user_unread", "user_id", "is_read", "created_at"),
    )

    id = Column(CHAR(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id = Column(CHAR(36), ForeignKey("users.id"), nullable=False, index=True)
    actor_id = Column(CHAR(36), ForeignKey("users.id"), nullable=True)
    workspace_id = Column(CHAR(36), ForeignKey("workspaces.id"), nullable=True)
    issue_id = Column(CHAR(36), ForeignKey("issues.id"), nullable=True)

    type = Column(String(50), nullable=False, default="system")  # assigned, status_changed, mentioned, comment, system
    title = Column(String(255), nullable=False)
    message = Column(Text, nullable=False)
    link = Column(String(500), nullable=True)
    is_read = Column(BOOLEAN, nullable=False, default=False)
    created_at = Column(DateTime, default=_now, nullable=False)
    read_at = Column(DateTime, nullable=True)
