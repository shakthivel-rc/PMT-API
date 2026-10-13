"""Typed dependencies, releases with fix versions, and saved board filters.

Revision ID: 20261005g001
Revises: 20261005f001
Create Date: 2026-10-05

docs/design/09-restructuring.md §5.4 and §10. Four new tables, all empty; nothing existing
changes. `issue_links` holds `blocks`, `relates`, `duplicates` and `clones` between any two
issues of a workspace — the plan's typed dependencies, which is how cross-space coordination
is recorded without a cross-space parent.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "20261005g001"
down_revision: Union[str, None] = "20261005f001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_ID = sa.CHAR(36)


def upgrade() -> None:
    op.create_table(
        "issue_links",
        sa.Column("id", _ID, primary_key=True, nullable=False),
        sa.Column("workspace_id", _ID, sa.ForeignKey("workspaces.id"), nullable=False),
        sa.Column("source_issue_id", _ID, sa.ForeignKey("issues.id"), nullable=False),
        sa.Column("target_issue_id", _ID, sa.ForeignKey("issues.id"), nullable=False),
        sa.Column("kind", sa.String(20), nullable=False),
        sa.Column("created_by", _ID, sa.ForeignKey("users.id"), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("source_issue_id", "target_issue_id", "kind",
                            name="uq_issue_links_pair_kind"),
    )
    op.create_index("ix_issue_links_workspace_source", "issue_links",
                    ["workspace_id", "source_issue_id"])
    op.create_index("ix_issue_links_target", "issue_links", ["target_issue_id"])

    op.create_table(
        "releases",
        sa.Column("id", _ID, primary_key=True, nullable=False),
        sa.Column("workspace_id", _ID, sa.ForeignKey("workspaces.id"), nullable=False),
        sa.Column("space_id", _ID, sa.ForeignKey("spaces.id"), nullable=False),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("state", sa.String(20), nullable=False, server_default="unreleased"),
        sa.Column("start_date", sa.Date(), nullable=True),
        sa.Column("release_date", sa.Date(), nullable=True),
        sa.Column("released_at", sa.DateTime(), nullable=True),
        sa.Column("created_by", _ID, sa.ForeignKey("users.id"), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.Column("deleted", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("deleted_at", sa.DateTime(), nullable=True),
    )
    op.create_index("ix_releases_workspace_space_state", "releases",
                    ["workspace_id", "space_id", "state"])

    op.create_table(
        "issue_fix_versions",
        sa.Column("id", _ID, primary_key=True, nullable=False),
        sa.Column("workspace_id", _ID, sa.ForeignKey("workspaces.id"), nullable=False),
        sa.Column("issue_id", _ID, sa.ForeignKey("issues.id"), nullable=False),
        sa.Column("release_id", _ID, sa.ForeignKey("releases.id"), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("issue_id", "release_id", name="uq_issue_fix_versions_pair"),
    )
    op.create_index("ix_issue_fix_versions_release", "issue_fix_versions", ["release_id"])

    op.create_table(
        "saved_views",
        sa.Column("id", _ID, primary_key=True, nullable=False),
        sa.Column("workspace_id", _ID, sa.ForeignKey("workspaces.id"), nullable=False),
        sa.Column("owner_id", _ID, sa.ForeignKey("users.id"), nullable=False),
        sa.Column("name", sa.String(120), nullable=False),
        sa.Column("view_type", sa.String(20), nullable=False, server_default="board"),
        sa.Column("scope_kind", sa.String(20), nullable=False),
        sa.Column("scope_id", _ID, nullable=False),
        sa.Column("filters", sa.JSON(), nullable=True),
        sa.Column("visibility", sa.String(20), nullable=False, server_default="private"),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.Column("deleted", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("deleted_at", sa.DateTime(), nullable=True),
    )
    op.create_index("ix_saved_views_workspace_owner", "saved_views", ["workspace_id", "owner_id"])


def downgrade() -> None:
    op.drop_index("ix_saved_views_workspace_owner", table_name="saved_views")
    op.drop_table("saved_views")
    op.drop_index("ix_issue_fix_versions_release", table_name="issue_fix_versions")
    op.drop_table("issue_fix_versions")
    op.drop_index("ix_releases_workspace_space_state", table_name="releases")
    op.drop_table("releases")
    op.drop_index("ix_issue_links_target", table_name="issue_links")
    op.drop_index("ix_issue_links_workspace_source", table_name="issue_links")
    op.drop_table("issue_links")
