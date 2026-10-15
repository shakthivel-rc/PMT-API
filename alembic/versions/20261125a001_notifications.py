"""Add notifications table.

Revision ID: 20261125a001
Revises: 20261120a001
Create Date: 2026-11-25
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "20261125a001"
down_revision: Union[str, None] = "20261120a001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_ID = sa.CHAR(36)


def upgrade() -> None:
    op.create_table(
        "notifications",
        sa.Column("id", _ID, primary_key=True),
        sa.Column("user_id", _ID, sa.ForeignKey("users.id"), nullable=False, index=True),
        sa.Column("actor_id", _ID, sa.ForeignKey("users.id"), nullable=True),
        sa.Column("workspace_id", _ID, sa.ForeignKey("workspaces.id"), nullable=True),
        sa.Column("issue_id", _ID, sa.ForeignKey("issues.id"), nullable=True),
        sa.Column("type", sa.String(50), nullable=False, server_default="system"),
        sa.Column("title", sa.String(255), nullable=False),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column("link", sa.String(500), nullable=True),
        sa.Column("is_read", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("read_at", sa.DateTime(), nullable=True),
    )
    op.create_index(
        "ix_notifications_user_unread",
        "notifications",
        ["user_id", "is_read", "created_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_notifications_user_unread", table_name="notifications")
    op.drop_table("notifications")
