"""Role-based access control pre-check for the AI Chat Gateway.
Validates that the acting user holds sufficient workspace permissions before dispatching tool execution.
"""
from __future__ import annotations

from typing import Optional, Tuple

ADMIN_ONLY_TOOLS = {
    "space_create",
    "space_update",
    "space_archive",
    "space_freeze",
    "space_unfreeze",
    "folder_create",
    "folder_update",
    "folder_archive",
    "list_archive",
    "workspace_update",
    "workspace_transfer",
    "workspace_members_add",
    "workspace_members_remove",
    "workspace_members_role_update",
    "governance_settings_update",
    "workflow_scheme_update",
    "workflow_scheme_publish",
    "storage_config_get",
    "storage_config_set",
    "storage_config_test",
    "integration_github_install",
    "integration_github_uninstall",
    "integration_repo_link",
    "integration_repo_unlink",
    "reports_settings_update",
}


def check_tool_permission(
    tool_or_perm: str,
    user_role: str,  # owner, admin, member, viewer
    is_write: Optional[bool] = None,
    is_platform_admin: bool = False,
) -> Tuple[bool, Optional[str]]:
    """Pre-checks if caller's workspace role satisfies the tool's permission requirement.
    Returns (True, None) if authorized, or (False, error_message) if refused.
    """
    from ai_chat.registry import TOOL_REGISTRY

    role = (user_role or "guest").lower().strip()
    if is_platform_admin or role == "owner":
        return True, None

    tool = TOOL_REGISTRY.get(tool_or_perm)
    perm_desc = ""
    is_mutative = is_write

    if tool:
        perm_desc = (tool.description or "").lower()
        if is_mutative is None:
            is_mutative = tool.policy.tier in ("T1", "T2", "T3")
    else:
        perm_desc = tool_or_perm.lower()
        if is_mutative is None:
            is_mutative = False

    # 1. Viewers and guests can never perform mutative actions
    if is_mutative and role in ("viewer", "guest"):
        return (
            False,
            f"You currently have '{user_role.title()}' access in this workspace. Modifying or creating items requires at least 'Member' role.",
        )

    # 2. Admin-only tools check
    is_admin_tool = (
        tool_or_perm in ADMIN_ONLY_TOOLS
        or "workspace admin" in perm_desc
        or "admin only" in perm_desc
    )
    if is_admin_tool and role not in ("admin", "owner") and not is_platform_admin:
        return (
            False,
            f"This action requires Workspace Admin permissions. You currently have '{user_role.title()}' role in this workspace.",
        )

    # 3. Mutative actions require at least Member
    if is_mutative and role not in ("member", "admin", "owner") and not is_platform_admin:
        return (
            False,
            f"This action requires Workspace Member access. You currently have '{user_role.title()}' role.",
        )

    return True, None
