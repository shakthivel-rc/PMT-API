"""Customisable workflows: stage colours, WIP, soft-deleted stages, transitions, templates.

Revision ID: 20261005d001
Revises: 20261005c001
Create Date: 2026-10-05

docs/design/09-restructuring.md §4 and §6. What changes, and what every existing row becomes:

* `workflow_statuses` gains `color`, `counts_toward_wip`, `wip_limit`, `deleted`,
  `deleted_at` and `updated_at`. Existing stages get the colour of their category and count
  toward WIP exactly when they are `in_progress` — the two stages the default workflow has
  always treated as active work.
* `spaces` gains the workflow's settings. Every existing space keeps behaving as it did:
  `transition_mode` `any` (free movement, as before), `initial_status_id` its first `todo`
  stage (what `services/issue.default_status` used to pick), and the merge action `status`
  aimed at its first `done` stage (what the hard-wired webhook rule used to pick) — or `none`
  for a space that has no done stage, where that rule did nothing either.
* New tables: `workflow_transitions`, `workflow_templates`, `workflow_revisions`,
  `space_workflow_editors`.

Written by hand and portable (CLAUDE.md §17). The backfills are correlated scalar subqueries,
which MySQL 8 and SQLite both run as written. `updated_at` is added nullable, filled from
`created_at` and then made NOT NULL, because neither database gives a DATETIME a portable
"now" default to backfill with.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "20261005d001"
down_revision: Union[str, None] = "20261005c001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_ID = sa.CHAR(36)


def upgrade() -> None:
    with op.batch_alter_table("workflow_statuses") as batch:
        batch.add_column(sa.Column("color", sa.String(20), nullable=False,
                                   server_default="neutral"))
        batch.add_column(sa.Column("counts_toward_wip", sa.Boolean(), nullable=False,
                                   server_default=sa.text("0")))
        batch.add_column(sa.Column("wip_limit", sa.Integer(), nullable=True))
        batch.add_column(sa.Column("deleted", sa.Integer(), nullable=False,
                                   server_default="0"))
        batch.add_column(sa.Column("deleted_at", sa.DateTime(), nullable=True))
        batch.add_column(sa.Column("updated_at", sa.DateTime(), nullable=True))
    op.execute("UPDATE workflow_statuses SET updated_at = created_at")
    op.execute("UPDATE workflow_statuses SET color = 'info', counts_toward_wip = 1 "
               "WHERE category = 'in_progress'")
    op.execute("UPDATE workflow_statuses SET color = 'success' WHERE category = 'done'")
    with op.batch_alter_table("workflow_statuses") as batch:
        batch.alter_column("updated_at", existing_type=sa.DateTime(), nullable=False)

    with op.batch_alter_table("spaces") as batch:
        batch.add_column(sa.Column("initial_status_id", _ID, nullable=True))
        batch.add_column(sa.Column("transition_mode", sa.String(20), nullable=False,
                                   server_default="any"))
        batch.add_column(sa.Column("workflow_version", sa.Integer(), nullable=False,
                                   server_default="1"))
        batch.add_column(sa.Column("wip_policy", sa.String(10), nullable=False,
                                   server_default="warn"))
        batch.add_column(sa.Column("merge_action", sa.String(20), nullable=False,
                                   server_default="status"))
        batch.add_column(sa.Column("merge_target_status_id", _ID, nullable=True))
    op.execute(
        "UPDATE spaces SET initial_status_id = ("
        " SELECT ws.id FROM workflow_statuses ws"
        " WHERE ws.space_id = spaces.id AND ws.category = 'todo'"
        " ORDER BY ws.position, ws.id LIMIT 1)")
    op.execute(
        "UPDATE spaces SET initial_status_id = ("
        " SELECT ws.id FROM workflow_statuses ws WHERE ws.space_id = spaces.id"
        " ORDER BY ws.position, ws.id LIMIT 1)"
        " WHERE initial_status_id IS NULL")
    op.execute(
        "UPDATE spaces SET merge_target_status_id = ("
        " SELECT ws.id FROM workflow_statuses ws"
        " WHERE ws.space_id = spaces.id AND ws.category = 'done'"
        " ORDER BY ws.position, ws.id LIMIT 1)")
    op.execute("UPDATE spaces SET merge_action = 'none' WHERE merge_target_status_id IS NULL")

    op.create_table(
        "workflow_transitions",
        sa.Column("id", _ID, primary_key=True, nullable=False),
        sa.Column("workspace_id", _ID, sa.ForeignKey("workspaces.id"), nullable=False),
        sa.Column("space_id", _ID, sa.ForeignKey("spaces.id"), nullable=False),
        sa.Column("from_status_id", _ID, sa.ForeignKey("workflow_statuses.id"), nullable=True),
        sa.Column("to_status_id", _ID, sa.ForeignKey("workflow_statuses.id"), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("space_id", "from_status_id", "to_status_id",
                            name="uq_workflow_transitions_pair"),
    )
    op.create_index("ix_workflow_transitions_space", "workflow_transitions",
                    ["workspace_id", "space_id"])

    op.create_table(
        "workflow_templates",
        sa.Column("id", _ID, primary_key=True, nullable=False),
        sa.Column("workspace_id", _ID, sa.ForeignKey("workspaces.id"), nullable=False),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("definition", sa.JSON(), nullable=False),
        sa.Column("created_by", _ID, sa.ForeignKey("users.id"), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.Column("deleted", sa.Integer(), nullable=False, server_default="0"),
    )
    op.create_index("ix_workflow_templates_workspace", "workflow_templates", ["workspace_id"])

    op.create_table(
        "workflow_revisions",
        sa.Column("id", _ID, primary_key=True, nullable=False),
        sa.Column("workspace_id", _ID, sa.ForeignKey("workspaces.id"), nullable=False),
        sa.Column("space_id", _ID, sa.ForeignKey("spaces.id"), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("definition", sa.JSON(), nullable=False),
        sa.Column("impact", sa.JSON(), nullable=True),
        sa.Column("published_by", _ID, sa.ForeignKey("users.id"), nullable=False),
        sa.Column("published_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_workflow_revisions_space_version", "workflow_revisions",
                    ["space_id", "version"])

    op.create_table(
        "space_workflow_editors",
        sa.Column("id", _ID, primary_key=True, nullable=False),
        sa.Column("workspace_id", _ID, sa.ForeignKey("workspaces.id"), nullable=False),
        sa.Column("space_id", _ID, sa.ForeignKey("spaces.id"), nullable=False),
        sa.Column("user_id", _ID, sa.ForeignKey("users.id"), nullable=False),
        sa.Column("granted_by", _ID, sa.ForeignKey("users.id"), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("space_id", "user_id", name="uq_space_workflow_editors_space_user"),
    )
    op.create_index("ix_space_workflow_editors_user_id", "space_workflow_editors", ["user_id"])


def downgrade() -> None:
    op.drop_index("ix_space_workflow_editors_user_id", table_name="space_workflow_editors")
    op.drop_table("space_workflow_editors")
    op.drop_index("ix_workflow_revisions_space_version", table_name="workflow_revisions")
    op.drop_table("workflow_revisions")
    op.drop_index("ix_workflow_templates_workspace", table_name="workflow_templates")
    op.drop_table("workflow_templates")
    op.drop_index("ix_workflow_transitions_space", table_name="workflow_transitions")
    op.drop_table("workflow_transitions")
    # Stages removed while this revision was applied come back as live stages: the column
    # that hid them is going. Their issues were moved to a replacement when they were
    # removed, so a revived stage is empty — but it is visible, which is the honest result of
    # running a schema backwards over data written forwards.
    with op.batch_alter_table("spaces") as batch:
        batch.drop_column("merge_target_status_id")
        batch.drop_column("merge_action")
        batch.drop_column("wip_policy")
        batch.drop_column("workflow_version")
        batch.drop_column("transition_mode")
        batch.drop_column("initial_status_id")
    with op.batch_alter_table("workflow_statuses") as batch:
        batch.drop_column("updated_at")
        batch.drop_column("deleted_at")
        batch.drop_column("deleted")
        batch.drop_column("wip_limit")
        batch.drop_column("counts_toward_wip")
        batch.drop_column("color")
