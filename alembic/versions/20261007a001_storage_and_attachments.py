"""Issue attachments and storage configurations.

Revision ID: 20261007a001
Revises: 20261005g001
Create Date: 2026-10-07

Creates `issue_attachments` table for storing attachments uploaded to issues
via local filesystem or cloud object storage (AWS S3, Azure Blob Storage) using MCP.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "20261007a001"
down_revision: Union[str, None] = "20261005g001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_ID = sa.CHAR(36)


def upgrade() -> None:
    op.create_table(
        "issue_attachments",
        sa.Column("id", _ID, primary_key=True, nullable=False),
        sa.Column("workspace_id", _ID, sa.ForeignKey("workspaces.id"), nullable=False),
        sa.Column("issue_id", _ID, sa.ForeignKey("issues.id"), nullable=False),
        sa.Column("uploader_id", _ID, sa.ForeignKey("users.id"), nullable=False),
        sa.Column("filename", sa.String(255), nullable=False),
        sa.Column("stored_filename", sa.String(255), nullable=False),
        sa.Column("file_size", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "content_type",
            sa.String(100),
            nullable=False,
            server_default="application/octet-stream",
        ),
        sa.Column("storage_provider", sa.String(20), nullable=False, server_default="local"),
        sa.Column("storage_path", sa.String(500), nullable=False),
        sa.Column("is_image", sa.Boolean(), nullable=False, server_default=sa.text("0")),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("deleted", sa.Integer(), nullable=False, server_default="0"),
    )
    op.create_index(
        "ix_issue_attachments_issue_created",
        "issue_attachments",
        ["issue_id", "created_at"],
    )
    op.create_index(
        "ix_issue_attachments_workspace",
        "issue_attachments",
        ["workspace_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_issue_attachments_workspace", table_name="issue_attachments")
    op.drop_index("ix_issue_attachments_issue_created", table_name="issue_attachments")
    op.drop_table("issue_attachments")
