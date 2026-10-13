"""Resolvers for human-friendly references (space names, list names, sprint numbers,
member names, issue keys) into canonical database UUIDs / IDs, scoped strictly to the
active workspace.
"""
from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, field
from typing import Any, Optional

from sqlalchemy import and_, func, or_
from sqlalchemy.orm import Session

from models.hierarchy import Folder, ProjectList, Space
from models.issue import Issue
from models.sprint import Sprint
from models.user import User
from models.workspace import WorkspaceMember


@dataclass
class ResolutionResult:
    success: bool
    value: Any = None
    entity: Optional[Any] = None
    candidates: list[dict[str, Any]] = field(default_factory=list)
    is_ambiguous: bool = False
    error: Optional[str] = None


def is_valid_uuid(val: Any) -> bool:
    if not isinstance(val, str):
        return False
    try:
        uuid.UUID(val)
        return True
    except (ValueError, AttributeError):
        return False


def resolve_space(db: Session, workspace_id: str, query: str | None) -> ResolutionResult:
    if not query:
        return ResolutionResult(success=False, error="Space identifier is required.")

    query_str = str(query).strip()

    # If already a valid UUID
    if is_valid_uuid(query_str):
        space = db.query(Space).filter(
            Space.workspace_id == workspace_id,
            Space.id == query_str,
            Space.deleted == 0,
        ).first()
        if space:
            return ResolutionResult(success=True, value=space.id, entity=space)

    # Clean query (remove leading '$space:' if present)
    clean_query = query_str
    if clean_query.startswith("$space:"):
        clean_query = clean_query[len("$space:"):]

    # Match by key (case-insensitive exact)
    space_by_key = db.query(Space).filter(
        Space.workspace_id == workspace_id,
        func.lower(Space.key) == clean_query.lower(),
        Space.deleted == 0,
    ).first()
    if space_by_key:
        return ResolutionResult(success=True, value=space_by_key.id, entity=space_by_key)

    # Match by exact name (case-insensitive)
    space_by_name = db.query(Space).filter(
        Space.workspace_id == workspace_id,
        func.lower(Space.name) == clean_query.lower(),
        Space.deleted == 0,
    ).first()
    if space_by_name:
        return ResolutionResult(success=True, value=space_by_name.id, entity=space_by_name)

    # Match by substring
    candidates = db.query(Space).filter(
        Space.workspace_id == workspace_id,
        or_(
            Space.name.ilike(f"%{clean_query}%"),
            Space.key.ilike(f"%{clean_query}%"),
        ),
        Space.deleted == 0,
    ).all()

    if len(candidates) == 1:
        return ResolutionResult(success=True, value=candidates[0].id, entity=candidates[0])
    elif len(candidates) > 1:
        cand_list = [{"id": s.id, "name": s.name, "key": s.key} for s in candidates]
        return ResolutionResult(
            success=False,
            is_ambiguous=True,
            candidates=cand_list,
            error=f"Multiple spaces matched '{clean_query}': {', '.join(s.name for s in candidates)}",
        )

    return ResolutionResult(success=False, error=f"Space '{clean_query}' not found in workspace.")


