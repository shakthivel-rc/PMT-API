from sqlalchemy import Column, String, Text, DateTime, ForeignKey, Index
from sqlalchemy.dialects.mysql import CHAR
from db.session import Base
from datetime import datetime, timezone
import uuid


class ActivityLog(Base):
    __tablename__ = "activity_logs"
    # The log is always read newest-first and usually filtered by user. Declared here as well
    # as in the baseline revision, so a database built by `create_all` (the test harness) and
    # one built by Alembic (production) carry the same index under the same name.
    __table_args__ = (Index("ix_activity_logs_user_created", "user_id", "created_at"),)

    id = Column(CHAR(36), primary_key=True, default=lambda: str(uuid.uuid4()), index=True)
    user_id = Column(CHAR(36), ForeignKey("users.id"), nullable=False)
    user_name = Column(String(200), nullable=True)
    action = Column(String(50), nullable=False)       # LOGIN, LOGOUT, CREATE, UPDATE, DELETE, SECURITY
    entity_type = Column(String(50), nullable=True)    # user, role, permission
    entity_id = Column(CHAR(36), nullable=True)
    details = Column(Text, nullable=True)
    ip_address = Column(String(45), nullable=True)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), nullable=False)
