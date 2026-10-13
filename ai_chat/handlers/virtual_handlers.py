"""Handlers for virtual tools:
ask_user, find_space, find_list, find_sprint, find_member, find_issue,
knowledge_search, issue_clone, and issue_bulk_update.
"""
from __future__ import annotations

import re
from typing import Any, Optional

from sqlalchemy import or_

from ai_chat.handlers.context import ToolContext
from ai_chat.resolvers import (
    resolve_issue,
    resolve_list,
    resolve_member,
    resolve_space,
    resolve_sprint,
)
from models.hierarchy import ProjectList, Space
from models.issue import Issue, IssueComment, IssueLink
from models.user import User
from models.workspace import WorkspaceMember


def handle_ask_user(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    question = args.get("question", "")
    options = args.get("options") or []
    fields = args.get("fields") or []
    return {
        "action": "ask_user",
        "question": question,
        "options": options,
        "fields": fields,
    }


def handle_find_space(ctx: ToolContext, args: dict[str, Any]) -> list[dict[str, Any]]:
    query = args.get("query", "").strip()
    q = ctx.db.query(Space).filter(
        Space.workspace_id == ctx.workspace_id,
        Space.deleted == 0,
    )
    if query:
        q = q.filter(or_(
            Space.name.ilike(f"%{query}%"),
            Space.key.ilike(f"%{query}%"),
        ))
    spaces = q.all()
    return [
        {
            "id": s.id,
            "name": s.name,
            "key": s.key,
            "description": s.description,
            "is_archived": bool(s.archived_at),
            "is_frozen": bool(s.frozen_at),
        }
        for s in spaces
    ]


def handle_find_list(ctx: ToolContext, args: dict[str, Any]) -> list[dict[str, Any]]:
    space_query = args.get("space")
    list_query = args.get("query", "").strip() if args.get("query") else None

    space_res = resolve_space(ctx.db, ctx.workspace_id, space_query)
    if not space_res.success:
        return [{"error": space_res.error}]

    q = ctx.db.query(ProjectList).filter(
        ProjectList.workspace_id == ctx.workspace_id,
        ProjectList.space_id == space_res.value,
        ProjectList.deleted == 0,
    )
    if list_query:
        q = q.filter(ProjectList.name.ilike(f"%{list_query}%"))

    lists = q.order_by(ProjectList.position).all()
    return [{"id": l.id, "name": l.name, "folder_id": l.folder_id} for l in lists]


def handle_find_sprint(ctx: ToolContext, args: dict[str, Any]) -> list[dict[str, Any]]:
    from models.sprint import Sprint

    space_query = args.get("space")
    sprint_query = args.get("query", "").strip() if args.get("query") else None
    status = args.get("status")

    space_res = resolve_space(ctx.db, ctx.workspace_id, space_query)
    if not space_res.success:
        return [{"error": space_res.error}]

    q = ctx.db.query(Sprint).filter(
        Sprint.workspace_id == ctx.workspace_id,
        Sprint.space_id == space_res.value,
        Sprint.deleted == 0,
    )
    if status:
        q = q.filter(Sprint.state == status)
    if sprint_query:
        q = q.filter(Sprint.name.ilike(f"%{sprint_query}%"))

    sprints = q.order_by(Sprint.created_at.desc()).all()
    return [
        {
            "id": s.id,
            "name": s.name,
            "status": s.state,
            "goal": s.goal,
            "starts_on": str(s.starts_on) if s.starts_on else None,
            "ends_on": str(s.ends_on) if s.ends_on else None,
        }
        for s in sprints
    ]


def handle_find_member(ctx: ToolContext, args: dict[str, Any]) -> list[dict[str, Any]]:
    query = args.get("query", "").strip()
    q = (
        ctx.db.query(User, WorkspaceMember.role)
        .join(WorkspaceMember, WorkspaceMember.user_id == User.id)
        .filter(
            WorkspaceMember.workspace_id == ctx.workspace_id,
            User.deleted == 0,
        )
    )
    if query:
        q = q.filter(or_(
            User.first_name.ilike(f"%{query}%"),
            User.last_name.ilike(f"%{query}%"),
            User.email.ilike(f"%{query}%"),
            User.username.ilike(f"%{query}%"),
        ))

    results = q.all()
    return [
        {
            "id": u.id,
            "name": f"{u.first_name or ''} {u.last_name or ''}".strip() or u.username,
            "username": u.username,
            "email": u.email,
            "role": role,
        }
        for u, role in results
    ]


def handle_find_issue(ctx: ToolContext, args: dict[str, Any]) -> list[dict[str, Any]]:
    query = args.get("query", "").strip()
    space_query = args.get("space")

    space_id = None
    if space_query:
        space_res = resolve_space(ctx.db, ctx.workspace_id, space_query)
        if space_res.success:
            space_id = space_res.value

    # Check if query is an issue key (e.g. XYZ-104)
    key_match = re.match(r"^([A-Za-z0-9]+)[-_](\d+)$", query)
    if key_match:
        prefix = key_match.group(1).upper()
        num = int(key_match.group(2))
        space = ctx.db.query(Space).filter(
            Space.workspace_id == ctx.workspace_id,
            Space.key == prefix,
            Space.deleted == 0,
        ).first()
        if space:
            issue = ctx.db.query(Issue).filter(
                Issue.workspace_id == ctx.workspace_id,
                Issue.space_id == space.id,
                Issue.number == num,
                Issue.deleted == 0,
            ).first()
            if issue:
                return [{
                    "id": issue.id,
                    "key": f"{space.key}-{issue.number}",
                    "title": issue.title,
                    "status_id": issue.status_id,
                    "priority": issue.priority,
                    "type": issue.type,
                }]

    # Title / text search
    q = ctx.db.query(Issue, Space.key).join(Space, Space.id == Issue.space_id).filter(
        Issue.workspace_id == ctx.workspace_id,
        Issue.deleted == 0,
    )
    if space_id:
        q = q.filter(Issue.space_id == space_id)
    if query:
        q = q.filter(or_(
            Issue.title.ilike(f"%{query}%"),
            Issue.description.ilike(f"%{query}%"),
        ))

    issues = q.limit(20).all()
    return [
        {
            "id": i.id,
            "key": f"{key}-{i.number}",
            "title": i.title,
            "status_id": i.status_id,
            "priority": i.priority,
            "type": i.type,
        }
        for i, key in issues
    ]


def handle_knowledge_search(ctx: ToolContext, args: dict[str, Any]) -> list[dict[str, Any]]:
    query = args.get("query", "").strip()
    top_k = args.get("top_k", 5)

    passages = []

    # 1. Search issues
    issue_hits = (
        ctx.db.query(Issue, Space.key)
        .join(Space, Space.id == Issue.space_id)
        .filter(
            Issue.workspace_id == ctx.workspace_id,
            Issue.deleted == 0,
            or_(
                Issue.title.ilike(f"%{query}%"),
                Issue.description.ilike(f"%{query}%"),
            ),
        )
        .limit(top_k)
        .all()
    )
    for issue, key in issue_hits:
        snippet = (issue.description or issue.title)[:300]
        passages.append({
            "source": "issues",
            "citation": f"{key}-{issue.number}",
            "title": issue.title,
            "passage": snippet,
        })

    # 2. Search comments
    comment_hits = (
        ctx.db.query(IssueComment, Issue, Space.key)
        .join(Issue, Issue.id == IssueComment.issue_id)
        .join(Space, Space.id == Issue.space_id)
        .filter(
            Issue.workspace_id == ctx.workspace_id,
            IssueComment.deleted == 0,
            IssueComment.body.ilike(f"%{query}%"),
        )
        .limit(top_k)
        .all()
    )
    for comment, issue, key in comment_hits:
        passages.append({
            "source": "comments",
            "citation": f"{key}-{issue.number}",
            "title": f"Comment on {issue.title}",
            "passage": comment.body[:300],
        })

    return passages[:top_k]


def handle_issue_clone(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    from controllers import issue_controller
    from schemas.issues import IssueCreate

    issue_ref = args.get("issue")
    target_list_ref = args.get("target_list")

    issue_res = resolve_issue(ctx.db, ctx.workspace_id, issue_ref)
    if not issue_res.success:
        return {"error": issue_res.error}

    src_issue: Issue = issue_res.entity

    target_list_id = src_issue.list_id
    if target_list_ref:
        list_res = resolve_list(ctx.db, ctx.workspace_id, src_issue.space_id, target_list_ref)
        if list_res.success:
            target_list_id = list_res.value

    # Create cloned issue
    body = IssueCreate(
        list_id=target_list_id,
        title=f"Clone of {src_issue.title}",
        description=src_issue.description or "",
        type=src_issue.type,
        priority=src_issue.priority,
        story_points=src_issue.story_points,
    )
    new_issue_resp = issue_controller.create_issue(ctx.access, body, ctx.db)
    new_issue_dict = new_issue_resp if isinstance(new_issue_resp, dict) else (
        new_issue_resp.model_dump() if hasattr(new_issue_resp, "model_dump") else {}
    )

    new_id = new_issue_dict.get("id")
    if new_id:
        # Create dependency link "clones"
        dep = IssueLink(
            workspace_id=ctx.workspace_id,
            source_issue_id=new_id,
            target_issue_id=src_issue.id,
            kind="clones",
            created_by=ctx.current_user.id,
        )
        ctx.db.add(dep)
        ctx.db.commit()

    return {
        "cloned_issue": new_issue_dict,
        "source_issue_id": src_issue.id,
        "relationship": "clones",
    }


def handle_issue_bulk_update(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    from controllers import issue_controller
    from schemas.issues import IssueUpdate

    issues_refs = args.get("issues") or []
    changes = args.get("changes") or {}
    dry_run = args.get("dry_run", True)

    resolved_issues: list[Issue] = []
    for ref in issues_refs[:100]:
        res = resolve_issue(ctx.db, ctx.workspace_id, ref)
        if res.success and res.entity:
            resolved_issues.append(res.entity)

    if dry_run:
        sample = [{"id": i.id, "number": i.number, "title": i.title} for i in resolved_issues[:5]]
        return {
            "dry_run": True,
            "matched_count": len(resolved_issues),
            "changes": changes,
            "sample": sample,
        }

    # Execute updates
    is_delete = bool(changes.get("delete"))
    updated = 0
    failed = 0
    consecutive_failures = 0
    results = []

    for issue in resolved_issues:
        if consecutive_failures >= 3:
            results.append({"status": "aborted", "reason": "Stopped after 3 consecutive failures."})
            break

        try:
            if is_delete:
                issue_controller.delete_issue(ctx.access, issue.id, ctx.db)
            else:
                body = IssueUpdate(**{k: v for k, v in changes.items() if k != "delete"})
                issue_controller.update_issue(ctx.access, issue.id, body, ctx.db)

            updated += 1
            consecutive_failures = 0
            results.append({"id": issue.id, "number": issue.number, "status": "ok"})
        except Exception as exc:
            failed += 1
            consecutive_failures += 1
            results.append({"id": issue.id, "number": issue.number, "status": "error", "error": str(exc)})

    return {
        "dry_run": False,
        "total": len(resolved_issues),
        "updated": updated,
        "failed": failed,
        "results": results,
    }
