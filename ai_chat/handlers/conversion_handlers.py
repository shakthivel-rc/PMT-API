"""Autonomous conversion handlers for multimodal assets:
issue_create_from_attachment, workspace_scaffold_from_spec, and bulk_issue_create.
"""
from __future__ import annotations

import re
from typing import Any

from ai_chat.handlers.context import ToolContext
from ai_chat.handlers.read_handlers import unwrap_response
from ai_chat.resolvers import resolve_list, resolve_member
from controllers import issue_controller, sprint_controller, workspace_controller
from models.workspace import Workspace
from schemas.issues import IssueCreate
from schemas.sprints import SprintCreate
from schemas.workspaces import ListCreate, SpaceCreate
from services import chat_attachment_service
from services import storage_service as storage_svc


def _extract_item(data: Any) -> dict[str, Any]:
    if isinstance(data, dict):
        if "data" in data and isinstance(data["data"], dict):
            return data["data"]
        return data
    return {}


def handle_issue_create_from_attachment(ctx: ToolContext, args: dict[str, Any]) -> Any:
    """Create an issue and attach the uploaded chat file directly to it."""
    attachment_id = args.get("attachment_id")
    list_id = args.get("list_id")
    space_id = args.get("space_id") or args.get("space")

    if not list_id and space_id:
        l_res = resolve_list(ctx.db, ctx.workspace_id, space_id)
        if not l_res.success:
            return {"error": l_res.error}
        list_id = l_res.value
    elif list_id and space_id:
        l_res = resolve_list(ctx.db, ctx.workspace_id, space_id, list_id)
        if l_res.success:
            list_id = l_res.value

    assignee_id = args.get("assignee_id") or args.get("assignee")
    if assignee_id:
        m_res = resolve_member(ctx.db, ctx.workspace_id, assignee_id, caller_id=ctx.user_id)
        assignee_id = m_res.value if m_res.success else assignee_id

    body = IssueCreate(
        list_id=list_id,
        title=args.get("title", "Reported Issue"),
        description=args.get("description", ""),
        type=args.get("type", "bug"),
        priority=args.get("priority", "high"),
        assignee_id=str(assignee_id) if assignee_id else None,
        story_points=args.get("story_points"),
        due_date=args.get("due_date"),
    )
    resp = issue_controller.create_issue(ctx.access, body, ctx.db)
    raw_issue_data = unwrap_response(resp)
    created_issue_data = _extract_item(raw_issue_data)
    issue_id = created_issue_data.get("id")

    # Link the uploaded chat attachment directly to the newly created issue
    attached = False
    filename = None
    if attachment_id and issue_id:
        att = chat_attachment_service.get_chat_attachment(ctx.db, ctx.workspace_id, attachment_id)
        ws = ctx.db.query(Workspace).filter(Workspace.id == ctx.workspace_id).first()
        if att and ws:
            file_bytes = chat_attachment_service.read_chat_attachment_bytes(ws, att)
            if file_bytes:
                from models.issue import Issue
                issue_obj = ctx.db.query(Issue).filter(Issue.id == issue_id).first()
                if issue_obj:
                    storage_svc.upload_attachment(
                        db=ctx.db,
                        workspace=ws,
                        issue=issue_obj,
                        uploader_id=ctx.user_id,
                        filename=att.original_filename,
                        content=file_bytes,
                        content_type=att.mime_type,
                    )
                    attached = True
                    filename = att.original_filename

    return {
        "issue": created_issue_data or raw_issue_data,
        "attachment_linked": attached,
        "filename": filename,
    }