def resolve_list(
    db: Session,
    workspace_id: str,
    space_identifier: Optional[str],
    query: Optional[str] = None,
) -> ResolutionResult:
    """Resolve a project list in a space.
    Rule 3: If the space has exactly one list, that list is selected automatically.
    """
    # First resolve space if needed
    space_id = None
    if space_identifier:
        space_res = resolve_space(db, workspace_id, space_identifier)
        if not space_res.success:
            return ResolutionResult(success=False, error=f"Cannot resolve list: {space_res.error}")
        space_id = space_res.value

    # If query is a UUID
    if query and is_valid_uuid(str(query)):
        q = db.query(ProjectList).filter(
            ProjectList.workspace_id == workspace_id,
            ProjectList.id == str(query),
            ProjectList.deleted == 0,
        )
        if space_id:
            q = q.filter(ProjectList.space_id == space_id)
        plist = q.first()
        if plist:
            return ResolutionResult(success=True, value=plist.id, entity=plist)

    clean_query = str(query).strip() if query else ""
    if clean_query.startswith("$list:"):
        # e.g. "$list:XYZ/Backlog"
        parts = clean_query[len("$list:"):].split("/")
        if len(parts) > 1:
            if not space_id:
                space_res = resolve_space(db, workspace_id, parts[0])
                if space_res.success:
                    space_id = space_res.value
            clean_query = parts[-1]
        else:
            clean_query = parts[0]

    # If space_id is still unknown, try finding lists workspace-wide by name
    if not space_id:
        workspace_lists = db.query(ProjectList).filter(
            ProjectList.workspace_id == workspace_id,
            ProjectList.deleted == 0,
        )
        if clean_query:
            workspace_lists = workspace_lists.filter(
                func.lower(ProjectList.name) == clean_query.lower()
            )
        matched = workspace_lists.all()
        if len(matched) == 1:
            return ResolutionResult(success=True, value=matched[0].id, entity=matched[0])
        elif len(matched) > 1:
            cand_list = [{"id": l.id, "name": l.name, "space_id": l.space_id} for l in matched]
            return ResolutionResult(
                success=False,
                is_ambiguous=True,
                candidates=cand_list,
                error="Multiple lists found. Please specify which project/space.",
            )
        return ResolutionResult(success=False, error="List not found. Please specify space.")

    # Fetch all active lists in this space
    space_lists = db.query(ProjectList).filter(
        ProjectList.workspace_id == workspace_id,
        ProjectList.space_id == space_id,
        ProjectList.deleted == 0,
    ).order_by(ProjectList.position).all()

    if not space_lists:
        return ResolutionResult(success=False, error="No lists exist in this space.")

    # Rule 3: If no query provided and space has exactly ONE list, pick it!
    if not clean_query:
        if len(space_lists) == 1:
            return ResolutionResult(success=True, value=space_lists[0].id, entity=space_lists[0])
        else:
            cand_list = [{"id": l.id, "name": l.name} for l in space_lists]
            return ResolutionResult(
                success=False,
                is_ambiguous=True,
                candidates=cand_list,
                error="Please specify which list in the project.",
            )

    # Match by exact name
    for l in space_lists:
        if l.name.lower() == clean_query.lower():
            return ResolutionResult(success=True, value=l.id, entity=l)

    # Match by substring
    sub_matches = [l for l in space_lists if clean_query.lower() in l.name.lower()]
    if len(sub_matches) == 1:
        return ResolutionResult(success=True, value=sub_matches[0].id, entity=sub_matches[0])
    elif len(sub_matches) > 1:
        cand_list = [{"id": l.id, "name": l.name} for l in sub_matches]
        return ResolutionResult(
            success=False,
            is_ambiguous=True,
            candidates=cand_list,
            error=f"Multiple lists matched '{clean_query}': {', '.join(l.name for l in sub_matches)}",
        )

    # If space has exactly 1 list, fall back to it
    if len(space_lists) == 1:
        return ResolutionResult(success=True, value=space_lists[0].id, entity=space_lists[0])

    return ResolutionResult(success=False, error=f"List '{clean_query}' not found in space.")


