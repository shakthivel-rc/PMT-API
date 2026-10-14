"""CI/CD Deployment pipelines and issue deployment markers.

Tracks multi-environment deployments across Development, Staging, and Production.
"""
from datetime import UTC, datetime
import uuid

from sqlalchemy import CHAR, Column, DateTime, ForeignKey, Index, String, UniqueConstraint
from db.session import Base


def _now() -> datetime:
    return datetime.now(UTC)


class Deployment(Base):
    """A registered CI/CD deployment event."""

    __tablename__ = "deployments"
    __table_args__ = (
        Index("ix_deployments_ws_env_created", "workspace_id", "environment", "created_at"),
    )

    id = Column(CHAR(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    workspace_id = Column(CHAR(36), ForeignKey("workspaces.id"), nullable=False, index=True)
    repository_id = Column(CHAR(36), ForeignKey("vcs_repositories.id"), nullable=True, index=True)
    environment = Column(String(50), nullable=False)  # development, staging, production, canary
    status = Column(String(20), nullable=False)       # in_progress, success, failed, rolled_back
    commit_sha = Column(String(40), nullable=False)
    ref_name = Column(String(100), nullable=True)     # e.g. "main", "release/v1.4.0"
    deployer_name = Column(String(100), nullable=True)
    deployment_url = Column(String(500), nullable=True)
    started_at = Column(DateTime, default=_now, nullable=False)
    finished_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=_now, nullable=False)


class IssueDeployment(Base):
    """Which issues were shipped in which deployment."""

    __tablename__ = "issue_deployments"
    __table_args__ = (
        UniqueConstraint("issue_id", "deployment_id", name="uq_issue_deployments_pair"),
    )

    id = Column(CHAR(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    workspace_id = Column(CHAR(36), ForeignKey("workspaces.id"), nullable=False)
    issue_id = Column(CHAR(36), ForeignKey("issues.id"), nullable=False, index=True)
    deployment_id = Column(CHAR(36), ForeignKey("deployments.id"), nullable=False, index=True)
    created_at = Column(DateTime, default=_now, nullable=False)
