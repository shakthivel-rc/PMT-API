"""Transfers and migration plans: old-key aliases, the register, baselines, deferred deliveries.

Revision ID: 20261005f001
Revises: 20261005e001
Create Date: 2026-10-05

docs/design/09-restructuring.md §6.3, §7 and §8:

* `issue_key_aliases` — the key an issue had before it moved to another space, so it keeps
  resolving.
* `migration_baselines`, `migration_plans`, `migration_items` — the plan's register, the
  runner's record and what reconciliation compares against.
* `spaces.frozen_by_plan_id` — which plan froze a space.
* `webhook_deliveries` gains `status` (every existing row was `processed`), `workspace_id`,
  `payload`, `processed_at` and `note`, for deliveries deferred while a space is frozen.

Portable as the revisions before it (CLAUDE.md §17). `migration_baselines.fingerprints` is
LONGTEXT on MySQL and TEXT elsewhere, as `report_cache.payload_json` is.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import mysql

revision: str = "20261005f001"
down_revision: Union[str, None] = "20261005e001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_ID = sa.CHAR(36)
_LONG_TEXT = sa.Text().with_variant(mysql.LONGTEXT(), "mysql")


def upgrade() -> None:
    op.create_table(
        "issue_key_aliases",
        sa.Column("id", _ID, primary_key=True, nullable=False),
        sa.Column("workspace_id", _ID, sa.ForeignKey("workspaces.id"), nullable=False),
        sa.Column("issue_id", _ID, sa.ForeignKey("issues.id"), nullable=False),
        sa.Column("space_id", _ID, sa.ForeignKey("spaces.id"), nullable=False),
        sa.Column("space_key", sa.String(10), nullable=False),
        sa.Column("number", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("workspace_id", "space_key", "number",
                            name="uq_issue_key_aliases_key"),
    )
    op.create_index("ix_issue_key_aliases_issue", "issue_key_aliases", ["issue_id"])

    op.create_table(
        "migration_baselines",
        sa.Column("id", _ID, primary_key=True, nullable=False),
        sa.Column("workspace_id", _ID, sa.ForeignKey("workspaces.id"), nullable=False),
        sa.Column("label", sa.String(120), nullable=False),
        sa.Column("space_ids", sa.JSON(), nullable=False),
        sa.Column("totals", sa.JSON(), nullable=False),
        sa.Column("fingerprints", _LONG_TEXT, nullable=False),
        sa.Column("issue_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("captured_by", _ID, sa.ForeignKey("users.id"), nullable=False),
        sa.Column("captured_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_migration_baselines_workspace", "migration_baselines",
                    ["workspace_id", "captured_at"])

    op.create_table(
        "migration_plans",
        sa.Column("id", _ID, primary_key=True, nullable=False),
        sa.Column("workspace_id", _ID, sa.ForeignKey("workspaces.id"), nullable=False),
        sa.Column("name", sa.String(120), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("status", sa.String(20), nullable=False, server_default="draft"),
        sa.Column("operations", sa.JSON(), nullable=False),
        sa.Column("affected_space_ids", sa.JSON(), nullable=True),
        sa.Column("validation", sa.JSON(), nullable=True),
        sa.Column("baseline_id", _ID, sa.ForeignKey("migration_baselines.id"), nullable=True),
        sa.Column("created_by", _ID, sa.ForeignKey("users.id"), nullable=False),
        sa.Column("approved_by", _ID, sa.ForeignKey("users.id"), nullable=True),
        sa.Column("approved_at", sa.DateTime(), nullable=True),
        sa.Column("validated_at", sa.DateTime(), nullable=True),
        sa.Column("frozen_at", sa.DateTime(), nullable=True),
        sa.Column("started_at", sa.DateTime(), nullable=True),
        sa.Column("finished_at", sa.DateTime(), nullable=True),
        sa.Column("rolled_back_at", sa.DateTime(), nullable=True),
        sa.Column("canceled_at", sa.DateTime(), nullable=True),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_migration_plans_workspace", "migration_plans",
                    ["workspace_id", "created_at"])

    op.create_table(
        "migration_items",
        sa.Column("id", _ID, primary_key=True, nullable=False),
        sa.Column("workspace_id", _ID, sa.ForeignKey("workspaces.id"), nullable=False),
        sa.Column("plan_id", _ID, sa.ForeignKey("migration_plans.id"), nullable=False),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("group_key", sa.String(64), nullable=False),
        sa.Column("kind", sa.String(30), nullable=False),
        sa.Column("subject_type", sa.String(20), nullable=False),
        sa.Column("subject_id", _ID, nullable=False),
        sa.Column("subject_key", sa.String(120), nullable=True),
        sa.Column("source", sa.JSON(), nullable=True),
        sa.Column("target", sa.JSON(), nullable=True),
        sa.Column("options", sa.JSON(), nullable=True),
        sa.Column("owner_id", _ID, sa.ForeignKey("users.id"), nullable=True),
        sa.Column("exception_note", sa.Text(), nullable=True),
        sa.Column("approved", sa.Boolean(), nullable=False, server_default=sa.text("0")),
        sa.Column("state", sa.String(20), nullable=False, server_default="pending"),
        sa.Column("batch", sa.Integer(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("before", sa.JSON(), nullable=True),
        sa.Column("after", sa.JSON(), nullable=True),
        sa.Column("applied_at", sa.DateTime(), nullable=True),
        sa.Column("rolled_back_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("plan_id", "kind", "subject_id", name="uq_migration_items_subject"),
    )
    op.create_index("ix_migration_items_plan_seq", "migration_items", ["plan_id", "seq"])
    op.create_index("ix_migration_items_plan_state", "migration_items", ["plan_id", "state"])

    with op.batch_alter_table("spaces") as batch:
        batch.add_column(sa.Column("frozen_by_plan_id", _ID, nullable=True))
        batch.create_foreign_key("fk_spaces_frozen_by_plan_id_migration_plans",
                                 "migration_plans", ["frozen_by_plan_id"], ["id"])

    with op.batch_alter_table("webhook_deliveries") as batch:
        batch.add_column(sa.Column("status", sa.String(20), nullable=False,
                                   server_default="processed"))
        batch.add_column(sa.Column("workspace_id", _ID, nullable=True))
        batch.add_column(sa.Column("payload", sa.JSON(), nullable=True))
        batch.add_column(sa.Column("processed_at", sa.DateTime(), nullable=True))
        batch.add_column(sa.Column("note", sa.String(255), nullable=True))
        batch.create_foreign_key("fk_webhook_deliveries_workspace_id_workspaces",
                                 "workspaces", ["workspace_id"], ["id"])
        batch.create_index("ix_webhook_deliveries_workspace_status",
                           ["workspace_id", "status", "received_at"])


def downgrade() -> None:
    with op.batch_alter_table("webhook_deliveries") as batch:
        batch.drop_index("ix_webhook_deliveries_workspace_status")
        batch.drop_constraint("fk_webhook_deliveries_workspace_id_workspaces",
                              type_="foreignkey")
        batch.drop_column("note")
        batch.drop_column("processed_at")
        batch.drop_column("payload")
        batch.drop_column("workspace_id")
        batch.drop_column("status")
    with op.batch_alter_table("spaces") as batch:
        batch.drop_constraint("fk_spaces_frozen_by_plan_id_migration_plans",
                              type_="foreignkey")
        batch.drop_column("frozen_by_plan_id")
    op.drop_index("ix_migration_items_plan_state", table_name="migration_items")
    op.drop_index("ix_migration_items_plan_seq", table_name="migration_items")
    op.drop_table("migration_items")
    op.drop_index("ix_migration_plans_workspace", table_name="migration_plans")
    op.drop_table("migration_plans")
    op.drop_index("ix_migration_baselines_workspace", table_name="migration_baselines")
    op.drop_table("migration_baselines")
    op.drop_index("ix_issue_key_aliases_issue", table_name="issue_key_aliases")
    op.drop_table("issue_key_aliases")