def handle_workspace_scaffold_from_spec(ctx: ToolContext, args: dict[str, Any]) -> Any:
    """Scaffold Space, Lists, Sprint, and Backlog items extracted from a PRD spec."""
    space_name = args.get("space_name", "New Project Space").strip()
    space_key = args.get("space_key")

    if not space_key:
        cleaned = re.sub(r"[^A-Za-z0-9]", "", space_name).upper()
        space_key = cleaned[:4] if len(cleaned) >= 2 else "PRJ"

    # 1. Create Space
    space_resp = workspace_controller.create_space(
        ctx.access,
        SpaceCreate(name=space_name, key=space_key),
        ctx.db,
    )
    space_raw = unwrap_response(space_resp)
    if isinstance(space_raw, dict) and "error" in space_raw:
        return space_raw
    space_data = _extract_item(space_raw)
    space_id = space_data.get("id") or (space_raw.get("id") if isinstance(space_raw, dict) else None)

    # 2. Create Lists
    list_names = args.get("lists") or ["Backlog", "In Progress", "Review", "Done"]
    created_lists: list[dict[str, Any]] = []
    list_id_map: dict[str, str] = {}

    for l_name in list_names:
        l_resp = workspace_controller.create_list(
            ctx.access,
            ListCreate(space_id=str(space_id), name=l_name.strip()),
            ctx.db,
        )
        l_raw = unwrap_response(l_resp)
        l_data = _extract_item(l_raw)
        item_to_add = l_data if ("id" in l_data) else (l_raw if isinstance(l_raw, dict) and "id" in l_raw else None)
        if item_to_add:
            created_lists.append(item_to_add)
            list_id_map[l_name.lower().strip()] = item_to_add["id"]

    # 3. Create Sprint if specified
    created_sprint: dict[str, Any] | None = None
    sprint_name = args.get("sprint_name")
    if sprint_name:
        s_resp = sprint_controller.create_sprint(
            ctx.access,
            str(space_id),
            SprintCreate(name=sprint_name.strip()),
            ctx.db,
        )
        s_raw = unwrap_response(s_resp)
        created_sprint = _extract_item(s_raw) or s_raw

    # 4. Create Initial Issues
    initial_issues = args.get("initial_issues") or []
    created_issues: list[dict[str, Any]] = []
    default_list_id = created_lists[0]["id"] if created_lists else None

    for item in initial_issues:
        target_list_name = item.get("list_name", "").lower().strip()
        target_list_id = list_id_map.get(target_list_name, default_list_id)
        if not target_list_id:
            continue

        issue_body = IssueCreate(
            list_id=target_list_id,
            title=item.get("title", "Backlog item"),
            description=item.get("description", ""),
            type=item.get("type", "story"),
            priority=item.get("priority", "medium"),
        )
        iss_resp = issue_controller.create_issue(ctx.access, issue_body, ctx.db)
        iss_raw = unwrap_response(iss_resp)
        iss_data = _extract_item(iss_raw)
        item_to_add = iss_data if ("id" in iss_data) else (iss_raw if isinstance(iss_raw, dict) and "id" in iss_raw else None)
        if item_to_add:
            created_issues.append(item_to_add)

    return {
        "space": space_data or space_raw,
        "lists": created_lists,
        "sprint": created_sprint,
        "issues_created": created_issues,
        "summary": (
            f"Successfully scaffolded Space '{space_name}' ({space_key}) with "
            f"{len(created_lists)} lists and {len(created_issues)} initial issues."
        ),
    }


def handle_bulk_issue_create(ctx: ToolContext, args: dict[str, Any]) -> Any:
    """Batch-create multiple tickets from parsed rows of a CSV or sheet."""
    list_id = args.get("list_id")
    space_id = args.get("space_id") or args.get("space")
    issues_input = args.get("issues") or []

    if not list_id and space_id:
        l_res = resolve_list(ctx.db, ctx.workspace_id, space_id)
        if not l_res.success:
            return {"error": l_res.error}
        list_id = l_res.value
    elif list_id and space_id:
        l_res = resolve_list(ctx.db, ctx.workspace_id, space_id, list_id)
        if l_res.success:
            list_id = l_res.value

    if not list_id:
        return {"error": "Target list_id is required for bulk issue creation."}

    created: list[dict[str, Any]] = []
    failures: list[dict[str, Any]] = []

    for idx, item in enumerate(issues_input):
        title = item.get("title", f"Imported Issue {idx + 1}")
        assignee_id = item.get("assignee_id")
        if assignee_id:
            m_res = resolve_member(ctx.db, ctx.workspace_id, assignee_id, caller_id=ctx.user_id)
            assignee_id = m_res.value if m_res.success else assignee_id

        issue_body = IssueCreate(
            list_id=list_id,
            title=title,
            description=item.get("description", ""),
            type=item.get("type", "task"),
            priority=item.get("priority", "medium"),
            assignee_id=str(assignee_id) if assignee_id else None,
            story_points=item.get("story_points"),
            due_date=item.get("due_date"),
        )
        try:
            resp = issue_controller.create_issue(ctx.access, issue_body, ctx.db)
            raw = unwrap_response(resp)
            data = _extract_item(raw)
            item_to_add = data if ("id" in data) else (raw if isinstance(raw, dict) and "id" in raw else None)
            if item_to_add:
                created.append(item_to_add)
            else:
                failures.append({"title": title, "error": str(raw)})
        except Exception as exc:
            failures.append({"title": title, "error": str(exc)})

    return {
        "total_requested": len(issues_input),
        "total_created": len(created),
        "created_issues": created,
        "failures": failures,
        "summary": f"Batch created {len(created)} of {len(issues_input)} issues successfully.",
    }
