"""Cross-space portfolio initiatives (Epics 2.0).

Strategic high-level initiatives spanning multiple spaces and issues within a workspace.
"""
from datetime import UTC, datetime
import uuid

from sqlalchemy import CHAR, Column, Date, DateTime, ForeignKey, Integer, Numeric, String, Text, UniqueConstraint
from db.session import Base


def _now() -> datetime:
    return datetime.now(UTC)


class Initiative(Base):
    """A cross-space portfolio initiative uniting epics, features, and teams."""

    __tablename__ = "initiatives"
    __table_args__ = (
        UniqueConstraint("workspace_id", "key", name="uq_initiatives_workspace_key"),
    )

    id = Column(CHAR(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    workspace_id = Column(CHAR(36), ForeignKey("workspaces.id"), nullable=False, index=True)
    key = Column(String(20), nullable=False)
    title = Column(String(255), nullable=False)
    description = Column(Text, nullable=True)
    status = Column(String(30), nullable=False, default="planned", server_default="planned")
    owner_id = Column(CHAR(36), ForeignKey("users.id"), nullable=True)
    target_quarter = Column(String(10), nullable=True)  # e.g. "2026-Q4"
    start_date = Column(Date, nullable=True)
    target_date = Column(Date, nullable=True)
    budget_amount = Column(Numeric(12, 2), nullable=True)
    created_by = Column(CHAR(36), ForeignKey("users.id"), nullable=False)
    created_at = Column(DateTime, default=_now, nullable=False)
    updated_at = Column(DateTime, default=_now, onupdate=_now, nullable=False)
    deleted = Column(Integer, nullable=False, default=0, server_default="0")


class InitiativeSpace(Base):
    """Which Spaces contribute to an Initiative."""

    __tablename__ = "initiative_spaces"

    initiative_id = Column(CHAR(36), ForeignKey("initiatives.id"), primary_key=True)
    space_id = Column(CHAR(36), ForeignKey("spaces.id"), primary_key=True)
    workspace_id = Column(CHAR(36), ForeignKey("workspaces.id"), nullable=False, index=True)
    created_at = Column(DateTime, default=_now, nullable=False)


class InitiativeIssue(Base):
    """Linked Epics and Issues feeding into an Initiative."""

    __tablename__ = "initiative_issues"
    __table_args__ = (
        UniqueConstraint("initiative_id", "issue_id", name="uq_initiative_issues_pair"),
    )

    id = Column(CHAR(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    workspace_id = Column(CHAR(36), ForeignKey("workspaces.id"), nullable=False, index=True)
    initiative_id = Column(CHAR(36), ForeignKey("initiatives.id"), nullable=False, index=True)
    issue_id = Column(CHAR(36), ForeignKey("issues.id"), nullable=False, index=True)
    created_at = Column(DateTime, default=_now, nullable=False)
