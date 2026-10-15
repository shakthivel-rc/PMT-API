"""Seed comprehensive RBAC permissions and default roles.

Revision ID: 20261128a001
Revises: 20261125a001
Create Date: 2026-11-28
"""
from typing import Sequence, Union
import datetime
import uuid

from alembic import op
import sqlalchemy as sa

revision: str = "20261128a001"
down_revision: Union[str, None] = "20261125a001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# Comprehensive component permission catalogue
TARGET_PERMISSION_CATALOG: tuple[tuple[str, str], ...] = (
    # --- System Administration & RBAC ---
    ("Sessions Read", "sessions:read"),
    ("Sessions Revoke", "sessions:revoke"),
    ("Users Read", "users:read"),
    ("Users Create", "users:create"),
    ("Users Update", "users:update"),
    ("Users Disable", "users:disable"),
    ("Users Delete", "users:delete"),
    ("Roles Read", "roles:read"),
    ("Roles Create", "roles:create"),
    ("Roles Update", "roles:update"),
    ("Roles Delete", "roles:delete"),
    ("Permissions Read", "permissions:read"),
    ("Permissions Manage", "permissions:manage"),
    ("Audit Read", "audit:read"),
    ("Admin Read", "admin:read"),

    # --- Workspaces & Project Governance ---
    ("Workspaces Read", "workspaces:read"),
    ("Workspaces Create", "workspaces:create"),
    ("Workspaces Update", "workspaces:update"),
    ("Workspaces Delete", "workspaces:delete"),
    ("Workspaces Manage", "workspaces:manage"),
    ("Workspaces Restructure", "workspaces:restructure"),
    ("Governance Read", "governance:read"),
    ("Governance Manage", "governance:manage"),

    # --- Spaces, Folders & Lists ---
    ("Spaces Read", "spaces:read"),
    ("Spaces Create", "spaces:create"),
    ("Spaces Update", "spaces:update"),
    ("Spaces Delete", "spaces:delete"),
    ("Spaces Archive", "spaces:archive"),
    ("Spaces Freeze", "spaces:freeze"),
    ("Folders Create", "folders:create"),
    ("Folders Update", "folders:update"),
    ("Folders Delete", "folders:delete"),
    ("Lists Create", "lists:create"),
    ("Lists Update", "lists:update"),
    ("Lists Delete", "lists:delete"),

    # --- Issues, Epics, Tasks & Comments ---
    ("Issues Read", "issues:read"),
    ("Issues Create", "issues:create"),
    ("Issues Update", "issues:update"),
    ("Issues Delete", "issues:delete"),
    ("Issues Transition", "issues:transition"),
    ("Issues Assign", "issues:assign"),
    ("Issues Bulk Operations", "issues:bulk"),
    ("Comments Create", "comments:create"),
    ("Comments Update", "comments:update"),
    ("Comments Delete", "comments:delete"),
    ("Attachments Upload", "attachments:upload"),
    ("Attachments Delete", "attachments:delete"),

    # --- Sprints & Backlog ---
    ("Sprints Read", "sprints:read"),
    ("Sprints Create", "sprints:create"),
    ("Sprints Update", "sprints:update"),
    ("Sprints Delete", "sprints:delete"),
    ("Sprints Start", "sprints:start"),
    ("Sprints Complete", "sprints:complete"),
    ("Backlog Manage", "backlog:manage"),

    # --- Workflow Schemes & Custom Stages ---
    ("Workflow Read", "workflow:read"),
    ("Workflow Create", "workflow:create"),
    ("Workflow Update", "workflow:update"),
    ("Workflow Delete", "workflow:delete"),
    ("Workflow Publish", "workflow:publish"),

    # --- Cross-Space Portfolio Initiatives ---
    ("Initiatives Read", "initiatives:read"),
    ("Initiatives Create", "initiatives:create"),
    ("Initiatives Update", "initiatives:update"),
    ("Initiatives Delete", "initiatives:delete"),
    ("Initiatives Link Items", "initiatives:link"),

    # --- Time Tracking & Timesheets ---
    ("Time Read", "time:read"),
    ("Time Log", "time:log"),
    ("Time Manage", "time:manage"),
    ("Timesheets Approve", "timesheets:approve"),

    # --- Reports, Dashboards & Exports ---
    ("View Reports", "view_reports"),
    ("Reports Read", "reports:read"),
    ("Reports Create", "reports:create"),
    ("Reports Update", "reports:update"),
    ("Reports Delete", "reports:delete"),
    ("Reports Export", "reports:export"),
    ("Reports Schedule", "reports:schedule"),

    # --- Releases & Deployments ---
    ("Releases Read", "releases:read"),
    ("Releases Create", "releases:create"),
    ("Releases Update", "releases:update"),
    ("Releases Delete", "releases:delete"),

    # --- AI Chat & Autonomous Agents ---
    ("AI Chat Access", "ai:chat"),
    ("AI Settings", "ai:settings"),
    ("AI Autonomous Agents", "ai:agents"),

    # --- Cloud Storage & Integrations ---
    ("Storage Read", "storage:read"),
    ("Storage Manage", "storage:manage"),
    ("Integrations Read", "integrations:read"),
    ("Integrations Manage", "integrations:manage"),
    ("Webhooks Manage", "webhooks:manage"),

    # --- Planning Poker & Estimations ---
    ("Poker Participate", "poker:participate"),
    ("Poker Manage", "poker:manage"),

    # --- Timeline & Roadmaps ---
    ("Timeline Read", "timeline:read"),
    ("Timeline Manage", "timeline:manage"),

    # --- Notifications & Activity Logs ---
    ("Notifications Read", "notifications:read"),
    ("Activity Log Read", "activity:read"),
)