def resolve_sprint(
    db: Session,
    workspace_id: str,
    space_identifier: Optional[str],
    query: Optional[str] = None,
    status: Optional[str] = None,
) -> ResolutionResult:
    """Resolve a sprint by UUID, name, number, or status (e.g. active)."""
    space_id = None
    if space_identifier:
        space_res = resolve_space(db, workspace_id, space_identifier)
        if not space_res.success:
            return ResolutionResult(success=False, error=f"Cannot resolve sprint: {space_res.error}")
        space_id = space_res.value

    # If query is UUID
    if query and is_valid_uuid(str(query)):
        q = db.query(Sprint).filter(
            Sprint.workspace_id == workspace_id,
            Sprint.id == str(query),
            Sprint.deleted == 0,
        )
        if space_id:
            q = q.filter(Sprint.space_id == space_id)
        sprint = q.first()
        if sprint:
            return ResolutionResult(success=True, value=sprint.id, entity=sprint)

    clean_query = str(query).strip() if query else ""
    if clean_query.startswith("$sprint:"):
        parts = clean_query[len("$sprint:"):].split("/")
        if len(parts) > 1:
            if not space_id:
                space_res = resolve_space(db, workspace_id, parts[0])
                if space_res.success:
                    space_id = space_res.value
            clean_query = parts[-1]
        else:
            clean_query = parts[0]

    q = db.query(Sprint).filter(
        Sprint.workspace_id == workspace_id,
        Sprint.deleted == 0,
    )
    if space_id:
        q = q.filter(Sprint.space_id == space_id)

    if status:
        q = q.filter(Sprint.state == status)

    sprints = q.all()
    if not sprints:
        return ResolutionResult(success=False, error="No matching sprints found.")

    # If only status specified and no name query, e.g. "current active sprint"
    if not clean_query and status:
        if len(sprints) == 1:
            return ResolutionResult(success=True, value=sprints[0].id, entity=sprints[0])
        elif len(sprints) > 1:
            cand_list = [{"id": s.id, "name": s.name, "state": s.state} for s in sprints]
            return ResolutionResult(
                success=False,
                is_ambiguous=True,
                candidates=cand_list,
                error=f"Multiple {status} sprints found: {', '.join(s.name for s in sprints)}",
            )

    # Match by exact name (case-insensitive)
    for s in sprints:
        if s.name.lower() == clean_query.lower():
            return ResolutionResult(success=True, value=s.id, entity=s)

    # Match by number, e.g. "13" -> "Sprint 13"
    digit_match = re.search(r"\b(\d+)\b", clean_query)
    if digit_match:
        num = digit_match.group(1)
        num_matches = [s for s in sprints if re.search(rf"\b{num}\b", s.name)]
        if len(num_matches) == 1:
            return ResolutionResult(success=True, value=num_matches[0].id, entity=num_matches[0])

    # Substring match
    sub_matches = [s for s in sprints if clean_query.lower() in s.name.lower()]
    if len(sub_matches) == 1:
        return ResolutionResult(success=True, value=sub_matches[0].id, entity=sub_matches[0])
    elif len(sub_matches) > 1:
        cand_list = [{"id": s.id, "name": s.name, "state": s.state} for s in sub_matches]
        return ResolutionResult(
            success=False,
            is_ambiguous=True,
            candidates=cand_list,
            error=f"Multiple sprints matched '{clean_query}': {', '.join(s.name for s in sub_matches)}",
        )

    return ResolutionResult(success=False, error=f"Sprint '{clean_query}' not found.")


