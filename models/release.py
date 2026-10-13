"""Releases (fix versions) of a space, and which issues ship in each.

docs/design/09-restructuring.md §10. A release belongs to one space, like a sprint, because
it is a promise a delivery unit makes; an issue may be in several (a fix back-ported to two
versions). Release notes are generated from the issues and their outcome, never stored.
"""
import uuid
from datetime import UTC, datetime

from sqlalchemy import (
    CHAR,
    Column,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)

from db.session import Base

RELEASE_STATES: tuple[str, ...] = ("unreleased", "released", "archived")


def _now() -> datetime:
    return datetime.now(UTC)


class Release(Base):
    __tablename__ = "releases"
    __table_args__ = (Index("ix_releases_workspace_space_state", "workspace_id", "space_id",
                            "state"),)

    id = Column(CHAR(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    workspace_id = Column(CHAR(36), ForeignKey("workspaces.id"), nullable=False)
    space_id = Column(CHAR(36), ForeignKey("spaces.id"), nullable=False)
    name = Column(String(100), nullable=False)
    description = Column(Text, nullable=True)
    state = Column(String(20), nullable=False, default="unreleased",
                   server_default="unreleased")
    start_date = Column(Date, nullable=True)
    release_date = Column(Date, nullable=True)
    released_at = Column(DateTime, nullable=True)
    created_by = Column(CHAR(36), ForeignKey("users.id"), nullable=False)
    created_at = Column(DateTime, default=_now, nullable=False)
    updated_at = Column(DateTime, default=_now, onupdate=_now, nullable=False)
    deleted = Column(Integer, nullable=False, default=0)
    deleted_at = Column(DateTime, nullable=True)


class IssueFixVersion(Base):
    __tablename__ = "issue_fix_versions"
    __table_args__ = (
        UniqueConstraint("issue_id", "release_id", name="uq_issue_fix_versions_pair"),
        Index("ix_issue_fix_versions_release", "release_id"),
    )

    id = Column(CHAR(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    workspace_id = Column(CHAR(36), ForeignKey("workspaces.id"), nullable=False)
    issue_id = Column(CHAR(36), ForeignKey("issues.id"), nullable=False)
    release_id = Column(CHAR(36), ForeignKey("releases.id"), nullable=False)
    created_at = Column(DateTime, default=_now, nullable=False)
