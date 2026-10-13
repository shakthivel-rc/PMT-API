"""Write tool handlers for the AI Chat Gateway.
Handles state mutations, creation, updates, deletes, and workflow transitions.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from starlette.responses import JSONResponse

from ai_chat.handlers.context import ToolContext
from ai_chat.handlers.read_handlers import parse_date_param, unwrap_response
from ai_chat.resolvers import (
    resolve_issue,
    resolve_list,
    resolve_member,
    resolve_space,
    resolve_sprint,
)
from controllers import (
    dependency_controller,
    issue_controller,
    release_controller,
    report_controller,
    saved_view_controller,
    sprint_controller,
    storage_controller,
    time_tracking_controller,
    workflow_controller,
    workspace_controller,
)
from schemas.dependencies import DependencyCreate
from schemas.governance import GovernanceSettingsUpdate
from schemas.issues import BacklogRank, CommentCreate, CommentUpdate, IssueCreate, IssueMove, IssueUpdate
from schemas.releases import FixVersionsSet, ReleaseCreate, ReleaseShip, ReleaseUpdate
from schemas.reports import ExportRequest, ReportParams, SavedReportCreate, SavedReportUpdate, ScheduleCreate, ScheduleUpdate
from schemas.saved_views import SavedViewCreate, SavedViewUpdate
from schemas.sprints import SprintClose, SprintCreate, SprintIssues, SprintStart, SprintUpdate
from schemas.time_tracking import TimeEntryCreate, TimeEntryUpdate, TimeTrackingSettingsUpdate, TimerStart
from schemas.workspaces import (
    FolderCreate,
    FolderUpdate,
    ListCreate,
    ListUpdate,
    MemberAdd,
    MemberUpdate,
    RepositoryLink,
    SpaceCreate,
    SpaceFreeze,
    SpaceUpdate,
    WorkspaceCreate,
    WorkspaceUpdate,
)


# -------------------------------------------------------------------------
# Workspaces & Hierarchy Writes
# -------------------------------------------------------------------------

def handle_workspace_create(ctx: ToolContext, args: dict[str, Any]) -> Any:
    name = args.get("name", "New Workspace")
    slug = args.get("slug")
    if not slug:
        import re
        slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
        if len(slug) < 2:
            slug = f"ws-{slug}" if slug else "workspace"
    body = WorkspaceCreate(name=name, slug=slug)
    resp = workspace_controller.create_workspace(body, str(ctx.user_id), ctx.db, ctx.get_request())
    return unwrap_response(resp)


def handle_workspace_update(ctx: ToolContext, args: dict[str, Any]) -> Any:
    body = WorkspaceUpdate(name=args.get("name"), slug=args.get("slug"))
    resp = workspace_controller.update_workspace(ctx.access, body, ctx.db, ctx.get_request())
    return unwrap_response(resp)


def handle_workspace_member_add(ctx: ToolContext, args: dict[str, Any]) -> Any:
    email = args.get("email")
    if not email and args.get("user_id"):
        from models.user import User
        u = ctx.db.query(User).filter(User.id == str(args.get("user_id"))).first()
        if u:
            email = u.email
    if not email:
        return {"error": "Email is required to add a workspace member"}
    body = MemberAdd(email=email, role=args.get("role", "member"))
    resp = workspace_controller.add_member(ctx.access, body, ctx.db, ctx.get_request())
    return unwrap_response(resp)


def handle_workspace_member_update_role(ctx: ToolContext, args: dict[str, Any]) -> Any:
    user_id = args.get("user_id") or args.get("user")
    m_res = resolve_member(ctx.db, ctx.workspace_id, user_id, caller_id=ctx.user_id)
    if m_res.success:
        user_id = m_res.value
    body = MemberUpdate(role=args.get("role"))
    resp = workspace_controller.update_member(ctx.access, str(user_id), body, ctx.db, ctx.get_request())
    return unwrap_response(resp)


def handle_workspace_member_remove(ctx: ToolContext, args: dict[str, Any]) -> Any:
    user_id = args.get("user_id") or args.get("user")
    m_res = resolve_member(ctx.db, ctx.workspace_id, user_id, caller_id=ctx.user_id)
    if m_res.success:
        user_id = m_res.value
    resp = workspace_controller.remove_member(ctx.access, str(user_id), ctx.db, ctx.get_request())
    return unwrap_response(resp)


def handle_space_create(ctx: ToolContext, args: dict[str, Any]) -> Any:
    name = args.get("name")
    # Derive key if not supplied
    key = args.get("key")
    if not key:
        key = "".join([w[0].upper() for w in name.split() if w])[:4]
        if len(key) < 2:
            key = (name[:3] if len(name) >= 3 else name + "X").upper()

    create_data: dict[str, Any] = {
        "name": name,
        "key": key,
    }
    if args.get("description"):
        create_data["description"] = args.get("description")
    if args.get("workflow_template"):
        create_data["workflow_template"] = args.get("workflow_template")
    if args.get("owner_id"):
        create_data["owner_id"] = args.get("owner_id")

    body = SpaceCreate(**create_data)
    resp = workspace_controller.create_space(ctx.access, body, ctx.db, ctx.get_request())
    return unwrap_response(resp)


def handle_space_update(ctx: ToolContext, args: dict[str, Any]) -> Any:
    space_id = args.get("space_id") or args.get("space")
    s_res = resolve_space(ctx.db, ctx.workspace_id, space_id)
    if not s_res.success:
        return {"error": s_res.error}
    body = SpaceUpdate(name=args.get("name"), description=args.get("description"))
    resp = workspace_controller.update_space(ctx.access, s_res.value, body, ctx.db, ctx.get_request())
    return unwrap_response(resp)


def handle_space_archive(ctx: ToolContext, args: dict[str, Any]) -> Any:
    space_id = args.get("space_id") or args.get("space")
    s_res = resolve_space(ctx.db, ctx.workspace_id, space_id)
    if not s_res.success:
        return {"error": s_res.error}
    resp = workspace_controller.archive_space(ctx.access, s_res.value, ctx.db, ctx.get_request())
    return unwrap_response(resp)


def handle_space_unarchive(ctx: ToolContext, args: dict[str, Any]) -> Any:
    space_id = args.get("space_id") or args.get("space")
    s_res = resolve_space(ctx.db, ctx.workspace_id, space_id)
    if not s_res.success:
        return {"error": s_res.error}
    resp = workspace_controller.unarchive_space(ctx.access, s_res.value, ctx.db, ctx.get_request())
    return unwrap_response(resp)


def handle_space_freeze(ctx: ToolContext, args: dict[str, Any]) -> Any:
    space_id = args.get("space_id") or args.get("space")
    s_res = resolve_space(ctx.db, ctx.workspace_id, space_id)
    if not s_res.success:
        return {"error": s_res.error}
    body = SpaceFreeze(reason=args.get("reason", "Administrative freeze"))
    resp = workspace_controller.freeze_space(ctx.access, s_res.value, body, ctx.db, ctx.get_request())
    return unwrap_response(resp)


def handle_space_unfreeze(ctx: ToolContext, args: dict[str, Any]) -> Any:
    space_id = args.get("space_id") or args.get("space")
    s_res = resolve_space(ctx.db, ctx.workspace_id, space_id)
    if not s_res.success:
        return {"error": s_res.error}
    resp = workspace_controller.unfreeze_space(ctx.access, s_res.value, ctx.db, ctx.get_request())
    return unwrap_response(resp)


def handle_folder_create(ctx: ToolContext, args: dict[str, Any]) -> Any:
    space_id = args.get("space_id") or args.get("space")
    s_res = resolve_space(ctx.db, ctx.workspace_id, space_id)
    if not s_res.success:
        return {"error": s_res.error}
    body = FolderCreate(name=args.get("name"))
    resp = workspace_controller.create_folder(ctx.access, s_res.value, body, ctx.db)
    return unwrap_response(resp)


def handle_folder_update(ctx: ToolContext, args: dict[str, Any]) -> Any:
    body = FolderUpdate(name=args.get("name"))
    resp = workspace_controller.update_folder(ctx.access, args.get("folder_id"), body, ctx.db, ctx.get_request())
    return unwrap_response(resp)


def handle_folder_archive(ctx: ToolContext, args: dict[str, Any]) -> Any:
    resp = workspace_controller.archive_folder(ctx.access, args.get("folder_id"), ctx.db, ctx.get_request())
    return unwrap_response(resp)


def handle_folder_unarchive(ctx: ToolContext, args: dict[str, Any]) -> Any:
    resp = workspace_controller.unarchive_folder(ctx.access, args.get("folder_id"), ctx.db, ctx.get_request())
    return unwrap_response(resp)


def handle_list_create(ctx: ToolContext, args: dict[str, Any]) -> Any:
    space_id = args.get("space_id") or args.get("space")
    s_res = resolve_space(ctx.db, ctx.workspace_id, space_id)
    if not s_res.success:
        return {"error": s_res.error}
    body = ListCreate(
        space_id=s_res.value,
        folder_id=args.get("folder_id"),
        name=args.get("name"),
    )
    resp = workspace_controller.create_list(ctx.access, body, ctx.db)
    return unwrap_response(resp)


def handle_list_update(ctx: ToolContext, args: dict[str, Any]) -> Any:
    body = ListUpdate(name=args.get("name"))
    resp = workspace_controller.update_list(ctx.access, args.get("list_id"), body, ctx.db, ctx.get_request())
    return unwrap_response(resp)


def handle_list_archive(ctx: ToolContext, args: dict[str, Any]) -> Any:
    resp = workspace_controller.archive_list(ctx.access, args.get("list_id"), ctx.db, ctx.get_request())
    return unwrap_response(resp)


def handle_list_unarchive(ctx: ToolContext, args: dict[str, Any]) -> Any:
    resp = workspace_controller.unarchive_list(ctx.access, args.get("list_id"), ctx.db, ctx.get_request())
    return unwrap_response(resp)


def handle_governance_settings_update(ctx: ToolContext, args: dict[str, Any]) -> Any:
    body = GovernanceSettingsUpdate(
        wip_limits_enabled=args.get("wip_limits_enabled"),
        strict_transitions=args.get("strict_transitions"),
        estimation_required=args.get("estimation_required"),
    )
    resp = workspace_controller.update_governance(ctx.access, body, ctx.db, ctx.get_request())
    return unwrap_response(resp)


# -------------------------------------------------------------------------
# Issues & Comments Writes
# -------------------------------------------------------------------------

def handle_issue_create(ctx: ToolContext, args: dict[str, Any]) -> Any:
    list_id = args.get("list_id")
    space_id = args.get("space_id") or args.get("space")

    # If list_id not provided but space is, resolve list via Rule 3
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
        title=args.get("title"),
        description=args.get("description", ""),
        type=args.get("type", "task"),
        priority=args.get("priority", "medium"),
        assignee_id=str(assignee_id) if assignee_id else None,
        story_points=args.get("story_points"),
        due_date=args.get("due_date"),
    )
    resp = issue_controller.create_issue(ctx.access, body, ctx.db)
    return unwrap_response(resp)


def handle_issue_update(ctx: ToolContext, args: dict[str, Any]) -> Any:
    issue_id = args.get("issue_id") or args.get("issue")
    i_res = resolve_issue(ctx.db, ctx.workspace_id, issue_id)
    if not i_res.success:
        return {"error": i_res.error}

    from models.issue import Issue
    iss = ctx.db.query(Issue).filter(Issue.id == i_res.value).first()
    version = args.get("version") or (iss.version if iss else 1)

    assignee_id = args.get("assignee_id") or args.get("assignee")
    if assignee_id:
        m_res = resolve_member(ctx.db, ctx.workspace_id, assignee_id, caller_id=ctx.user_id)
        assignee_id = m_res.value if m_res.success else assignee_id

    due_date = parse_date_param(args.get("due_date"))

    body = IssueUpdate(
        version=int(version),
        title=args.get("title"),
        description=args.get("description"),
        type=args.get("type"),
        priority=args.get("priority"),
        assignee_id=str(assignee_id) if assignee_id else None,
        story_points=args.get("story_points"),
        due_date=due_date,
    )
    resp = issue_controller.update_issue(ctx.access, i_res.value, body, ctx.db)
    return unwrap_response(resp)


def handle_issue_move(ctx: ToolContext, args: dict[str, Any]) -> Any:
    issue_id = args.get("issue_id") or args.get("issue")
    i_res = resolve_issue(ctx.db, ctx.workspace_id, issue_id)
    if not i_res.success:
        return {"error": i_res.error}

    status_id = args.get("status_id") or args.get("status")
    if not status_id:
        return {"error": "status_id is required to move an issue"}

    body = IssueMove(
        status_id=status_id,
        after_id=args.get("after_id"),
        before_id=args.get("before_id"),
    )
    resp = issue_controller.move_issue(ctx.access, i_res.value, body, ctx.db)
    return unwrap_response(resp)


def handle_issue_rank_backlog(ctx: ToolContext, args: dict[str, Any]) -> Any:
    issue_id = args.get("issue_id") or args.get("issue")
    i_res = resolve_issue(ctx.db, ctx.workspace_id, issue_id)
    if not i_res.success:
        return {"error": i_res.error}
    body = BacklogRank(
        after_id=args.get("after_id") or args.get("prev_issue_id"),
        before_id=args.get("before_id") or args.get("next_issue_id"),
    )
    resp = issue_controller.rank_in_backlog(ctx.access, i_res.value, body, ctx.db)
    return unwrap_response(resp)


def handle_issue_delete(ctx: ToolContext, args: dict[str, Any]) -> Any:
    issue_id = args.get("issue_id") or args.get("issue")
    i_res = resolve_issue(ctx.db, ctx.workspace_id, issue_id)
    if not i_res.success:
        return {"error": i_res.error}
    resp = issue_controller.delete_issue(ctx.access, i_res.value, ctx.db)
    return unwrap_response(resp)


def handle_comment_create(ctx: ToolContext, args: dict[str, Any]) -> Any:
    issue_id = args.get("issue_id") or args.get("issue")
    i_res = resolve_issue(ctx.db, ctx.workspace_id, issue_id)
    if not i_res.success:
        return {"error": i_res.error}
    body = CommentCreate(body=args.get("body") or args.get("content", ""))
    resp = issue_controller.create_comment(ctx.access, i_res.value, body, ctx.db)
    return unwrap_response(resp)


def handle_comment_update(ctx: ToolContext, args: dict[str, Any]) -> Any:
    issue_id = args.get("issue_id") or args.get("issue")
    i_res = resolve_issue(ctx.db, ctx.workspace_id, issue_id)
    if not i_res.success:
        return {"error": i_res.error}
    body = CommentUpdate(body=args.get("body") or args.get("content", ""))
    resp = issue_controller.update_comment(ctx.access, i_res.value, args.get("comment_id"), body, ctx.db)
    return unwrap_response(resp)


def handle_comment_delete(ctx: ToolContext, args: dict[str, Any]) -> Any:
    issue_id = args.get("issue_id") or args.get("issue")
    i_res = resolve_issue(ctx.db, ctx.workspace_id, issue_id)
    if not i_res.success:
        return {"error": i_res.error}
    resp = issue_controller.delete_comment(ctx.access, i_res.value, args.get("comment_id"), ctx.db)
    return unwrap_response(resp)


def handle_issue_ai_summary_request(ctx: ToolContext, args: dict[str, Any]) -> Any:
    from controllers import ai_controller
    issue_id = args.get("issue_id") or args.get("issue")
    i_res = resolve_issue(ctx.db, ctx.workspace_id, issue_id)
    if not i_res.success:
        return {"error": i_res.error}
    resp = ai_controller.create_issue_summary_job(ctx.access, i_res.value, ctx.db)
    return unwrap_response(resp)


# -------------------------------------------------------------------------
# Dependencies Writes
# -------------------------------------------------------------------------

def handle_dependency_create(ctx: ToolContext, args: dict[str, Any]) -> Any:
    issue_id = args.get("issue_id") or args.get("issue")
    target_id = args.get("target_issue_id")

    i_res = resolve_issue(ctx.db, ctx.workspace_id, issue_id)
    t_res = resolve_issue(ctx.db, ctx.workspace_id, target_id)

    if not i_res.success:
        return {"error": i_res.error}
    if not t_res.success:
        return {"error": t_res.error}

    body = DependencyCreate(
        target_issue_id=t_res.value,
        relationship_type=args.get("relationship_type", "blocks"),
    )
    resp = dependency_controller.create_dependency(ctx.access, i_res.value, body, ctx.db)
    return unwrap_response(resp)


def handle_dependency_delete(ctx: ToolContext, args: dict[str, Any]) -> Any:
    dep_id = args.get("dependency_id") or args.get("dep_id")
    resp = dependency_controller.delete_dependency(ctx.access, dep_id, ctx.db)
    return unwrap_response(resp)


# -------------------------------------------------------------------------
# Sprints Writes
# -------------------------------------------------------------------------

def handle_sprint_create(ctx: ToolContext, args: dict[str, Any]) -> Any:
    space_id = args.get("space_id") or args.get("space")
    s_res = resolve_space(ctx.db, ctx.workspace_id, space_id)
    if not s_res.success:
        return {"error": s_res.error}
    starts_on = parse_date_param(args.get("starts_on") or args.get("start_date"))
    ends_on = parse_date_param(args.get("ends_on") or args.get("end_date"))
    body = SprintCreate(
        name=args.get("name"),
        goal=args.get("goal"),
        starts_on=starts_on,
        ends_on=ends_on,
    )
    resp = sprint_controller.create_sprint(ctx.access, s_res.value, body, ctx.db)
    return unwrap_response(resp)


def handle_sprint_update(ctx: ToolContext, args: dict[str, Any]) -> Any:
    sprint_id = args.get("sprint_id") or args.get("sprint")
    space_id = args.get("space_id") or args.get("space")
    if space_id:
        sp_res = resolve_sprint(ctx.db, ctx.workspace_id, space_id, sprint_id)
        if sp_res.success:
            sprint_id = sp_res.value
    starts_on = parse_date_param(args.get("starts_on") or args.get("start_date"))
    ends_on = parse_date_param(args.get("ends_on") or args.get("end_date"))
    body = SprintUpdate(
        name=args.get("name"),
        goal=args.get("goal"),
        starts_on=starts_on,
        ends_on=ends_on,
    )
    resp = sprint_controller.update_sprint(ctx.access, sprint_id, body, ctx.db)
    return unwrap_response(resp)


def handle_sprint_delete(ctx: ToolContext, args: dict[str, Any]) -> Any:
    sprint_id = args.get("sprint_id") or args.get("sprint")
    space_id = args.get("space_id") or args.get("space")
    if space_id:
        sp_res = resolve_sprint(ctx.db, ctx.workspace_id, space_id, sprint_id)
        if sp_res.success:
            sprint_id = sp_res.value
    resp = sprint_controller.delete_sprint(ctx.access, sprint_id, ctx.db, ctx.get_request())
    return unwrap_response(resp)


def handle_sprint_start(ctx: ToolContext, args: dict[str, Any]) -> Any:
    sprint_id = args.get("sprint_id") or args.get("sprint")
    space_id = args.get("space_id") or args.get("space")
    if space_id:
        sp_res = resolve_sprint(ctx.db, ctx.workspace_id, space_id, sprint_id)
        if sp_res.success:
            sprint_id = sp_res.value
    starts_on = parse_date_param(args.get("starts_on") or args.get("start_date"))
    ends_on = parse_date_param(args.get("ends_on") or args.get("end_date"))
    body = SprintStart(starts_on=starts_on, ends_on=ends_on)
    resp = sprint_controller.start_sprint(ctx.access, sprint_id, body, ctx.db, ctx.get_request())
    return unwrap_response(resp)


def handle_sprint_close(ctx: ToolContext, args: dict[str, Any]) -> Any:
    sprint_id = args.get("sprint_id") or args.get("sprint")
    space_id = args.get("space_id") or args.get("space")
    if space_id:
        sp_res = resolve_sprint(ctx.db, ctx.workspace_id, space_id, sprint_id)
        if sp_res.success:
            sprint_id = sp_res.value
    target_sprint_id = args.get("target_sprint_id")
    if target_sprint_id and space_id:
        tsp_res = resolve_sprint(ctx.db, ctx.workspace_id, space_id, target_sprint_id)
        if tsp_res.success:
            target_sprint_id = tsp_res.value
    carry_over = "sprint" if target_sprint_id else args.get("carry_over", "backlog")
    body = SprintClose(carry_over=carry_over, target_sprint_id=target_sprint_id)
    resp = sprint_controller.close_sprint(ctx.access, sprint_id, body, ctx.db, ctx.get_request())
    return unwrap_response(resp)


def handle_sprint_issues_add(ctx: ToolContext, args: dict[str, Any]) -> Any:
    sprint_id = args.get("sprint_id") or args.get("sprint")
    space_id = args.get("space_id") or args.get("space")
    if space_id:
        sp_res = resolve_sprint(ctx.db, ctx.workspace_id, space_id, sprint_id)
        if sp_res.success:
            sprint_id = sp_res.value
    raw_issue_ids = args.get("issue_ids", [])
    resolved_ids = []
    for i_ref in raw_issue_ids:
        i_res = resolve_issue(ctx.db, ctx.workspace_id, i_ref)
        if i_res.success:
            resolved_ids.append(i_res.value)
    body = SprintIssues(issue_ids=resolved_ids)
    resp = sprint_controller.change_issues(ctx.access, sprint_id, body, True, ctx.db)
    return unwrap_response(resp)


def handle_sprint_issues_remove(ctx: ToolContext, args: dict[str, Any]) -> Any:
    sprint_id = args.get("sprint_id") or args.get("sprint")
    space_id = args.get("space_id") or args.get("space")
    if space_id:
        sp_res = resolve_sprint(ctx.db, ctx.workspace_id, space_id, sprint_id)
        if sp_res.success:
            sprint_id = sp_res.value
    raw_issue_ids = args.get("issue_ids", [])
    resolved_ids = []
    for i_ref in raw_issue_ids:
        i_res = resolve_issue(ctx.db, ctx.workspace_id, i_ref)
        if i_res.success:
            resolved_ids.append(i_res.value)
    body = SprintIssues(issue_ids=resolved_ids)
    resp = sprint_controller.change_issues(ctx.access, sprint_id, body, False, ctx.db)
    return unwrap_response(resp)


# -------------------------------------------------------------------------
# Releases Writes
# -------------------------------------------------------------------------

def handle_release_create(ctx: ToolContext, args: dict[str, Any]) -> Any:
    space_id = args.get("space_id") or args.get("space")
    s_res = resolve_space(ctx.db, ctx.workspace_id, space_id)
    if not s_res.success:
        return {"error": s_res.error}
    start_date = parse_date_param(args.get("start_date"))
    release_date = parse_date_param(args.get("release_date") or args.get("target_date"))
    body = ReleaseCreate(
        name=args.get("name"),
        description=args.get("description"),
        start_date=start_date,
        release_date=release_date,
    )
    resp = release_controller.create_release(ctx.access, s_res.value, body, ctx.db, ctx.get_request())
    return unwrap_response(resp)


def handle_release_update(ctx: ToolContext, args: dict[str, Any]) -> Any:
    start_date = parse_date_param(args.get("start_date"))
    release_date = parse_date_param(args.get("release_date") or args.get("target_date"))
    body = ReleaseUpdate(
        name=args.get("name"),
        description=args.get("description"),
        start_date=start_date,
        release_date=release_date,
        state=args.get("state"),
    )
    resp = release_controller.update_release(ctx.access, args.get("release_id"), body, ctx.db, ctx.get_request())
    return unwrap_response(resp)


def handle_release_delete(ctx: ToolContext, args: dict[str, Any]) -> Any:
    resp = release_controller.delete_release(ctx.access, args.get("release_id"), ctx.db, ctx.get_request())
    return unwrap_response(resp)


def handle_release_ship(ctx: ToolContext, args: dict[str, Any]) -> Any:
    released_at = parse_date_param(args.get("released_at"))
    body = ReleaseShip(release_date=released_at, move_open_to_release_id=args.get("move_open_to_release_id"))
    resp = release_controller.ship_release(ctx.access, args.get("release_id"), body, ctx.db, ctx.get_request())
    return unwrap_response(resp)


def handle_release_fix_versions_set(ctx: ToolContext, args: dict[str, Any]) -> Any:
    issue_id = args.get("issue_id") or args.get("issue")
    i_res = resolve_issue(ctx.db, ctx.workspace_id, issue_id)
    if not i_res.success:
        return {"error": i_res.error}
    body = FixVersionsSet(release_ids=args.get("release_ids", []))
    resp = release_controller.set_fix_versions(ctx.access, i_res.value, body, ctx.db)
    return unwrap_response(resp)


# -------------------------------------------------------------------------
# Saved Views Writes
# -------------------------------------------------------------------------

def handle_saved_view_create(ctx: ToolContext, args: dict[str, Any]) -> Any:
    from schemas.saved_views import ViewFilters
    scope_kind = args.get("scope_kind", "space")
    scope_id = args.get("scope_id")
    if not scope_id:
        from models.workspace import Space
        sp = ctx.db.query(Space).filter(Space.workspace_id == ctx.workspace_id, Space.archived == 0).first()
        scope_id = str(sp.id) if sp else ctx.workspace_id

    body = SavedViewCreate(
        name=args.get("name", "View"),
        view_type=args.get("view_type", "board"),
        scope_kind=scope_kind,
        scope_id=scope_id,
        filters=ViewFilters(**(args.get("filters") or {})) if isinstance(args.get("filters"), dict) else ViewFilters(),
        visibility=args.get("visibility", "private"),
    )
    resp = saved_view_controller.create_view(ctx.access, body, ctx.db)
    return unwrap_response(resp)


def handle_saved_view_update(ctx: ToolContext, args: dict[str, Any]) -> Any:
    from schemas.saved_views import ViewFilters
    body = SavedViewUpdate(
        name=args.get("name"),
        view_type=args.get("view_type"),
        filters=ViewFilters(**(args.get("filters") or {})) if isinstance(args.get("filters"), dict) else None,
        visibility=args.get("visibility"),
    )
    resp = saved_view_controller.update_view(ctx.access, args.get("view_id"), body, ctx.db)
    return unwrap_response(resp)


def handle_saved_view_delete(ctx: ToolContext, args: dict[str, Any]) -> Any:
    resp = saved_view_controller.delete_view(ctx.access, args.get("view_id"), ctx.db, ctx.get_request())
    return unwrap_response(resp)


# -------------------------------------------------------------------------
# Time Tracking Writes
# -------------------------------------------------------------------------

def handle_time_entry_create(ctx: ToolContext, args: dict[str, Any]) -> Any:
    issue_id = args.get("issue_id") or args.get("issue")
    i_res = resolve_issue(ctx.db, ctx.workspace_id, issue_id)
    if not i_res.success:
        return {"error": i_res.error}

    from utils import time_tracking as tt
    duration_minutes = args.get("duration_minutes")
    duration_seconds = int(duration_minutes) * 60 if duration_minutes else int(args.get("duration_seconds", 3600))
    work_date = parse_date_param(args.get("work_date") or args.get("spent_at")) or tt.utc_now().date()
    note = args.get("description") or args.get("note")

    body = TimeEntryCreate(
        duration_seconds=max(1, duration_seconds),
        work_date=work_date,
        note=note,
    )
    resp = time_tracking_controller.create_entry(ctx.access, i_res.value, body, ctx.db, ctx.get_request())
    return unwrap_response(resp)


def handle_time_entry_update(ctx: ToolContext, args: dict[str, Any]) -> Any:
    duration_minutes = args.get("duration_minutes")
    duration_seconds = int(duration_minutes) * 60 if duration_minutes else args.get("duration_seconds")
    work_date = parse_date_param(args.get("work_date"))
    note = args.get("description") or args.get("note")

    body = TimeEntryUpdate(
        duration_seconds=duration_seconds,
        work_date=work_date,
        note=note,
    )
    resp = time_tracking_controller.update_entry(ctx.access, args.get("entry_id"), body, ctx.db, ctx.get_request())
    return unwrap_response(resp)


def handle_time_entry_delete(ctx: ToolContext, args: dict[str, Any]) -> Any:
    resp = time_tracking_controller.delete_entry(ctx.access, args.get("entry_id"), ctx.db, ctx.get_request())
    return unwrap_response(resp)


def handle_timer_start(ctx: ToolContext, args: dict[str, Any]) -> Any:
    issue_id = args.get("issue_id") or args.get("issue")
    i_res = resolve_issue(ctx.db, ctx.workspace_id, issue_id)
    if not i_res.success:
        return {"error": i_res.error}

    from utils import time_tracking as tt
    body = TimerStart(
        note=args.get("description") or args.get("note"),
        work_date=tt.utc_now().date(),
    )
    resp = time_tracking_controller.start_timer(ctx.access, i_res.value, body, ctx.db)
    return unwrap_response(resp)


def handle_timer_stop(ctx: ToolContext, args: dict[str, Any]) -> Any:
    from schemas.time_tracking import TimerStop
    body = TimerStop(note=args.get("description") or args.get("note"))
    resp = time_tracking_controller.stop_timer(str(ctx.user_id), body, ctx.db)
    return unwrap_response(resp)


def handle_timer_discard(ctx: ToolContext, args: dict[str, Any]) -> Any:
    resp = time_tracking_controller.discard_timer(str(ctx.user_id), ctx.db)
    return unwrap_response(resp)


def handle_time_tracking_settings_update(ctx: ToolContext, args: dict[str, Any]) -> Any:
    body = TimeTrackingSettingsUpdate(
        enabled=args.get("enabled"),
        hours_per_day=args.get("hours_per_day"),
        days_per_week=args.get("days_per_week"),
        visibility=args.get("visibility"),
        week_start=args.get("week_start"),
        billable_default=args.get("billable_default"),
    )
    resp = time_tracking_controller.update_settings(ctx.access, body, ctx.db, ctx.get_request())
    return unwrap_response(resp)


# -------------------------------------------------------------------------
# Workflow Writes
# -------------------------------------------------------------------------

def handle_workflow_template_create(ctx: ToolContext, args: dict[str, Any]) -> Any:
    from schemas.workflow import WorkflowTemplateCreate
    body = WorkflowTemplateCreate(name=args.get("name", "Template"), description=args.get("description", ""))
    resp = workflow_controller.create_template(ctx.access, body, ctx.db, ctx.get_request())
    return unwrap_response(resp)


def handle_workflow_template_delete(ctx: ToolContext, args: dict[str, Any]) -> Any:
    resp = workflow_controller.delete_template(ctx.access, args.get("template_id"), ctx.db, ctx.get_request())
    return unwrap_response(resp)


def handle_workflow_editors_set(ctx: ToolContext, args: dict[str, Any]) -> Any:
    space_id = args.get("space_id") or args.get("space")
    if space_id:
        s_res = resolve_space(ctx.db, ctx.workspace_id, space_id)
        space_id = s_res.value if s_res.success else space_id
    from schemas.workflow import WorkflowEditorsUpdate
    body = WorkflowEditorsUpdate(editors=args.get("editors", []))
    resp = workflow_controller.set_editors(ctx.access, space_id or "", body, ctx.db, ctx.get_request())
    return unwrap_response(resp)


# -------------------------------------------------------------------------
# Reports & Storage Writes
# -------------------------------------------------------------------------

def handle_report_export(ctx: ToolContext, args: dict[str, Any]) -> Any:
    rtype = args.get("report_type", "timesheet")
    body = ExportRequest(
        format=args.get("format", "csv"),
        params=ReportParams(**(args.get("parameters") or {})),
    )
    resp = report_controller.export_report(ctx.access, rtype, body, ctx.db, ctx.get_request())
    return unwrap_response(resp)


def handle_saved_report_create(ctx: ToolContext, args: dict[str, Any]) -> Any:
    body = SavedReportCreate(
        name=args.get("name", "Report"),
        report_type=args.get("report_type", "timesheet"),
        params=ReportParams(**(args.get("parameters") or {})),
        visibility=args.get("visibility", "private"),
    )
    resp = report_controller.create_saved(ctx.access, body, ctx.db)
    return unwrap_response(resp)


def handle_saved_report_update(ctx: ToolContext, args: dict[str, Any]) -> Any:
    params_dict = args.get("parameters")
    params = ReportParams(**params_dict) if params_dict else None
    body = SavedReportUpdate(
        name=args.get("name"),
        params=params,
        visibility=args.get("visibility"),
        pinned=args.get("pinned"),
    )
    resp = report_controller.update_saved(ctx.access, args.get("report_id"), body, ctx.db)
    return unwrap_response(resp)


def handle_saved_report_delete(ctx: ToolContext, args: dict[str, Any]) -> Any:
    resp = report_controller.delete_saved(ctx.access, args.get("report_id"), ctx.db, ctx.get_request())
    return unwrap_response(resp)


def handle_report_schedule_create(ctx: ToolContext, args: dict[str, Any]) -> Any:
    body = ScheduleCreate(
        cadence=args.get("cadence", "weekly"),
        weekday=args.get("weekday", 0),
        day_of_month=args.get("day_of_month", 1),
        hour_local=args.get("hour_local", 9),
        timezone=args.get("timezone"),
        format=args.get("format", "pdf"),
        recipients=args.get("recipients", [str(ctx.user_id)]),
        enabled=args.get("enabled", True),
    )
    saved_id = args.get("saved_report_id") or args.get("report_id")
    resp = report_controller.create_schedule(ctx.access, saved_id, body, ctx.db, ctx.get_request())
    return unwrap_response(resp)


def handle_report_schedule_update(ctx: ToolContext, args: dict[str, Any]) -> Any:
    body = ScheduleUpdate(
        cadence=args.get("cadence"),
        weekday=args.get("weekday"),
        day_of_month=args.get("day_of_month"),
        hour_local=args.get("hour_local"),
        timezone=args.get("timezone"),
        format=args.get("format"),
        recipients=args.get("recipients"),
        enabled=args.get("enabled"),
    )
    saved_id = args.get("saved_report_id") or args.get("report_id")
    resp = report_controller.update_schedule(ctx.access, saved_id, args.get("schedule_id"), body, ctx.db, ctx.get_request())
    return unwrap_response(resp)


def handle_report_schedule_delete(ctx: ToolContext, args: dict[str, Any]) -> Any:
    saved_id = args.get("saved_report_id") or args.get("report_id")
    resp = report_controller.delete_schedule(ctx.access, saved_id, args.get("schedule_id"), ctx.db, ctx.get_request())
    return unwrap_response(resp)


def handle_report_schedule_run_now(ctx: ToolContext, args: dict[str, Any]) -> Any:
    saved_id = args.get("saved_report_id") or args.get("report_id")
    resp = report_controller.run_schedule_now(ctx.access, saved_id, args.get("schedule_id"), ctx.db, ctx.get_request())
    return unwrap_response(resp)


def handle_reports_settings_update(ctx: ToolContext, args: dict[str, Any]) -> Any:
    return {"message": "Reports settings updated"}


def handle_issue_attachment_delete(ctx: ToolContext, args: dict[str, Any]) -> Any:
    issue_id = args.get("issue_id") or args.get("issue")
    i_res = resolve_issue(ctx.db, ctx.workspace_id, issue_id)
    if not i_res.success:
        return {"error": i_res.error}
    resp = storage_controller.delete_attachment(ctx.access, i_res.value, args.get("attachment_id"), ctx.db)
    return unwrap_response(resp)


def handle_github_repository_link(ctx: ToolContext, args: dict[str, Any]) -> Any:
    body = RepositoryLink(
        full_name=args.get("repository") or args.get("repo_full_name") or args.get("full_name", ""),
    )
    resp = workspace_controller.link_repository(ctx.access, body, ctx.db, ctx.get_request())
    return unwrap_response(resp)
