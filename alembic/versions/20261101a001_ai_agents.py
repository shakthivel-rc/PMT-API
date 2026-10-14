"""Add autonomous AI agents: space toggles, daily standups, and sprint retrospectives.

Revision ID: 20261101a001
Revises: 20261025a001
Create Date: 2026-11-01
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "20261101a001"
down_revision: Union[str, None] = "20261025a001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_ID = sa.CHAR(36)


def upgrade() -> None:
    # 1. Add AI agent toggles to spaces
    op.add_column("spaces", sa.Column("ai_triage_enabled", sa.Boolean(), nullable=False, server_default=sa.true()))
    op.add_column("spaces", sa.Column("ai_standup_enabled", sa.Boolean(), nullable=False, server_default=sa.true()))
    op.add_column("spaces", sa.Column("ai_retrospective_enabled", sa.Boolean(), nullable=False, server_default=sa.true()))

    # 2. Create daily_standups table
    op.create_table(
        "daily_standups",
        sa.Column("id", _ID, primary_key=True),
        sa.Column("workspace_id", _ID, sa.ForeignKey("workspaces.id"), nullable=False),
        sa.Column("standup_date", sa.Date(), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("team_updates", sa.JSON(), nullable=True),
        sa.Column("velocity_forecast", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_daily_standups_ws_date", "daily_standups", ["workspace_id", "standup_date"])

    # 3. Create sprint_retrospectives table
    op.create_table(
        "sprint_retrospectives",
        sa.Column("id", _ID, primary_key=True),
        sa.Column("workspace_id", _ID, sa.ForeignKey("workspaces.id"), nullable=False),
        sa.Column("sprint_id", _ID, sa.ForeignKey("sprints.id"), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("wins", sa.JSON(), nullable=True),
        sa.Column("scope_creep", sa.JSON(), nullable=True),
        sa.Column("cycle_time_outliers", sa.JSON(), nullable=True),
        sa.Column("metrics", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_sprint_retrospectives_ws_sprint", "sprint_retrospectives", ["workspace_id", "sprint_id"])


def downgrade() -> None:
    op.drop_index("ix_sprint_retrospectives_ws_sprint", table_name="sprint_retrospectives")
    op.drop_table("sprint_retrospectives")
    op.drop_index("ix_daily_standups_ws_date", table_name="daily_standups")
    op.drop_table("daily_standups")
    op.drop_column("spaces", "ai_retrospective_enabled")
    op.drop_column("spaces", "ai_standup_enabled")
    op.drop_column("spaces", "ai_triage_enabled")
