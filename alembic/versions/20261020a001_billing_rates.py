"""Add billing rates and financial budget tracking.

Revision ID: 20261020a001
Revises: 20261012a001
Create Date: 2026-10-20
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "20261020a001"
down_revision: Union[str, None] = "20261012a001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_ID = sa.CHAR(36)


def upgrade() -> None:
    # 1. Add workspace financial settings
    op.add_column("workspaces", sa.Column("currency", sa.String(3), nullable=False, server_default="USD"))
    op.add_column("workspaces", sa.Column("default_billing_rate", sa.Numeric(10, 2), nullable=True))

    # 2. Add space budget fields
    op.add_column("spaces", sa.Column("budget_amount", sa.Numeric(12, 2), nullable=True))
    op.add_column("spaces", sa.Column("budget_alert_threshold", sa.Integer(), nullable=False, server_default="80"))

    # 3. Add workspace member rate overrides
    op.create_table(
        "workspace_member_rates",
        sa.Column("workspace_id", _ID, sa.ForeignKey("workspaces.id"), primary_key=True),
        sa.Column("user_id", _ID, sa.ForeignKey("users.id"), primary_key=True),
        sa.Column("hourly_rate", sa.Numeric(10, 2), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.func.now(), onupdate=sa.func.now()),
    )

    # 4. Snapshot rate and cost on time entries
    op.add_column("time_entries", sa.Column("billing_rate", sa.Numeric(10, 2), nullable=True))
    op.add_column("time_entries", sa.Column("cost_amount", sa.Numeric(12, 2), nullable=True))


def downgrade() -> None:
    op.drop_column("time_entries", "cost_amount")
    op.drop_column("time_entries", "billing_rate")
    op.drop_table("workspace_member_rates")
    op.drop_column("spaces", "budget_alert_threshold")
    op.drop_column("spaces", "budget_amount")
    op.drop_column("workspaces", "default_billing_rate")
    op.drop_column("workspaces", "currency")
