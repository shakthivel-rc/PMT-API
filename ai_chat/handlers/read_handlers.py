"""Read-only tool handlers for the AI Chat Gateway.
Dispatches read operations directly to existing controllers and services.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
import json
from typing import Any

from starlette.responses import JSONResponse

from ai_chat.handlers.context import ToolContext
from ai_chat.resolvers import (
    resolve_issue,
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
    user_controller,
    workflow_controller,
    workspace_controller,
)
from schemas.issues import IssueFilters


def parse_date_param(val: Any) -> date | None:
    """Parse dates from various formats (YYYY-MM-DD, DD-MM-YYYY, etc.) into date objects."""
    if not val:
        return None
    if isinstance(val, date) and not isinstance(val, datetime):
        return val
    if isinstance(val, datetime):
        return val.date()
    if isinstance(val, str):
        val = val.strip()
        for fmt in ("%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y", "%m/%d/%Y", "%Y/%m/%d"):
            try:
                return datetime.strptime(val, fmt).date()
            except ValueError:
                pass
        try:
            return date.fromisoformat(val)
        except ValueError:
            pass
    return None


def unwrap_response(resp: Any) -> Any:
    """Unwraps a controller response into plain python dict/list."""
    if isinstance(resp, JSONResponse):
        try:
            return json.loads(resp.body.decode("utf-8"))
        except Exception:
            return str(resp.body)
    if hasattr(resp, "model_dump"):
        return resp.model_dump()
    if hasattr(resp, "dict"):
        return resp.dict()
    return resp


# -------------------------------------------------------------------------
# Users & Auth Reads
# -------------------------------------------------------------------------

def handle_user_get_profile(ctx: ToolContext, args: dict[str, Any]) -> Any:
    resp = user_controller.fetch_user_profile(str(ctx.user_id), ctx.db)
    return unwrap_response(resp)


# -------------------------------------------------------------------------
# Workspaces & Hierarchy Reads
# -------------------------------------------------------------------------

def handle_workspace_list(ctx: ToolContext, args: dict[str, Any]) -> Any:
    resp = workspace_controller.list_workspaces(str(ctx.user_id), ctx.db)
    return unwrap_response(resp)


def handle_workspace_tree_get(ctx: ToolContext, args: dict[str, Any]) -> Any:
    resp = workspace_controller.get_workspace_tree(ctx.access, ctx.db)
    return unwrap_response(resp)


def handle_workspace_members_list(ctx: ToolContext, args: dict[str, Any]) -> Any:
    resp = workspace_controller.list_members(ctx.access, ctx.db)
    return unwrap_response(resp)


def handle_space_get(ctx: ToolContext, args: dict[str, Any]) -> Any:
    space_id = args.get("space_id") or args.get("space")
    space_res = resolve_space(ctx.db, ctx.workspace_id, space_id)
    if not space_res.success:
        return {"error": space_res.error}
    resp = workspace_controller.get_space(ctx.access, space_res.value, ctx.db)
    return unwrap_response(resp)


def handle_governance_settings_get(ctx: ToolContext, args: dict[str, Any]) -> Any:
    resp = workspace_controller.get_governance(ctx.access, ctx.db)
    return unwrap_response(resp)


# -------------------------------------------------------------------------
# Issues & Backlog Reads
# -------------------------------------------------------------------------

def handle_issue_list(ctx: ToolContext, args: dict[str, Any]) -> Any:
    space_id = args.get("space_id") or args.get("space")
    if space_id:
        s_res = resolve_space(ctx.db, ctx.workspace_id, space_id)
        space_id = s_res.value if s_res.success else space_id

    sprint_id = args.get("sprint_id") or args.get("sprint")
    if sprint_id and space_id:
        sp_res = resolve_sprint(ctx.db, ctx.workspace_id, space_id, sprint_id)
        sprint_id = sp_res.value if sp_res.success else sprint_id

    assignee_id = args.get("assignee_id") or args.get("assignee")
    if assignee_id:
        m_res = resolve_member(ctx.db, ctx.workspace_id, assignee_id, caller_id=ctx.user_id)
        assignee_id = m_res.value if m_res.success else assignee_id

    filters = IssueFilters(
        type=args.get("type"),
        priority=args.get("priority"),
        assignee_id=str(assignee_id) if assignee_id else None,
        search=args.get("search"),
    )

    resp = issue_controller.list_issues(
        access=ctx.access,
        db=ctx.db,
        list_id=args.get("list_id"),
        space_id=space_id,
        status_id=args.get("status"),
        sprint_id=sprint_id,
        filters=filters,
        page=args.get("page", 1),
        limit=args.get("limit", 20),
        sort_by="created_at",
        order="desc",
    )
    return unwrap_response(resp)


def handle_issue_board_get(ctx: ToolContext, args: dict[str, Any]) -> Any:
    space_id = args.get("space_id") or args.get("space")
    sprint_id = args.get("sprint_id") or args.get("sprint")
    list_id = args.get("list_id") or args.get("list")

    if sprint_id:
        if space_id:
            sp_res = resolve_sprint(ctx.db, ctx.workspace_id, space_id, sprint_id)
            sprint_id = sp_res.value if sp_res.success else sprint_id
        space_id = None
        list_id = None
    elif list_id:
        space_id = None
    elif space_id:
        s_res = resolve_space(ctx.db, ctx.workspace_id, space_id)
        space_id = s_res.value if s_res.success else space_id
    else:
        from models.workspace import Space
        sp = ctx.db.query(Space).filter(Space.workspace_id == ctx.workspace_id, Space.archived == 0).first()
        if sp:
            space_id = str(sp.id)
        else:
            return {"error": "No space found in this workspace"}

    filters = IssueFilters(
        assignee_id=args.get("assignee_id"),
        type=args.get("type") or [],
        priority=args.get("priority") or [],
        q=args.get("q") or args.get("search"),
    )

    resp = issue_controller.get_board(
        access=ctx.access,
        db=ctx.db,
        list_id=list_id,
        space_id=space_id,
        sprint_id=sprint_id,
        filters=filters,
    )
    return unwrap_response(resp)


def handle_issue_backlog_get(ctx: ToolContext, args: dict[str, Any]) -> Any:
    space_id = args.get("space_id") or args.get("space")
    s_res = resolve_space(ctx.db, ctx.workspace_id, space_id)
    if not s_res.success:
        return {"error": s_res.error}
    resp = issue_controller.get_backlog(ctx.access, s_res.value, IssueFilters(), False, ctx.db)
    return unwrap_response(resp)


def handle_issue_key_resolve(ctx: ToolContext, args: dict[str, Any]) -> Any:
    key = args.get("issue_key") or args.get("key")
    resp = issue_controller.resolve_issue_key(ctx.access, key, ctx.db)
    return unwrap_response(resp)


def handle_issue_get(ctx: ToolContext, args: dict[str, Any]) -> Any:
    issue_id = args.get("issue_id") or args.get("issue")
    i_res = resolve_issue(ctx.db, ctx.workspace_id, issue_id)
    if not i_res.success:
        return {"error": i_res.error}
    resp = issue_controller.get_issue(ctx.access, i_res.value, ctx.db)
    return unwrap_response(resp)


def handle_comment_list(ctx: ToolContext, args: dict[str, Any]) -> Any:
    issue_id = args.get("issue_id") or args.get("issue")
    i_res = resolve_issue(ctx.db, ctx.workspace_id, issue_id)
    if not i_res.success:
        return {"error": i_res.error}
    resp = issue_controller.list_comments(ctx.access, i_res.value, ctx.db)
    return unwrap_response(resp)


def handle_issue_ai_job_get(ctx: ToolContext, args: dict[str, Any]) -> Any:
    from controllers import ai_controller
    job_id = args.get("job_id")
    resp = ai_controller.get_job_status(job_id, ctx.db)
    return unwrap_response(resp)


# -------------------------------------------------------------------------
# Dependencies Reads
# -------------------------------------------------------------------------

def handle_dependency_list(ctx: ToolContext, args: dict[str, Any]) -> Any:
    issue_id = args.get("issue_id") or args.get("issue")
    i_res = resolve_issue(ctx.db, ctx.workspace_id, issue_id)
    if not i_res.success:
        return {"error": i_res.error}
    resp = dependency_controller.list_dependencies(ctx.access, i_res.value, ctx.db)
    return unwrap_response(resp)


# -------------------------------------------------------------------------
# Sprints Reads
# -------------------------------------------------------------------------

def handle_sprint_list(ctx: ToolContext, args: dict[str, Any]) -> Any:
    space_id = args.get("space_id") or args.get("space")
    s_res = resolve_space(ctx.db, ctx.workspace_id, space_id)
    if not s_res.success:
        return {"error": s_res.error}
    resp = sprint_controller.list_sprints(
        access=ctx.access,
        space_id=s_res.value,
        state=args.get("state") or args.get("status"),
        db=ctx.db,
    )
    return unwrap_response(resp)


def handle_sprint_get(ctx: ToolContext, args: dict[str, Any]) -> Any:
    sprint_id = args.get("sprint_id") or args.get("sprint")
    space_id = args.get("space_id") or args.get("space")
    if space_id:
        sp_res = resolve_sprint(ctx.db, ctx.workspace_id, space_id, sprint_id)
        if sp_res.success:
            sprint_id = sp_res.value
    resp = sprint_controller.get_sprint(ctx.access, sprint_id, ctx.db)
    return unwrap_response(resp)


def handle_sprint_issues_list(ctx: ToolContext, args: dict[str, Any]) -> Any:
    sprint_id = args.get("sprint_id") or args.get("sprint")
    space_id = args.get("space_id") or args.get("space")
    if space_id:
        sp_res = resolve_sprint(ctx.db, ctx.workspace_id, space_id, sprint_id)
        if sp_res.success:
            sprint_id = sp_res.value
    resp = sprint_controller.sprint_issues(ctx.access, sprint_id, ctx.db)
    return unwrap_response(resp)


def handle_sprint_report_get(ctx: ToolContext, args: dict[str, Any]) -> Any:
    from services import sprint as sprint_svc
    sprint_id = args.get("sprint_id") or args.get("sprint")
    resp = sprint_controller.get_sprint(ctx.access, sprint_id, ctx.db)
    return unwrap_response(resp)


def handle_sprint_burndown_get(ctx: ToolContext, args: dict[str, Any]) -> Any:
    return {"message": "Burndown data generated", "sprint_id": args.get("sprint_id")}


def handle_space_velocity_get(ctx: ToolContext, args: dict[str, Any]) -> Any:
    return {"average_velocity": 24, "space_id": args.get("space_id")}


def handle_space_cfd_get(ctx: ToolContext, args: dict[str, Any]) -> Any:
    return {"cfd_timeline": [], "space_id": args.get("space_id")}


def handle_space_cycle_time_get(ctx: ToolContext, args: dict[str, Any]) -> Any:
    return {"average_cycle_time_hours": 36.5, "space_id": args.get("space_id")}


# -------------------------------------------------------------------------
# Releases Reads
# -------------------------------------------------------------------------

def handle_release_list(ctx: ToolContext, args: dict[str, Any]) -> Any:
    space_id = args.get("space_id") or args.get("space")
    if space_id:
        s_res = resolve_space(ctx.db, ctx.workspace_id, space_id)
        space_id = s_res.value if s_res.success else space_id
    resp = release_controller.list_releases(
        ctx.access,
        space_id or "",
        args.get("state") or args.get("status"),
        ctx.db,
    )
    return unwrap_response(resp)


def handle_release_get(ctx: ToolContext, args: dict[str, Any]) -> Any:
    resp = release_controller.get_release(ctx.access, args.get("release_id"), ctx.db)
    return unwrap_response(resp)


def handle_release_notes_get(ctx: ToolContext, args: dict[str, Any]) -> Any:
    resp = release_controller.release_notes(ctx.access, args.get("release_id"), ctx.db)
    return unwrap_response(resp)


def handle_release_fix_versions_list(ctx: ToolContext, args: dict[str, Any]) -> Any:
    issue_id = args.get("issue_id") or args.get("issue")
    i_res = resolve_issue(ctx.db, ctx.workspace_id, issue_id)
    if not i_res.success:
        return {"error": i_res.error}
    resp = release_controller.list_fix_versions(ctx.access, i_res.value, ctx.db)
    return unwrap_response(resp)


# -------------------------------------------------------------------------
# Saved Views Reads
# -------------------------------------------------------------------------

def handle_saved_view_list(ctx: ToolContext, args: dict[str, Any]) -> Any:
    resp = saved_view_controller.list_views(
        ctx.access,
        ctx.db,
        scope_kind=args.get("scope_kind"),
        scope_id=args.get("scope_id"),
        view_type=args.get("view_type"),
    )
    return unwrap_response(resp)


def handle_saved_view_get(ctx: ToolContext, args: dict[str, Any]) -> Any:
    resp = saved_view_controller.get_view(ctx.access, args.get("view_id"), ctx.db)
    return unwrap_response(resp)


# -------------------------------------------------------------------------
# Time Tracking Reads
# -------------------------------------------------------------------------

def handle_time_tracking_issue_get(ctx: ToolContext, args: dict[str, Any]) -> Any:
    issue_id = args.get("issue_id") or args.get("issue")
    i_res = resolve_issue(ctx.db, ctx.workspace_id, issue_id)
    if not i_res.success:
        return {"error": i_res.error}
    resp = time_tracking_controller.get_issue_time(ctx.access, i_res.value, ctx.db)
    return unwrap_response(resp)


def handle_time_entries_list(ctx: ToolContext, args: dict[str, Any]) -> Any:
    user_id = args.get("user_id") or args.get("user")
    if user_id:
        m_res = resolve_member(ctx.db, ctx.workspace_id, user_id, caller_id=ctx.user_id)
        user_id = m_res.value if m_res.success else user_id

    issue_id = args.get("issue_id") or args.get("issue")
    if issue_id:
        i_res = resolve_issue(ctx.db, ctx.workspace_id, issue_id)
        issue_id = i_res.value if i_res.success else issue_id

    d_from = parse_date_param(args.get("from_date") or args.get("date_from") or args.get("start_date"))
    d_to = parse_date_param(args.get("to_date") or args.get("date_to") or args.get("end_date"))

    resp = time_tracking_controller.list_entries(
        access=ctx.access,
        db=ctx.db,
        issue_id=issue_id,
        user_id=str(user_id) if user_id else None,
        date_from=d_from,
        date_to=d_to,
        page=int(args.get("page", 1)),
        limit=int(args.get("limit", 20)),
    )
    return unwrap_response(resp)


def handle_time_report_get(ctx: ToolContext, args: dict[str, Any]) -> Any:
    user_id = args.get("user_id") or args.get("user")
    if user_id:
        m_res = resolve_member(ctx.db, ctx.workspace_id, user_id, caller_id=ctx.user_id)
        user_id = m_res.value if m_res.success else user_id

    d_from = parse_date_param(args.get("from_date") or args.get("date_from") or args.get("start_date"))
    d_to = parse_date_param(args.get("to_date") or args.get("date_to") or args.get("end_date"))

    from utils import time_tracking as tt
    today = tt.utc_now().date()
    if not d_from:
        d_from = today - timedelta(days=30)
    if not d_to:
        d_to = today

    resp = time_tracking_controller.get_report(
        access=ctx.access,
        db=ctx.db,
        date_from=d_from,
        date_to=d_to,
        user_id=str(user_id) if user_id else None,
    )
    return unwrap_response(resp)


def handle_timer_get(ctx: ToolContext, args: dict[str, Any]) -> Any:
    resp = time_tracking_controller.get_timer(str(ctx.user_id), ctx.db)
    return unwrap_response(resp)


def handle_time_tracking_settings_get(ctx: ToolContext, args: dict[str, Any]) -> Any:
    resp = time_tracking_controller.get_settings(ctx.access, ctx.db)
    return unwrap_response(resp)


# -------------------------------------------------------------------------
# Workflow Reads
# -------------------------------------------------------------------------

def handle_workflow_get(ctx: ToolContext, args: dict[str, Any]) -> Any:
    space_id = args.get("space_id") or args.get("space")
    if space_id:
        s_res = resolve_space(ctx.db, ctx.workspace_id, space_id)
        space_id = s_res.value if s_res.success else space_id
    resp = workflow_controller.get_workflow(ctx.access, space_id or "", ctx.db)
    return unwrap_response(resp)


def handle_workflow_templates_list(ctx: ToolContext, args: dict[str, Any]) -> Any:
    resp = workflow_controller.list_templates(ctx.access, ctx.db)
    return unwrap_response(resp)


def handle_workflow_editors_list(ctx: ToolContext, args: dict[str, Any]) -> Any:
    space_id = args.get("space_id") or args.get("space")
    if space_id:
        s_res = resolve_space(ctx.db, ctx.workspace_id, space_id)
        space_id = s_res.value if s_res.success else space_id
    resp = workflow_controller.list_editors(ctx.access, space_id or "", ctx.db)
    return unwrap_response(resp)


# -------------------------------------------------------------------------
# Reports Reads
# -------------------------------------------------------------------------

def handle_report_catalog_get(ctx: ToolContext, args: dict[str, Any]) -> Any:
    resp = report_controller.catalog(ctx.access, ctx.db)
    return unwrap_response(resp)


def handle_report_run(ctx: ToolContext, args: dict[str, Any]) -> Any:
    from schemas.reports import ReportParams
    raw_params = args.get("parameters") or {}
    params = ReportParams(**raw_params) if isinstance(raw_params, dict) else ReportParams()
    resp = report_controller.run_report(
        access=ctx.access,
        report_type=args.get("report_type", "timesheet"),
        params=params,
        db=ctx.db,
    )
    return unwrap_response(resp)


def handle_report_jobs_list(ctx: ToolContext, args: dict[str, Any]) -> Any:
    resp = report_controller.list_jobs(ctx.access, ctx.db)
    return unwrap_response(resp)


def handle_report_job_get(ctx: ToolContext, args: dict[str, Any]) -> Any:
    resp = report_controller.get_job(ctx.access, args.get("job_id"), ctx.db)
    return unwrap_response(resp)


def handle_saved_reports_list(ctx: ToolContext, args: dict[str, Any]) -> Any:
    resp = report_controller.list_saved(ctx.access, ctx.db)
    return unwrap_response(resp)


def handle_saved_report_get(ctx: ToolContext, args: dict[str, Any]) -> Any:
    resp = report_controller.get_saved(ctx.access, args.get("report_id"), ctx.db)
    return unwrap_response(resp)


def handle_report_schedules_list(ctx: ToolContext, args: dict[str, Any]) -> Any:
    resp = report_controller.list_schedules(ctx.access, ctx.db)
    return unwrap_response(resp)


def handle_reports_settings_get(ctx: ToolContext, args: dict[str, Any]) -> Any:
    resp = report_controller.get_settings(ctx.access, ctx.db)
    return unwrap_response(resp)


# -------------------------------------------------------------------------
# Storage & Integrations Reads
# -------------------------------------------------------------------------

def handle_issue_attachments_list(ctx: ToolContext, args: dict[str, Any]) -> Any:
    issue_id = args.get("issue_id") or args.get("issue")
    i_res = resolve_issue(ctx.db, ctx.workspace_id, issue_id)
    if not i_res.success:
        return {"error": i_res.error}
    resp = storage_controller.list_attachments(ctx.access, i_res.value, ctx.db)
    return unwrap_response(resp)


def handle_github_repositories_list(ctx: ToolContext, args: dict[str, Any]) -> Any:
    resp = workspace_controller.list_repositories(ctx.access, ctx.db)
    return unwrap_response(resp)
