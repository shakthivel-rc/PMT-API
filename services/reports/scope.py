"""Scope and filters → tenant-filtered queries (docs/design/08-reports.md §5.3, §6).

Every report reads issues through `issue_query` and time through `entry_query`, so the
tenancy filter, the soft-delete filter and the filters of `ReportParams` are written once.
A scope id from another workspace resolves to nothing and the controller answers 404, the
same as every other id in the API (CLAUDE.md §8).
"""
from __future__ import annotations

from collections.abc import Iterable

from sqlalchemy import or_
from sqlalchemy.orm import Query, Session

from models.hierarchy import Folder, ProjectList, Space, WorkflowStatus
from models.issue import Issue
from models.sprint import Sprint, SprintIssue
from models.time_tracking import TimeEntry
from models.workspace import Workspace
from services import sprint as sprint_svc
from services import time_tracking as tt_svc
from services.reports.base import ReportContext, ReportError, Scope


def resolve_scope(db: Session, workspace_id: str, ref: str) -> Scope:
    if ref == "workspace":
        workspace = db.query(Workspace).filter(Workspace.id == workspace_id).one()
        return Scope(kind="workspace", id=None, name=str(workspace.name))
    kind, _, ident = ref.partition(":")
    if kind == "space":
        space = db.query(Space).filter(Space.workspace_id == workspace_id, Space.id == ident,
                                       Space.deleted == 0).first()
        if space is None:
            raise ReportError(404, "Project not found")
        return Scope(kind="space", id=space.id, name=str(space.name), space_id=space.id,
                     space=space)
    if kind == "folder":
        folder = db.query(Folder).filter(Folder.workspace_id == workspace_id,
                                         Folder.id == ident, Folder.deleted == 0).first()
        if folder is None:
            raise ReportError(404, "Folder not found")
        lists = [list_id for (list_id,) in db.query(ProjectList.id).filter(
            ProjectList.workspace_id == workspace_id, ProjectList.folder_id == folder.id,
            ProjectList.deleted == 0).all()]
        return Scope(kind="folder", id=folder.id, name=str(folder.name),
                     space_id=folder.space_id, list_ids=lists)
    if kind == "list":
        item = db.query(ProjectList).filter(ProjectList.workspace_id == workspace_id,
                                            ProjectList.id == ident,
                                            ProjectList.deleted == 0).first()
        if item is None:
            raise ReportError(404, "List not found")
        return Scope(kind="list", id=item.id, name=str(item.name), space_id=item.space_id,
                     list_ids=[item.id])
    if kind == "sprint":
        sprint = sprint_svc.get_sprint(db, workspace_id, ident)
        if sprint is None:
            raise ReportError(404, "Sprint not found")
        return Scope(kind="sprint", id=sprint.id, name=str(sprint.name),
                     space_id=sprint.space_id, sprint=sprint)
    raise ReportError(422, "Unknown scope")


def sprint_member_ids(db: Session, sprint: Sprint) -> list[str]:
    """Every issue that was ever in the sprint, removed ones included."""
    return [issue_id for (issue_id,) in db.query(SprintIssue.issue_id).filter(
        SprintIssue.workspace_id == sprint.workspace_id,
        SprintIssue.sprint_id == sprint.id).distinct().all()]


def apply_scope(query: Query, ctx: ReportContext) -> Query:
    scope = ctx.scope
    if scope.kind == "space":
        query = query.filter(Issue.space_id == scope.space_id)
    elif scope.kind in ("folder", "list"):
        query = query.filter(Issue.list_id.in_(scope.list_ids or ["-"]))
    elif scope.kind == "sprint" and scope.sprint is not None:
        query = query.filter(Issue.id.in_(sprint_member_ids(ctx.db, scope.sprint) or ["-"]))
    return query


def apply_filters(query: Query, ctx: ReportContext, *, status_categories: bool = True) -> Query:
    params = ctx.params
    if params.assignee_ids:
        wanted = [a for a in params.assignee_ids if a != "none"]
        clauses = []
        if wanted:
            clauses.append(Issue.assignee_id.in_(wanted))
        if "none" in params.assignee_ids:
            clauses.append(Issue.assignee_id.is_(None))
        query = query.filter(or_(*clauses))
    if params.reporter_ids:
        query = query.filter(Issue.reporter_id.in_(params.reporter_ids))
    if params.types:
        query = query.filter(Issue.type.in_(params.types))
    if params.priorities:
        query = query.filter(Issue.priority.in_(params.priorities))
    if params.issue_ids:
        query = query.filter(Issue.id.in_(params.issue_ids))
    if params.epic_id:
        below = tt_svc.descendant_ids(ctx.db, ctx.access.workspace_id, params.epic_id)
        query = query.filter(Issue.id.in_([params.epic_id, *below]))
    if status_categories and params.status_categories:
        ids = [status_id for (status_id,) in ctx.db.query(WorkflowStatus.id).filter(
            WorkflowStatus.workspace_id == ctx.access.workspace_id,
            WorkflowStatus.category.in_(params.status_categories)).all()]
        query = query.filter(Issue.status_id.in_(ids or ["-"]))
    return query


def issue_query(ctx: ReportContext, *, columns: Iterable | None = None,
                status_categories: bool = True) -> Query:
    """Live issues in the scope that pass the filters."""
    query = ctx.db.query(*columns) if columns is not None else ctx.db.query(Issue)
    query = query.filter(Issue.workspace_id == ctx.access.workspace_id, Issue.deleted == 0)
    query = apply_scope(query, ctx)
    return apply_filters(query, ctx, status_categories=status_categories)


def entry_query(ctx: ReportContext, *columns, restrict: bool = True) -> Query:
    """Live time entries on live issues in the scope that pass the filters and the range.

    `restrict=False` drops only the `own` visibility filter — for the team total a member
    may see under `own` (the totals still show, docs/design/07-time-tracking.md).
    """
    query = tt_svc.entries_query(ctx.db, ctx.access.workspace_id)
    if columns:
        query = query.with_entities(*columns)
    query = apply_filters(apply_scope(query, ctx), ctx)
    params = ctx.params
    if ctx.date_from is not None:
        query = query.filter(TimeEntry.work_date >= ctx.date_from)
    if ctx.date_to is not None:
        query = query.filter(TimeEntry.work_date <= ctx.date_to)
    if params.user_ids:
        query = query.filter(TimeEntry.user_id.in_(params.user_ids))
    if params.billable == "billable":
        query = query.filter(TimeEntry.billable.is_(True))
    elif params.billable == "non_billable":
        query = query.filter(TimeEntry.billable.is_(False))
    if restrict and ctx.only_user_id is not None:
        query = query.filter(TimeEntry.user_id == ctx.only_user_id)
    return query


def statuses(ctx: ReportContext, space_ids: Iterable[str] | None = None) -> list[WorkflowStatus]:
    query = ctx.db.query(WorkflowStatus).filter(
        WorkflowStatus.workspace_id == ctx.access.workspace_id)
    ids = list(space_ids) if space_ids is not None else None
    if ids is not None:
        query = query.filter(WorkflowStatus.space_id.in_(ids or ["-"]))
    elif ctx.scope.space_id:
        query = query.filter(WorkflowStatus.space_id == ctx.scope.space_id)
    return query.order_by(WorkflowStatus.space_id, WorkflowStatus.position).all()


def category_by_status(ctx: ReportContext) -> dict[str, str]:
    rows = ctx.db.query(WorkflowStatus.id, WorkflowStatus.category).filter(
        WorkflowStatus.workspace_id == ctx.access.workspace_id).all()
    return dict(rows)
