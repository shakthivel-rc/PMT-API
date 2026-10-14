"""Add sprint planning poker tables.

Revision ID: 20261025a001
Revises: 20261020a001
Create Date: 2026-10-25
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "20261025a001"
down_revision: Union[str, None] = "20261020a001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_ID = sa.CHAR(36)


def upgrade() -> None:
    op.create_table(
        "poker_sessions",
        sa.Column("id", _ID, primary_key=True),
        sa.Column("workspace_id", _ID, sa.ForeignKey("workspaces.id"), nullable=False),
        sa.Column("sprint_id", _ID, sa.ForeignKey("sprints.id"), nullable=False),
        sa.Column("active_issue_id", _ID, sa.ForeignKey("issues.id"), nullable=True),
        sa.Column("status", sa.String(20), nullable=False, server_default="voting"),
        sa.Column("created_by", _ID, sa.ForeignKey("users.id"), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.func.now(), onupdate=sa.func.now()),
    )
    op.create_index("ix_poker_sessions_sprint", "poker_sessions", ["workspace_id", "sprint_id"])

    op.create_table(
        "poker_votes",
        sa.Column("session_id", _ID, sa.ForeignKey("poker_sessions.id"), primary_key=True),
        sa.Column("user_id", _ID, sa.ForeignKey("users.id"), primary_key=True),
        sa.Column("issue_id", _ID, sa.ForeignKey("issues.id"), primary_key=True),
        sa.Column("point_vote", sa.Integer(), nullable=True),
        sa.Column("special_vote", sa.String(10), nullable=True),
        sa.Column("voted_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
    )


def downgrade() -> None:
    op.drop_table("poker_votes")
    op.drop_index("ix_poker_sessions_sprint", table_name="poker_sessions")
    op.drop_table("poker_sessions")
