"""Add chat message attachments table.

Revision ID: 20261130a001
Revises: 20261128a001
Create Date: 2026-11-30
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "20261130a001"
down_revision: Union[str, None] = "20261128a001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_ID = sa.CHAR(36)


def upgrade() -> None:
    op.create_table(
        "chat_message_attachments",
        sa.Column("id", _ID, primary_key=True),
        sa.Column("conversation_id", _ID, sa.ForeignKey("chat_conversations.id"), nullable=True),
        sa.Column("message_id", _ID, sa.ForeignKey("chat_messages.id"), nullable=True),
        sa.Column("workspace_id", _ID, sa.ForeignKey("workspaces.id"), nullable=False),
        sa.Column("user_id", _ID, sa.ForeignKey("users.id"), nullable=False),
        sa.Column("original_filename", sa.String(255), nullable=False),
        sa.Column("stored_path", sa.String(500), nullable=False),
        sa.Column("storage_provider", sa.String(20), nullable=False, server_default="local"),
        sa.Column("mime_type", sa.String(100), nullable=False),
        sa.Column("file_size_bytes", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("extracted_text", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("deleted", sa.Integer(), nullable=False, server_default="0"),
    )
    op.create_index(
        "ix_chat_attachments_workspace",
        "chat_message_attachments",
        ["workspace_id"],
    )
    op.create_index(
        "ix_chat_attachments_conv",
        "chat_message_attachments",
        ["conversation_id"],
    )
    op.create_index(
        "ix_chat_attachments_msg",
        "chat_message_attachments",
        ["message_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_chat_attachments_msg", table_name="chat_message_attachments")
    op.drop_index("ix_chat_attachments_conv", table_name="chat_message_attachments")
    op.drop_index("ix_chat_attachments_workspace", table_name="chat_message_attachments")
    op.drop_table("chat_message_attachments")