DEFAULT_ROLES: dict[str, tuple[str, ...]] = {
    "Project Manager": (
        "workspaces:read", "workspaces:update", "governance:read",
        "spaces:read", "spaces:create", "spaces:update", "spaces:archive", "spaces:freeze",
        "folders:create", "folders:update", "folders:delete",
        "lists:create", "lists:update", "lists:delete",
        "issues:read", "issues:create", "issues:update", "issues:delete", "issues:transition", "issues:assign", "issues:bulk",
        "comments:create", "comments:update", "comments:delete",
        "attachments:upload", "attachments:delete",
        "sprints:read", "sprints:create", "sprints:update", "sprints:delete", "sprints:start", "sprints:complete", "backlog:manage",
        "workflow:read", "workflow:create", "workflow:update",
        "initiatives:read", "initiatives:create", "initiatives:update", "initiatives:link",
        "time:read", "time:log", "time:manage", "timesheets:approve",
        "view_reports", "reports:read", "reports:create", "reports:update", "reports:export", "reports:schedule",
        "releases:read", "releases:create", "releases:update",
        "ai:chat", "poker:participate", "poker:manage",
        "timeline:read", "timeline:manage",
        "notifications:read", "activity:read",
        "storage:read", "integrations:read",
    ),
    "Product Owner": (
        "workspaces:read", "governance:read",
        "spaces:read", "spaces:create", "spaces:update",
        "folders:create", "folders:update",
        "lists:create", "lists:update",
        "issues:read", "issues:create", "issues:update", "issues:transition", "issues:assign",
        "comments:create", "comments:update",
        "attachments:upload",
        "sprints:read", "sprints:create", "sprints:update", "sprints:start", "sprints:complete", "backlog:manage",
        "workflow:read",
        "initiatives:read", "initiatives:create", "initiatives:update", "initiatives:delete", "initiatives:link",
        "time:read", "time:log",
        "view_reports", "reports:read", "reports:create", "reports:export",
        "releases:read", "releases:create", "releases:update",
        "ai:chat", "poker:participate", "poker:manage",
        "timeline:read", "timeline:manage",
        "notifications:read", "activity:read",
    ),
    "Developer": (
        "workspaces:read",
        "spaces:read",
        "issues:read", "issues:create", "issues:update", "issues:transition",
        "comments:create", "comments:update",
        "attachments:upload",
        "sprints:read",
        "workflow:read",
        "initiatives:read",
        "time:read", "time:log",
        "releases:read",
        "ai:chat", "poker:participate",
        "timeline:read",
        "notifications:read", "activity:read",
        "storage:read", "integrations:read",
    ),
    "QA Engineer": (
        "workspaces:read",
        "spaces:read",
        "issues:read", "issues:create", "issues:update", "issues:transition",
        "comments:create", "comments:update",
        "attachments:upload",
        "sprints:read",
        "workflow:read",
        "time:read", "time:log",
        "view_reports", "reports:read", "reports:export",
        "releases:read",
        "ai:chat", "poker:participate",
        "timeline:read",
        "notifications:read", "activity:read",
    ),
    "Stakeholder": (
        "workspaces:read",
        "spaces:read",
        "issues:read",
        "sprints:read",
        "initiatives:read",
        "view_reports", "reports:read",
        "timeline:read",
        "notifications:read", "activity:read",
    ),
    "Finance Manager": (
        "workspaces:read", "governance:read",
        "time:read", "time:manage", "timesheets:approve",
        "view_reports", "reports:read", "reports:create", "reports:export", "reports:schedule",
        "notifications:read", "activity:read",
    ),
}


