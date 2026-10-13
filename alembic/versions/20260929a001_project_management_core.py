"""Project management core: tenancy, hierarchy, issues, VCS links and the AI job queue.

Revision ID: 20260929a001
Revises: 20260821a001
Create Date: 2026-09-29

Phase 1 of docs/design/06-phase-1-plan.md. Adds the tenant (`workspaces`,
`workspace_members`), the hierarchy (`spaces`, `folders`, `lists`) and each space's
workflow (`workflow_statuses`), the issue tracker (`issues`, `issue_comments`,
`issue_events`), inbound GitHub (`vcs_repositories`, `vcs_links`, `webhook_deliveries`) and
the table-backed AI work queue (`ai_jobs`).

**Portable on purpose.** Every type here exists on SQLite (the test harness), MySQL 8 (what
the base ships on) and PostgreSQL 16 (where docs/design/05-tech-stack.md moves it). The
PostgreSQL-only parts of the target schema — row-level security, JSONB custom fields,
pgvector embeddings, partitioned event tables — are in docs/design/schema.sql and arrive in
the revision that follows the cut-over, where they can be written without a dialect switch
in every line.

Index names are written out rather than left to the naming convention so that
`create_all` (the models) and this revision produce identical names; the models declare
the same ones.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "20260929a001"
down_revision: Union[str, None] = "20260821a001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_ID = sa.CHAR(36)


def upgrade() -> None:
    # ---- tenancy ---------------------------------------------------------------------
    op.create_table(
        "workspaces",
        sa.Column("id", _ID, primary_key=True, nullable=False),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("slug", sa.String(50), nullable=False, unique=True),
        sa.Column("created_by", _ID, sa.ForeignKey("users.id"), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.Column("deleted", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("deleted_at", sa.DateTime(), nullable=True),
    )

    op.create_table(
        "workspace_members",
        sa.Column("id", _ID, primary_key=True, nullable=False),
        sa.Column("workspace_id", _ID, sa.ForeignKey("workspaces.id"), nullable=False),
        sa.Column("user_id", _ID, sa.ForeignKey("users.id"), nullable=False),
        sa.Column("role", sa.String(20), nullable=False, server_default="member"),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("workspace_id", "user_id",
                            name="uq_workspace_members_workspace_user"),
    )
    op.create_index("ix_workspace_members_user_id", "workspace_members", ["user_id"])

    # ---- hierarchy -------------------------------------------------------------------
    op.create_table(
        "spaces",
        sa.Column("id", _ID, primary_key=True, nullable=False),
        sa.Column("workspace_id", _ID, sa.ForeignKey("workspaces.id"), nullable=False),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("key", sa.String(10), nullable=False),
        sa.Column("issue_counter", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.Column("deleted", sa.Integer(), nullable=False, server_default="0"),
        sa.UniqueConstraint("workspace_id", "key", name="uq_spaces_workspace_key"),
    )
    op.create_index("ix_spaces_workspace_id", "spaces", ["workspace_id"])

    op.create_table(
        "folders",
        sa.Column("id", _ID, primary_key=True, nullable=False),
        sa.Column("workspace_id", _ID, sa.ForeignKey("workspaces.id"), nullable=False),
        sa.Column("space_id", _ID, sa.ForeignKey("spaces.id"), nullable=False),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("deleted", sa.Integer(), nullable=False, server_default="0"),
    )
    op.create_index("ix_folders_space_id", "folders", ["space_id"])

    op.create_table(
        "lists",
        sa.Column("id", _ID, primary_key=True, nullable=False),
        sa.Column("workspace_id", _ID, sa.ForeignKey("workspaces.id"), nullable=False),
        sa.Column("space_id", _ID, sa.ForeignKey("spaces.id"), nullable=False),
        sa.Column("folder_id", _ID, sa.ForeignKey("folders.id"), nullable=True),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("deleted", sa.Integer(), nullable=False, server_default="0"),
    )
    op.create_index("ix_lists_space_id", "lists", ["space_id"])

    op.create_table(
        "workflow_statuses",
        sa.Column("id", _ID, primary_key=True, nullable=False),
        sa.Column("workspace_id", _ID, sa.ForeignKey("workspaces.id"), nullable=False),
        sa.Column("space_id", _ID, sa.ForeignKey("spaces.id"), nullable=False),
        sa.Column("name", sa.String(50), nullable=False),
        sa.Column("category", sa.String(20), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_workflow_statuses_space_position", "workflow_statuses",
                    ["space_id", "position"])

    # ---- issues ----------------------------------------------------------------------
    op.create_table(
        "issues",
        sa.Column("id", _ID, primary_key=True, nullable=False),
        sa.Column("workspace_id", _ID, sa.ForeignKey("workspaces.id"), nullable=False),
        sa.Column("space_id", _ID, sa.ForeignKey("spaces.id"), nullable=False),
        sa.Column("list_id", _ID, sa.ForeignKey("lists.id"), nullable=False),
        sa.Column("number", sa.Integer(), nullable=False),
        sa.Column("type", sa.String(20), nullable=False, server_default="task"),
        sa.Column("title", sa.String(500), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("status_id", _ID, sa.ForeignKey("workflow_statuses.id"), nullable=False),
        sa.Column("parent_id", _ID, sa.ForeignKey("issues.id"), nullable=True),
        sa.Column("assignee_id", _ID, sa.ForeignKey("users.id"), nullable=True),
        sa.Column("reporter_id", _ID, sa.ForeignKey("users.id"), nullable=False),
        sa.Column("priority", sa.String(10), nullable=False, server_default="none"),
        sa.Column("story_points", sa.Integer(), nullable=True),
        sa.Column("rank", sa.String(128), nullable=False),
        sa.Column("start_date", sa.Date(), nullable=True),
        sa.Column("due_date", sa.Date(), nullable=True),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.Column("deleted", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("deleted_at", sa.DateTime(), nullable=True),
        sa.UniqueConstraint("space_id", "number", name="uq_issues_space_number"),
    )
    op.create_index("ix_issues_workspace_list_status_rank", "issues",
                    ["workspace_id", "list_id", "status_id", "rank"])
    op.create_index("ix_issues_space_status_rank", "issues", ["space_id", "status_id", "rank"])
    op.create_index("ix_issues_workspace_assignee", "issues", ["workspace_id", "assignee_id"])
    op.create_index("ix_issues_parent_id", "issues", ["parent_id"])

    op.create_table(
        "issue_comments",
        sa.Column("id", _ID, primary_key=True, nullable=False),
        sa.Column("workspace_id", _ID, sa.ForeignKey("workspaces.id"), nullable=False),
        sa.Column("issue_id", _ID, sa.ForeignKey("issues.id"), nullable=False),
        sa.Column("author_id", _ID, sa.ForeignKey("users.id"), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.Column("deleted", sa.Integer(), nullable=False, server_default="0"),
    )
    op.create_index("ix_issue_comments_issue_created", "issue_comments",
                    ["issue_id", "created_at"])

    op.create_table(
        "issue_events",
        sa.Column("id", _ID, primary_key=True, nullable=False),
        sa.Column("workspace_id", _ID, sa.ForeignKey("workspaces.id"), nullable=False),
        sa.Column("issue_id", _ID, sa.ForeignKey("issues.id"), nullable=False),
        sa.Column("actor_id", _ID, sa.ForeignKey("users.id"), nullable=True),
        sa.Column("actor_kind", sa.String(20), nullable=False, server_default="user"),
        sa.Column("event", sa.String(40), nullable=False),
        sa.Column("from_value", sa.String(255), nullable=True),
        sa.Column("to_value", sa.String(255), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_issue_events_issue_created", "issue_events", ["issue_id", "created_at"])
    op.create_index("ix_issue_events_workspace_created", "issue_events",
                    ["workspace_id", "created_at"])

    # ---- version control -------------------------------------------------------------
    op.create_table(
        "vcs_repositories",
        sa.Column("id", _ID, primary_key=True, nullable=False),
        sa.Column("workspace_id", _ID, sa.ForeignKey("workspaces.id"), nullable=False),
        sa.Column("provider", sa.String(20), nullable=False, server_default="github"),
        sa.Column("full_name", sa.String(200), nullable=False),
        sa.Column("created_by", _ID, sa.ForeignKey("users.id"), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("provider", "full_name", name="uq_vcs_repositories_provider_name"),
    )
    op.create_index("ix_vcs_repositories_workspace_id", "vcs_repositories", ["workspace_id"])

    op.create_table(
        "vcs_links",
        sa.Column("id", _ID, primary_key=True, nullable=False),
        sa.Column("workspace_id", _ID, sa.ForeignKey("workspaces.id"), nullable=False),
        sa.Column("issue_id", _ID, sa.ForeignKey("issues.id"), nullable=False),
        sa.Column("repository_id", _ID, sa.ForeignKey("vcs_repositories.id"), nullable=False),
        sa.Column("provider", sa.String(20), nullable=False, server_default="github"),
        sa.Column("kind", sa.String(20), nullable=False),
        sa.Column("external_id", sa.String(255), nullable=False),
        sa.Column("url", sa.String(500), nullable=False),
        sa.Column("title", sa.String(500), nullable=True),
        sa.Column("state", sa.String(20), nullable=True),
        sa.Column("author_login", sa.String(100), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("issue_id", "provider", "kind", "external_id",
                            name="uq_vcs_links_issue_external"),
    )
    op.create_index("ix_vcs_links_issue_id", "vcs_links", ["issue_id"])

    op.create_table(
        "webhook_deliveries",
        sa.Column("id", sa.String(64), primary_key=True, nullable=False),
        sa.Column("provider", sa.String(20), nullable=False),
        sa.Column("event", sa.String(50), nullable=False),
        sa.Column("received_at", sa.DateTime(), nullable=False),
    )

    # ---- AI work queue ---------------------------------------------------------------
    op.create_table(
        "ai_jobs",
        sa.Column("id", _ID, primary_key=True, nullable=False),
        sa.Column("workspace_id", _ID, sa.ForeignKey("workspaces.id"), nullable=False),
        sa.Column("kind", sa.String(40), nullable=False),
        sa.Column("subject_type", sa.String(20), nullable=False, server_default="issue"),
        sa.Column("subject_id", _ID, nullable=False),
        sa.Column("status", sa.String(20), nullable=False, server_default="queued"),
        sa.Column("input_hash", sa.String(64), nullable=True),
        sa.Column("prompt_version", sa.String(40), nullable=True),
        sa.Column("model", sa.String(60), nullable=True),
        sa.Column("result", sa.JSON(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("input_tokens", sa.Integer(), nullable=True),
        sa.Column("output_tokens", sa.Integer(), nullable=True),
        sa.Column("requested_by", _ID, sa.ForeignKey("users.id"), nullable=False),
        sa.Column("available_at", sa.DateTime(), nullable=False),
        sa.Column("lease_expires_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("started_at", sa.DateTime(), nullable=True),
        sa.Column("finished_at", sa.DateTime(), nullable=True),
    )
    op.create_index("ix_ai_jobs_status_available", "ai_jobs", ["status", "available_at"])
    op.create_index("ix_ai_jobs_subject", "ai_jobs", ["workspace_id", "subject_id", "kind"])


def downgrade() -> None:
    # Children before parents, so every foreign key is gone before the table it points at.
    # `drop_table` takes the table's indexes with it on all three dialects.
    for table in (
        "ai_jobs",
        "webhook_deliveries",
        "vcs_links",
        "vcs_repositories",
        "issue_events",
        "issue_comments",
        "issues",
        "workflow_statuses",
        "lists",
        "folders",
        "spaces",
        "workspace_members",
        "workspaces",
    ):
        op.drop_table(table)
