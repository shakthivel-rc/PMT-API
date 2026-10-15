from __future__ import annotations


#: The fine-grained permission slugs, as (display name, slug). `seeders/user_seeder.py`
#: creates every row here and grants all of them to the seeded Admin role.
#:
#: Colon-bearing slugs must be declared here, because legacy `POST /permissions` derived machine
#: names through `utils.common.to_snake_case`, which stripped colons.
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

# Coarse grants that stand in for a set of fine-grained ones. They exist so a role can be
# described in a sentence ("can manage users") without enumerating a dozen slugs, and so an
# existing role keeps working when a new fine-grained slug is added to a set it already
# implies.
LEGACY_PERMISSION_IMPLICATIONS: dict[str, set[str]] = {
    "manage_users": {
        "admin:read",
        "users:read",
        "users:create",
        "users:update",
        "users:disable",
        "users:delete",
        "roles:read",
        "roles:create",
        "roles:update",
        "roles:delete",
        "permissions:read",
        "permissions:manage",
        "audit:read",
    },
    "manage_workspaces": {
        "workspaces:read",
        "workspaces:create",
        "workspaces:update",
        "workspaces:delete",
        "workspaces:manage",
        "workspaces:restructure",
        "governance:read",
        "governance:manage",
        "spaces:read",
        "spaces:create",
        "spaces:update",
        "spaces:delete",
        "spaces:archive",
        "spaces:freeze",
        "folders:create",
        "folders:update",
        "folders:delete",
        "lists:create",
        "lists:update",
        "lists:delete",
    },
    "manage_issues": {
        "issues:read",
        "issues:create",
        "issues:update",
        "issues:delete",
        "issues:transition",
        "issues:assign",
        "issues:bulk",
        "comments:create",
        "comments:update",
        "comments:delete",
        "attachments:upload",
        "attachments:delete",
    },
    "manage_sprints": {
        "sprints:read",
        "sprints:create",
        "sprints:update",
        "sprints:delete",
        "sprints:start",
        "sprints:complete",
        "backlog:manage",
    },
    "manage_initiatives": {
        "initiatives:read",
        "initiatives:create",
        "initiatives:update",
        "initiatives:delete",
        "initiatives:link",
    },
    "manage_reports": {
        "view_reports",
        "reports:read",
        "reports:create",
        "reports:update",
        "reports:delete",
        "reports:export",
        "reports:schedule",
    },
    "manage_time": {
        "time:read",
        "time:log",
        "time:manage",
        "timesheets:approve",
    },
    "manage_workflows": {
        "workflow:read",
        "workflow:create",
        "workflow:update",
        "workflow:delete",
        "workflow:publish",
    },
}


def permission_is_satisfied(user_permissions: set[str], required_permission: str) -> bool:
    if required_permission in user_permissions:
        return True

    for legacy_permission, implied_permissions in LEGACY_PERMISSION_IMPLICATIONS.items():
        if legacy_permission in user_permissions and required_permission in implied_permissions:
            return True

    implied_permissions = LEGACY_PERMISSION_IMPLICATIONS.get(required_permission)
    if implied_permissions and implied_permissions.issubset(user_permissions):
        return True

    return False
