"""Reports: snapshots, the result cache, saved reports, schedules, export jobs, billable time.

Revision ID: 20261005c001
Revises: 20261005b001
Create Date: 2026-10-05

The storage behind docs/design/08-reports.md §5.2; models/reporting.py describes each table.
Also:

* `time_entries.billable` — the billable flag the plan's §12 question 3 asked for. Every
  existing entry becomes billable through the server default, which is what it was logged
  for; nothing else in the row changes.
* Two indexes the report queries need (§5.2): `issues(workspace_id, space_id, status_id)`
  and `issue_events(workspace_id, event, created_at)`.

Written by hand and portable (CLAUDE.md §17). The two LONG column variants are MySQL-only
and fall back to BLOB / TEXT elsewhere, which SQLite does not limit.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import mysql

revision: str = "20261005c001"
down_revision: Union[str, None] = "20261005b001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_ID = sa.CHAR(36)
_LONG_BLOB = sa.LargeBinary().with_variant(mysql.LONGBLOB(), "mysql")
_LONG_TEXT = sa.Text().with_variant(mysql.LONGTEXT(), "mysql")


def upgrade() -> None:
    with op.batch_alter_table("time_entries") as batch:
        batch.add_column(sa.Column("billable", sa.Boolean(), nullable=False,
                                   server_default=sa.text("1")))
    op.create_index("ix_issues_workspace_space_status", "issues",
                    ["workspace_id", "space_id", "status_id"])
    op.create_index("ix_issue_events_workspace_event_created", "issue_events",
                    ["workspace_id", "event", "created_at"])

    op.create_table(
        "issue_daily_status",
        sa.Column("snapshot_date", sa.Date(), primary_key=True, nullable=False),
        sa.Column("issue_id", _ID, sa.ForeignKey("issues.id"), primary_key=True,
                  nullable=False),
        sa.Column("workspace_id", _ID, sa.ForeignKey("workspaces.id"), nullable=False),
        sa.Column("space_id", _ID, nullable=False),
        sa.Column("list_id", _ID, nullable=False),
        sa.Column("status_id", _ID, nullable=False),
        sa.Column("status_category", sa.String(20), nullable=False),
        sa.Column("assignee_id", _ID, nullable=True),
        sa.Column("story_points", sa.Integer(), nullable=True),
        sa.Column("time_estimate_seconds", sa.Integer(), nullable=True),
        sa.Column("sprint_id", _ID, nullable=True),
    )
    op.create_index("ix_issue_daily_status_scope", "issue_daily_status",
                    ["workspace_id", "space_id", "snapshot_date"])
    op.create_index("ix_issue_daily_status_sprint", "issue_daily_status",
                    ["sprint_id", "snapshot_date"])

    op.create_table(
        "issue_snapshot_days",
        sa.Column("workspace_id", _ID, sa.ForeignKey("workspaces.id"), primary_key=True,
                  nullable=False),
        sa.Column("snapshot_date", sa.Date(), primary_key=True, nullable=False),
        sa.Column("issue_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )

    op.create_table(
        "report_cache",
        sa.Column("key", sa.String(64), primary_key=True, nullable=False),
        sa.Column("workspace_id", _ID, sa.ForeignKey("workspaces.id"), nullable=False),
        sa.Column("report_type", sa.String(40), nullable=False),
        sa.Column("params_json", sa.JSON(), nullable=True),
        sa.Column("payload_json", _LONG_TEXT, nullable=False),
        sa.Column("computed_at", sa.DateTime(), nullable=False),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_report_cache_expires", "report_cache", ["expires_at"])
    op.create_index("ix_report_cache_workspace", "report_cache", ["workspace_id"])

    op.create_table(
        "saved_reports",
        sa.Column("id", _ID, primary_key=True, nullable=False),
        sa.Column("workspace_id", _ID, sa.ForeignKey("workspaces.id"), nullable=False),
        sa.Column("owner_id", _ID, sa.ForeignKey("users.id"), nullable=False),
        sa.Column("name", sa.String(120), nullable=False),
        sa.Column("report_type", sa.String(40), nullable=False),
        sa.Column("params", sa.JSON(), nullable=True),
        sa.Column("visibility", sa.String(20), nullable=False, server_default="private"),
        sa.Column("pinned", sa.Boolean(), nullable=False, server_default=sa.text("0")),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.Column("deleted", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("deleted_at", sa.DateTime(), nullable=True),
    )
    op.create_index("ix_saved_reports_workspace_owner", "saved_reports",
                    ["workspace_id", "owner_id"])

    op.create_table(
        "report_schedules",
        sa.Column("id", _ID, primary_key=True, nullable=False),
        sa.Column("workspace_id", _ID, sa.ForeignKey("workspaces.id"), nullable=False),
        sa.Column("saved_report_id", _ID, sa.ForeignKey("saved_reports.id"), nullable=False),
        sa.Column("cadence", sa.String(20), nullable=False, server_default="weekly"),
        sa.Column("weekday", sa.Integer(), nullable=True),
        sa.Column("day_of_month", sa.Integer(), nullable=True),
        sa.Column("hour_local", sa.Integer(), nullable=False, server_default="9"),
        sa.Column("timezone", sa.String(64), nullable=False, server_default="UTC"),
        sa.Column("format", sa.String(10), nullable=False, server_default="pdf"),
        sa.Column("recipients", sa.JSON(), nullable=True),
        sa.Column("next_run_at", sa.DateTime(), nullable=False),
        sa.Column("last_run_at", sa.DateTime(), nullable=True),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.text("1")),
        sa.Column("created_by", _ID, sa.ForeignKey("users.id"), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.Column("deleted", sa.Integer(), nullable=False, server_default="0"),
    )
    op.create_index("ix_report_schedules_due", "report_schedules", ["enabled", "next_run_at"])
    op.create_index("ix_report_schedules_saved", "report_schedules", ["saved_report_id"])

    op.create_table(
        "report_jobs",
        sa.Column("id", _ID, primary_key=True, nullable=False),
        sa.Column("workspace_id", _ID, sa.ForeignKey("workspaces.id"), nullable=False),
        sa.Column("requested_by", _ID, sa.ForeignKey("users.id"), nullable=False),
        sa.Column("kind", sa.String(20), nullable=False, server_default="export"),
        sa.Column("report_type", sa.String(40), nullable=False),
        sa.Column("params", sa.JSON(), nullable=True),
        sa.Column("options", sa.JSON(), nullable=True),
        sa.Column("format", sa.String(10), nullable=False),
        sa.Column("schedule_id", _ID, sa.ForeignKey("report_schedules.id"), nullable=True),
        sa.Column("status", sa.String(20), nullable=False, server_default="queued"),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("file_blob", _LONG_BLOB, nullable=True),
        sa.Column("file_name", sa.String(200), nullable=True),
        sa.Column("content_type", sa.String(100), nullable=True),
        sa.Column("size_bytes", sa.Integer(), nullable=True),
        sa.Column("row_count", sa.Integer(), nullable=True),
        sa.Column("expires_at", sa.DateTime(), nullable=True),
        sa.Column("available_at", sa.DateTime(), nullable=False),
        sa.Column("lease_expires_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("started_at", sa.DateTime(), nullable=True),
        sa.Column("finished_at", sa.DateTime(), nullable=True),
    )
    op.create_index("ix_report_jobs_status_available", "report_jobs",
                    ["status", "available_at"])
    op.create_index("ix_report_jobs_workspace_requester", "report_jobs",
                    ["workspace_id", "requested_by", "created_at"])
    op.create_index("ix_report_jobs_expires", "report_jobs", ["expires_at"])


def downgrade() -> None:
    # Children first: jobs reference schedules, schedules reference saved reports.
    op.drop_table("report_jobs")
    op.drop_table("report_schedules")
    op.drop_table("saved_reports")
    op.drop_table("report_cache")
    op.drop_table("issue_snapshot_days")
    op.drop_table("issue_daily_status")
    op.drop_index("ix_issue_events_workspace_event_created", table_name="issue_events")
    op.drop_index("ix_issues_workspace_space_status", table_name="issues")
    with op.batch_alter_table("time_entries") as batch:
        batch.drop_column("billable")
