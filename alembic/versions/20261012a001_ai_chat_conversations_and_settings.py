"""AI Chat conversations, message history, and user LLM settings.

Revision ID: 20261012a001
Revises: 20261008a001
Create Date: 2026-10-12

Creates tables for AI chat gateway:
- user_llm_settings: Per-user provider, model, encrypted API key, and base URL configuration.
- chat_conversations: Chat session metadata per workspace and user.
- chat_messages: Message history with tool calls, confirmation cards, and undo action records.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "20261012a001"
down_revision: Union[str, None] = "20261008a001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_ID = sa.CHAR(36)


def upgrade() -> None:
    # 1. user_llm_settings
    op.create_table(
        "user_llm_settings",
        sa.Column("id", _ID, primary_key=True, nullable=False),
        sa.Column("user_id", _ID, sa.ForeignKey("users.id"), nullable=False, unique=True),
        sa.Column("provider", sa.String(50), nullable=False, server_default="openai"),
        sa.Column("model", sa.String(100), nullable=False, server_default="gpt-4o"),
        sa.Column("encrypted_api_key", sa.Text(), nullable=True),
        sa.Column("base_url", sa.String(255), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
    )
    op.create_index(
        "ix_user_llm_settings_user",
        "user_llm_settings",
        ["user_id"],
    )

    # 2. chat_conversations
    op.create_table(
        "chat_conversations",
        sa.Column("id", _ID, primary_key=True, nullable=False),
        sa.Column("workspace_id", _ID, sa.ForeignKey("workspaces.id"), nullable=False),
        sa.Column("user_id", _ID, sa.ForeignKey("users.id"), nullable=False),
        sa.Column("title", sa.String(200), nullable=False, server_default="New Conversation"),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.Column("deleted", sa.Integer(), nullable=False, server_default="0"),
    )
    op.create_index(
        "ix_chat_conversations_user_workspace",
        "chat_conversations",
        ["workspace_id", "user_id"],
    )

    # 3. chat_messages
    op.create_table(
        "chat_messages",
        sa.Column("id", _ID, primary_key=True, nullable=False),
        sa.Column("conversation_id", _ID, sa.ForeignKey("chat_conversations.id"), nullable=False),
        sa.Column("role", sa.String(20), nullable=False),
        sa.Column("content", sa.Text(), nullable=True),
        sa.Column("tool_calls", sa.JSON(), nullable=True),
        sa.Column("tool_call_id", sa.String(100), nullable=True),
        sa.Column("name", sa.String(100), nullable=True),
        sa.Column("confirm_card", sa.JSON(), nullable=True),
        sa.Column("undo_action", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    op.create_index(
        "ix_chat_messages_conv_created",
        "chat_messages",
        ["conversation_id", "created_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_chat_messages_conv_created", table_name="chat_messages")
    op.drop_table("chat_messages")

    op.drop_index("ix_chat_conversations_user_workspace", table_name="chat_conversations")
    op.drop_table("chat_conversations")

    op.drop_index("ix_user_llm_settings_user", table_name="user_llm_settings")
    op.drop_table("user_llm_settings")