def upgrade() -> None:
    conn = op.get_bind()
    now = datetime.datetime.utcnow()

    # 1. Fetch existing permissions
    existing_perms = conn.execute(sa.text("SELECT id, machine_name FROM permissions")).fetchall()
    perm_map = {row[1]: row[0] for row in existing_perms}

    # Insert missing permissions
    for name, slug in TARGET_PERMISSION_CATALOG:
        if slug not in perm_map:
            perm_id = str(uuid.uuid4())
            conn.execute(
                sa.text(
                    "INSERT INTO permissions (id, name, machine_name, created_at, updated_at, deleted) "
                    "VALUES (:id, :name, :machine_name, :created_at, :updated_at, 0)"
                ),
                {
                    "id": perm_id,
                    "name": name,
                    "machine_name": slug,
                    "created_at": now,
                    "updated_at": now,
                },
            )
            perm_map[slug] = perm_id

    # 2. Fetch existing roles
    existing_roles = conn.execute(sa.text("SELECT id, name FROM roles")).fetchall()
    role_map = {row[1]: row[0] for row in existing_roles}

    # Ensure Admin and Member roles exist
    if "Admin" not in role_map:
        admin_id = str(uuid.uuid4())
        conn.execute(
            sa.text(
                "INSERT INTO roles (id, name, created_at, updated_at, deleted) "
                "VALUES (:id, 'Admin', :created_at, :updated_at, 0)"
            ),
            {"id": admin_id, "created_at": now, "updated_at": now},
        )
        role_map["Admin"] = admin_id

    if "Member" not in role_map:
        member_id = str(uuid.uuid4())
        conn.execute(
            sa.text(
                "INSERT INTO roles (id, name, created_at, updated_at, deleted) "
                "VALUES (:id, 'Member', :created_at, :updated_at, 0)"
            ),
            {"id": member_id, "created_at": now, "updated_at": now},
        )
        role_map["Member"] = member_id

    # Create additional functional roles
    for role_name in DEFAULT_ROLES:
        if role_name not in role_map:
            new_role_id = str(uuid.uuid4())
            conn.execute(
                sa.text(
                    "INSERT INTO roles (id, name, created_at, updated_at, deleted) "
                    "VALUES (:id, :name, :created_at, :updated_at, 0)"
                ),
                {"id": new_role_id, "name": role_name, "created_at": now, "updated_at": now},
            )
            role_map[role_name] = new_role_id

    # 3. Existing role-permission assignments
    existing_role_perms = conn.execute(
        sa.text("SELECT role_id, permission_id FROM role_permissions")
    ).fetchall()
    role_perm_set = {(row[0], row[1]) for row in existing_role_perms}

    # Grant ALL permissions to Admin
    admin_id = role_map["Admin"]
    all_perm_ids = conn.execute(sa.text("SELECT id FROM permissions")).fetchall()
    for (pid,) in all_perm_ids:
        if (admin_id, pid) not in role_perm_set:
            conn.execute(
                sa.text(
                    "INSERT INTO role_permissions (id, role_id, permission_id) "
                    "VALUES (:id, :role_id, :permission_id)"
                ),
                {"id": str(uuid.uuid4()), "role_id": admin_id, "permission_id": pid},
            )
            role_perm_set.add((admin_id, pid))

    # Grant mapped permissions to functional roles
    for role_name, perms in DEFAULT_ROLES.items():
        role_id = role_map.get(role_name)
        if not role_id:
            continue
        for slug in perms:
            pid = perm_map.get(slug)
            if pid and (role_id, pid) not in role_perm_set:
                conn.execute(
                    sa.text(
                        "INSERT INTO role_permissions (id, role_id, permission_id) "
                        "VALUES (:id, :role_id, :permission_id)"
                    ),
                    {"id": str(uuid.uuid4()), "role_id": role_id, "permission_id": pid},
                )
                role_perm_set.add((role_id, pid))


def downgrade() -> None:
    pass
