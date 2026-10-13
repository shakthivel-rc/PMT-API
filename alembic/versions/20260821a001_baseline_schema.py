"""Base Project baseline schema.

Revision ID: 20260821a001
Revises:
Create Date: 2026-08-21

The squashed baseline of the base project, and the single starting point for its database:
identity (`users`), RBAC (`roles`, `permissions`, `user_roles`, `role_permissions`),
server-side sessions (`user_sessions`) and the audit trail (`activity_logs`). A product built
on this base adds its own tables in revisions of its own on top of this one.

**Session tokens are stored only as SHA-256 hashes** — `access_token_hash CHAR(64)` and
`refresh_token_hash VARCHAR(128)`, both indexed — and never raw. A raw token at rest is a
bearer credential in every backup, replica and support query that ever reads the table, and
the lookup on every authenticated request needs an index: a utf8mb4 `VARCHAR(1024)` counts
as 4096 bytes against InnoDB's 3072-byte key limit (CLAUDE.md §17.5) and was not indexable,
while a 64-character hex digest is. SEC-5 and P-1 in `docs/production-readiness-plan.md`
are the two findings that shape closes.

**This is not an in-place upgrade.** This revision replaces the chain the project was
extracted from, so a database stamped by the old `jenkins-triage-agent` revisions cannot
upgrade from here (Alembic cannot locate its revision) and must be recreated, not migrated.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.mysql import CHAR


# revision identifiers, used by Alembic.
revision: str = "20260821a001"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ---- identity --------------------------------------------------------------------
    op.create_table(
        "users",
        sa.Column("id", CHAR(36), primary_key=True, nullable=False),
        sa.Column("email", sa.String(100), nullable=False, unique=True),
        sa.Column("username", sa.String(100), nullable=False, unique=True),
        sa.Column("first_name", sa.String(100), nullable=False),
        sa.Column("last_name", sa.String(100), nullable=False),
        sa.Column("organization", sa.String(100), nullable=False),
        sa.Column("department", sa.String(100), nullable=False),
        sa.Column("password", sa.String(512), nullable=True),
        sa.Column("status", sa.Boolean(), nullable=False, server_default=sa.text("0")),
        sa.Column("fp_token", sa.String(255), nullable=True),
        sa.Column("fp_token_created_at", sa.DateTime(), nullable=True),
        sa.Column("verify_token", sa.String(255), nullable=True),
        sa.Column("otp_code", sa.String(255), nullable=True),
        sa.Column("otp_created_at", sa.DateTime(), nullable=True),
        sa.Column("failed_login_attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("locked_until", sa.DateTime(), nullable=True),
        sa.Column("last_failed_login_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.Column("deleted_at", sa.DateTime(), nullable=True),
        sa.Column("deleted", sa.Integer(), nullable=False, server_default="0"),
    )
    op.create_index("ix_users_id", "users", ["id"])

    op.create_table(
        "roles",
        sa.Column("id", CHAR(36), primary_key=True, nullable=False),
        sa.Column("name", sa.String(50), nullable=False, unique=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.Column("deleted_at", sa.DateTime(), nullable=True),
        sa.Column("deleted", sa.Integer(), nullable=False, server_default="0"),
    )
    op.create_index("ix_roles_id", "roles", ["id"])

    op.create_table(
        "permissions",
        sa.Column("id", CHAR(36), primary_key=True, nullable=False),
        # 512 with a unique index needs utf8mb4 key-length room; MySQL's 3072-byte index
        # limit covers it on InnoDB with DYNAMIC row format, which is the default from 5.7.
        sa.Column("name", sa.String(512), nullable=False, unique=True),
        sa.Column("machine_name", sa.String(512), nullable=False, unique=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.Column("deleted_at", sa.DateTime(), nullable=True),
        sa.Column("deleted", sa.Integer(), nullable=False, server_default="0"),
    )
    op.create_index("ix_permissions_id", "permissions", ["id"])

    op.create_table(
        "user_roles",
        sa.Column("id", CHAR(36), primary_key=True, nullable=False),
        sa.Column("user_id", CHAR(36), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("role_id", CHAR(36), sa.ForeignKey("roles.id"), nullable=False),
    )
    op.create_index("ix_user_roles_id", "user_roles", ["id"])

    op.create_table(
        "role_permissions",
        sa.Column("id", CHAR(36), primary_key=True, nullable=False),
        sa.Column("role_id", CHAR(36), sa.ForeignKey("roles.id"), nullable=False),
        sa.Column("permission_id", CHAR(36), sa.ForeignKey("permissions.id"), nullable=False),
    )
    op.create_index("ix_role_permissions_id", "role_permissions", ["id"])

    op.create_table(
        "user_sessions",
        sa.Column("id", sa.String(36), primary_key=True, nullable=False),
        sa.Column("user_id", sa.String(36), nullable=False),
        # CHAR(64), not VARCHAR: a hex SHA-256 digest is exactly 64 characters, every time,
        # so the length prefix a VARCHAR carries would store nothing.
        sa.Column("access_token_hash", sa.CHAR(64), nullable=False),
        sa.Column("refresh_token_hash", sa.String(128), nullable=True),
        sa.Column("refresh_token_expires_at", sa.DateTime(), nullable=True),
        sa.Column("revoked_at", sa.DateTime(), nullable=True),
        sa.Column("user_agent", sa.String(512), nullable=True),
        sa.Column("ip_address", sa.String(45), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_user_sessions_user_id", "user_sessions", ["user_id"])
    # Refresh rotation looks a token up by hash on every refresh; without this it is a
    # full scan of every session ever issued.
    op.create_index("ix_user_sessions_refresh_token_hash", "user_sessions", ["refresh_token_hash"])
    # `routes/auth_middleware.assert_session_is_live` looks a session up by this hash on
    # every authenticated request, which makes it the highest-frequency query in the
    # service. Named to match what `Column(..., index=True)` on the model generates, so a
    # database built by `create_all` (the test harness) and one built by this revision
    # (production) describe the same index under the same name.
    op.create_index("ix_user_sessions_access_token_hash", "user_sessions", ["access_token_hash"])

    op.create_table(
        "activity_logs",
        sa.Column("id", CHAR(36), primary_key=True, nullable=False),
        sa.Column("user_id", CHAR(36), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("user_name", sa.String(200), nullable=True),
        sa.Column("action", sa.String(50), nullable=False),
        sa.Column("entity_type", sa.String(50), nullable=True),
        sa.Column("entity_id", CHAR(36), nullable=True),
        sa.Column("details", sa.Text(), nullable=True),
        sa.Column("ip_address", sa.String(45), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_activity_logs_id", "activity_logs", ["id"])
    # The log is always read newest-first and usually filtered by user.
    op.create_index("ix_activity_logs_user_created", "activity_logs", ["user_id", "created_at"])


def downgrade() -> None:
    # Children before parents, so no foreign key is ever left pointing at a dropped table.
    # Dropping a table drops its indexes with it.
    op.drop_table("activity_logs")
    op.drop_table("user_sessions")
    op.drop_table("role_permissions")
    op.drop_table("user_roles")
    op.drop_table("permissions")
    op.drop_table("roles")
    op.drop_table("users")
