"""Add cross-space portfolio initiatives and item links.

Revision ID: 20261115a001
Revises: 20261101a001
Create Date: 2026-11-15
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "20261115a001"
down_revision: Union[str, None] = "20261101a001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_ID = sa.CHAR(36)


def upgrade() -> None:
    # 1. Workspace-level Initiatives
    op.create_table(
        "initiatives",
        sa.Column("id", _ID, primary_key=True),
        sa.Column("workspace_id", _ID, sa.ForeignKey("workspaces.id"), nullable=False, index=True),
        sa.Column("key", sa.String(20), nullable=False),
        sa.Column("title", sa.String(255), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("status", sa.String(30), nullable=False, server_default="planned"),
        sa.Column("owner_id", _ID, sa.ForeignKey("users.id"), nullable=True),
        sa.Column("target_quarter", sa.String(10), nullable=True),
        sa.Column("start_date", sa.Date(), nullable=True),
        sa.Column("target_date", sa.Date(), nullable=True),
        sa.Column("budget_amount", sa.Numeric(12, 2), nullable=True),
        sa.Column("created_by", _ID, sa.ForeignKey("users.id"), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.func.now(), onupdate=sa.func.now()),
        sa.Column("deleted", sa.Integer(), nullable=False, server_default="0"),
        sa.UniqueConstraint("workspace_id", "key", name="uq_initiatives_workspace_key"),
    )

    # 2. Junction table: which Spaces contribute to an Initiative
    op.create_table(
        "initiative_spaces",
        sa.Column("initiative_id", _ID, sa.ForeignKey("initiatives.id"), primary_key=True),
        sa.Column("space_id", _ID, sa.ForeignKey("spaces.id"), primary_key=True),
        sa.Column("workspace_id", _ID, sa.ForeignKey("workspaces.id"), nullable=False, index=True),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
    )

    # 3. Linked Epics / Issues feeding into the Initiative
    op.create_table(
        "initiative_issues",
        sa.Column("id", _ID, primary_key=True),
        sa.Column("workspace_id", _ID, sa.ForeignKey("workspaces.id"), nullable=False, index=True),
        sa.Column("initiative_id", _ID, sa.ForeignKey("initiatives.id"), nullable=False, index=True),
        sa.Column("issue_id", _ID, sa.ForeignKey("issues.id"), nullable=False, index=True),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("initiative_id", "issue_id", name="uq_initiative_issues_pair"),
    )


def downgrade() -> None:
    op.drop_table("initiative_issues")
    op.drop_table("initiative_spaces")
    op.drop_table("initiatives")
