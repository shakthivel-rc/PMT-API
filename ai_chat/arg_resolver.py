"""Tool argument resolution layer.
Resolves human references ($member:Name, $space:Key, $list:Path, $sprint:Name, $issue:Key, $self, names)
to canonical IDs before calling tool handlers.
"""
from __future__ import annotations

from typing import Any, Optional, Tuple

from sqlalchemy.orm import Session

from ai_chat.resolvers import (
    ResolutionResult,
    is_valid_uuid,
    resolve_issue,
    resolve_list,
    resolve_member,
    resolve_space,
    resolve_sprint,
)


def resolve_tool_arguments(
    db: Session,
    workspace_id: str,
    caller_id: str,
    tool_name: str,
    args: dict[str, Any],
) -> Tuple[dict[str, Any], Optional[str], Optional[list[dict[str, Any]]]]:
    """Inspects and resolves parameters for a tool.
    Returns:
        (resolved_args, error_message, disambiguation_candidates)
    """
    resolved = dict(args)

    # 1. Resolve space_id / project_id first if present
    space_id_val = None
    for key in ("space_id", "project_id"):
        if key in resolved and resolved[key]:
            val = resolved[key]
            if not is_valid_uuid(val):
                res = resolve_space(db, workspace_id, val)
                if not res.success:
                    if res.is_ambiguous:
                        return resolved, res.error, res.candidates
                    return resolved, res.error, None
                resolved[key] = res.value
                space_id_val = res.value
            else:
                space_id_val = val

    # 2. Resolve list_id
    if "list_id" in resolved:
        val = resolved.get("list_id")
        if val and not is_valid_uuid(val):
            res = resolve_list(db, workspace_id, space_id_val, val)
            if not res.success:
                if res.is_ambiguous:
                    return resolved, res.error, res.candidates
                return resolved, res.error, None
            resolved["list_id"] = res.value
        elif not val and space_id_val:
            # Rule 3: Auto-select single list if space has exactly one
            res = resolve_list(db, workspace_id, space_id_val, None)
            if res.success:
                resolved["list_id"] = res.value

    # 3. Resolve sprint_id
    if "sprint_id" in resolved and resolved["sprint_id"]:
        val = resolved["sprint_id"]
        if not is_valid_uuid(val):
            res = resolve_sprint(db, workspace_id, space_id_val, val)
            if not res.success:
                if res.is_ambiguous:
                    return resolved, res.error, res.candidates
                return resolved, res.error, None
            resolved["sprint_id"] = res.value

    # 4. Resolve user / member references
    for key in ("user_id", "assignee_id", "member_id", "owner_id", "reporter_id", "actor_id"):
        if key in resolved and resolved[key] is not None:
            val = resolved[key]
            val_str = str(val).strip()
            # If explicit reference, self reference, or non-UUID string
            if val_str.startswith("$") or not is_valid_uuid(val_str):
                res = resolve_member(db, workspace_id, val, caller_id=caller_id)
                if not res.success:
                    if res.is_ambiguous:
                        return resolved, res.error, res.candidates
                    return resolved, res.error, None
                resolved[key] = res.value

    # 5. Resolve issue references
    for key in ("issue_id", "parent_id", "target_issue_id", "source_issue_id", "blocking_issue_id"):
        if key in resolved and resolved[key] is not None:
            val = resolved[key]
            val_str = str(val).strip()
            if val_str.startswith("$") or not is_valid_uuid(val_str):
                res = resolve_issue(db, workspace_id, val)
                if not res.success:
                    if res.is_ambiguous:
                        return resolved, res.error, res.candidates
                    return resolved, res.error, None
                resolved[key] = res.value

    # 6. Any other general $kind: value in arguments
    for k, v in list(resolved.items()):
        if isinstance(v, str):
            if v.startswith("$member:") or v in ("$self", "self"):
                res = resolve_member(db, workspace_id, v, caller_id=caller_id)
                if res.success:
                    resolved[k] = res.value
                elif res.is_ambiguous:
                    return resolved, res.error, res.candidates
            elif v.startswith("$space:"):
                res = resolve_space(db, workspace_id, v)
                if res.success:
                    resolved[k] = res.value
            elif v.startswith("$list:"):
                res = resolve_list(db, workspace_id, space_id_val, v)
                if res.success:
                    resolved[k] = res.value
            elif v.startswith("$sprint:"):
                res = resolve_sprint(db, workspace_id, space_id_val, v)
                if res.success:
                    resolved[k] = res.value
            elif v.startswith("$issue:"):
                res = resolve_issue(db, workspace_id, v)
                if res.success:
                    resolved[k] = res.value

    return resolved, None, None
