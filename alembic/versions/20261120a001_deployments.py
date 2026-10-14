"""Add CI/CD deployments and issue deployment markers.

Revision ID: 20261120a001
Revises: 20261115a001
Create Date: 2026-11-20
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "20261120a001"
down_revision: Union[str, None] = "20261115a001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_ID = sa.CHAR(36)


def upgrade() -> None:
    # 1. Deployments Table
    op.create_table(
        "deployments",
        sa.Column("id", _ID, primary_key=True),
        sa.Column("workspace_id", _ID, sa.ForeignKey("workspaces.id"), nullable=False, index=True),
        sa.Column("repository_id", _ID, sa.ForeignKey("vcs_repositories.id"), nullable=True, index=True),
        sa.Column("environment", sa.String(50), nullable=False),  # development, staging, production, canary
        sa.Column("status", sa.String(20), nullable=False),       # in_progress, success, failed, rolled_back
        sa.Column("commit_sha", sa.String(40), nullable=False),
        sa.Column("ref_name", sa.String(100), nullable=True),
        sa.Column("deployer_name", sa.String(100), nullable=True),
        sa.Column("deployment_url", sa.String(500), nullable=True),
        sa.Column("started_at", sa.DateTime(), nullable=False),
        sa.Column("finished_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_deployments_ws_env_created", "deployments", ["workspace_id", "environment", "created_at"])

    # 2. Issue Deployments Junction
    op.create_table(
        "issue_deployments",
        sa.Column("id", _ID, primary_key=True),
        sa.Column("workspace_id", _ID, sa.ForeignKey("workspaces.id"), nullable=False),
        sa.Column("issue_id", _ID, sa.ForeignKey("issues.id"), nullable=False, index=True),
        sa.Column("deployment_id", _ID, sa.ForeignKey("deployments.id"), nullable=False, index=True),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("issue_id", "deployment_id", name="uq_issue_deployments_pair"),
    )


def downgrade() -> None:
    op.drop_table("issue_deployments")
    op.drop_index("ix_deployments_ws_env_created", table_name="deployments")
    op.drop_table("deployments")