def resolve_member(
    db: Session,
    workspace_id: str,
    query: Any,
    caller_id: str | int | None = None,
) -> ResolutionResult:
    if query is None:
        return ResolutionResult(success=False, error="Member identifier required.")

    query_str = str(query).strip()

    # Self reference
    if query_str in ("$self", "self", "me", "myself"):
        if caller_id:
            user = db.query(User).filter(User.id == str(caller_id), User.deleted == 0).first()
            if user:
                return ResolutionResult(success=True, value=user.id, entity=user)
        return ResolutionResult(success=False, error="Cannot resolve self: caller_id not supplied.")

    if query_str.startswith("$member:"):
        query_str = query_str[len("$member:"):]

    # Match direct ID (UUID or numeric string)
    member_by_id = (
        db.query(User)
        .join(WorkspaceMember, WorkspaceMember.user_id == User.id)
        .filter(
            WorkspaceMember.workspace_id == workspace_id,
            User.id == query_str,
            User.deleted == 0,
        )
        .first()
    )
    if member_by_id:
        return ResolutionResult(success=True, value=member_by_id.id, entity=member_by_id)

    # Active workspace members only
    members = (
        db.query(User, WorkspaceMember.role)
        .join(WorkspaceMember, WorkspaceMember.user_id == User.id)
        .filter(
            WorkspaceMember.workspace_id == workspace_id,
            User.deleted == 0,
        )
        .all()
    )

    clean_q = query_str.lower()

    # Exact email match
    for user, role in members:
        if (user.email or "").lower() == clean_q:
            return ResolutionResult(success=True, value=user.id, entity=user)

    # Exact full name match
    for user, role in members:
        full_name = f"{user.first_name or ''} {user.last_name or ''}".strip().lower()
        if full_name and full_name == clean_q:
            return ResolutionResult(success=True, value=user.id, entity=user)

    # Substring / partial name matches (e.g. "Sarah" matching Sarah Khan and Sarah Lee)
    partial_matches = []
    for user, role in members:
        full_name = f"{user.first_name or ''} {user.last_name or ''}".strip()
        first = (user.first_name or "").lower()
        last = (user.last_name or "").lower()
        if clean_q in full_name.lower() or clean_q in first or clean_q in last:
            partial_matches.append((user, role, full_name))

    if len(partial_matches) == 1:
        u = partial_matches[0][0]
        return ResolutionResult(success=True, value=u.id, entity=u)
    elif len(partial_matches) > 1:
        candidates = [
            {
                "id": u.id,
                "name": name,
                "email": u.email,
                "role": role,
            }
            for u, role, name in partial_matches
        ]
        cand_names = [name for _, _, name in partial_matches]
        return ResolutionResult(
            success=False,
            is_ambiguous=True,
            candidates=candidates,
            error=f"Ambiguous member name '{query_str}'. Did you mean {' or '.join(cand_names)}?",
        )

    return ResolutionResult(success=False, error=f"Member '{query_str}' not found in workspace.")


def resolve_issue(db: Session, workspace_id: str, query: str | None) -> ResolutionResult:
    if not query:
        return ResolutionResult(success=False, error="Issue identifier is required.")

    query_str = str(query).strip()

    # If query is UUID
    if is_valid_uuid(query_str):
        issue = db.query(Issue).filter(
            Issue.workspace_id == workspace_id,
            Issue.id == query_str,
            Issue.deleted == 0,
        ).first()
        if issue:
            return ResolutionResult(success=True, value=issue.id, entity=issue)

    clean_query = query_str
    if clean_query.startswith("$issue:"):
        clean_query = clean_query[len("$issue:"):]

    # Match issue key pattern (e.g. XYZ-104)
    key_match = re.match(r"^([A-Za-z0-9]+)[-_](\d+)$", clean_query)
    if key_match:
        prefix = key_match.group(1).upper()
        num = int(key_match.group(2))

        # Find space by key
        space = db.query(Space).filter(
            Space.workspace_id == workspace_id,
            func.upper(Space.key) == prefix,
            Space.deleted == 0,
        ).first()
        if space:
            issue = db.query(Issue).filter(
                Issue.workspace_id == workspace_id,
                Issue.space_id == space.id,
                Issue.number == num,
                Issue.deleted == 0,
            ).first()
            if issue:
                return ResolutionResult(success=True, value=issue.id, entity=issue)

    # Match title substring
    candidates = db.query(Issue).filter(
        Issue.workspace_id == workspace_id,
        Issue.title.ilike(f"%{clean_query}%"),
        Issue.deleted == 0,
    ).limit(10).all()

    if len(candidates) == 1:
        return ResolutionResult(success=True, value=candidates[0].id, entity=candidates[0])
    elif len(candidates) > 1:
        cand_list = [{"id": i.id, "number": i.number, "title": i.title} for i in candidates]
        return ResolutionResult(
            success=False,
            is_ambiguous=True,
            candidates=cand_list,
            error=f"Multiple issues matched '{clean_query}'.",
        )

    return ResolutionResult(success=False, error=f"Issue '{clean_query}' not found in workspace.")
