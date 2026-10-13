"""Sprints: the sprint table, the history of which issues were in each, and `issues.sprint_id`.

Revision ID: 20261005b001
Revises: 20261005a001
Create Date: 2026-10-05

Phase R2 of the reports plan (docs/design/08-reports.md §5.1): burndown, burnup, velocity
and the sprint report all need sprints, and sprints were Phase 2 in
docs/design/06-phase-1-plan.md. This is the minimal model they need, ported from the
PostgreSQL target in docs/design/schema.sql; models/sprint.py records the three places it
departs from that target and why.

Written by hand (CLAUDE.md §17.1), portable as 20261005a001 is: CHAR(36) ids, naive UTC
DateTime, String enums, no CHECK and no partial index. The new column on `issues` goes
through `batch_alter_table` (§17.4), which on SQLite recreates the table — the foreign key
to `sprints` needs that table to exist first, so it is created first.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "20261005b001"
down_revision: Union[str, None] = "20261005a001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_ID = sa.CHAR(36)


def upgrade() -> None:
    op.create_table(
        "sprints",
        sa.Column("id", _ID, primary_key=True, nullable=False),
        sa.Column("workspace_id", _ID, sa.ForeignKey("workspaces.id"), nullable=False),
        sa.Column("space_id", _ID, sa.ForeignKey("spaces.id"), nullable=False),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("goal", sa.Text(), nullable=True),
        sa.Column("state", sa.String(20), nullable=False, server_default="planned"),
        sa.Column("starts_on", sa.Date(), nullable=True),
        sa.Column("ends_on", sa.Date(), nullable=True),
        sa.Column("committed_points", sa.Integer(), nullable=True),
        sa.Column("completed_points", sa.Integer(), nullable=True),
        sa.Column("started_at", sa.DateTime(), nullable=True),
        sa.Column("closed_at", sa.DateTime(), nullable=True),
        sa.Column("created_by", _ID, sa.ForeignKey("users.id"), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.Column("deleted", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("deleted_at", sa.DateTime(), nullable=True),
    )
    op.create_index("ix_sprints_space_state", "sprints", ["space_id", "state"])
    op.create_index("ix_sprints_workspace_space", "sprints", ["workspace_id", "space_id"])

    op.create_table(
        "sprint_issues",
        sa.Column("id", _ID, primary_key=True, nullable=False),
        sa.Column("workspace_id", _ID, sa.ForeignKey("workspaces.id"), nullable=False),
        sa.Column("sprint_id", _ID, sa.ForeignKey("sprints.id"), nullable=False),
        sa.Column("issue_id", _ID, sa.ForeignKey("issues.id"), nullable=False),
        sa.Column("points_at_add", sa.Integer(), nullable=True),
        sa.Column("added_at", sa.DateTime(), nullable=False),
        sa.Column("removed_at", sa.DateTime(), nullable=True),
    )
    op.create_index("ix_sprint_issues_sprint_issue", "sprint_issues", ["sprint_id", "issue_id"])
    op.create_index("ix_sprint_issues_issue", "sprint_issues", ["issue_id"])

    with op.batch_alter_table("issues") as batch:
        batch.add_column(sa.Column("sprint_id", _ID, nullable=True))
        batch.create_foreign_key("fk_issues_sprint_id_sprints", "sprints", ["sprint_id"],
                                 ["id"])
        batch.create_index("ix_issues_workspace_sprint", ["workspace_id", "sprint_id"])


def downgrade() -> None:
    with op.batch_alter_table("issues") as batch:
        batch.drop_index("ix_issues_workspace_sprint")
        batch.drop_constraint("fk_issues_sprint_id_sprints", type_="foreignkey")
        batch.drop_column("sprint_id")
    op.drop_table("sprint_issues")
    op.drop_table("sprints")
