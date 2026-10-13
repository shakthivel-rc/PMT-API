from __future__ import annotations


#: The fine-grained permission slugs, as (display name, slug). `seeders/user_seeder.py`
#: creates every row here and grants all of them to the seeded Admin role.
#:
#: Colon-bearing slugs must be declared here, because `POST /permissions` derives machine
#: names through `utils.common.to_snake_case`, which strips colons: `users:read` created
#: through the API would arrive as `usersread`, which no route asks for.
TARGET_PERMISSION_CATALOG: tuple[tuple[str, str], ...] = (
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
