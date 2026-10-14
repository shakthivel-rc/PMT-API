"""Interactive timeline / Gantt view and cascade rescheduling service."""
from __future__ import annotations

from collections import defaultdict, deque
from datetime import date, timedelta
from typing import Any

from sqlalchemy.orm import Session

from models.hierarchy import ProjectList, Space, WorkflowStatus
from models.issue import Issue, IssueEvent, IssueLink
from models.user import User
from schemas.timeline import (
    RescheduleRequest,
    RescheduleResponse,
    TimelineDependencyItem,
    TimelineGroup,
    TimelineIssueItem,
    TimelineResponse,
)
from services import write_guard
from utils.issue_keys import format_issue_key


def _calculate_critical_path(issues: list[Issue], links: list[IssueLink]) -> list[str]:
    """Calculate the critical path (longest path) in the DAG formed by 'blocks' links."""
    issue_map = {i.id: i for i in issues}
    valid_ids = set(issue_map.keys())

    # Build adjacency list for 'blocks' (source blocks target => source -> target)
    graph: dict[str, list[str]] = defaultdict(list)
    in_degree: dict[str, int] = {i_id: 0 for i_id in valid_ids}

    for link in links:
        if link.kind == "blocks" and link.source_issue_id in valid_ids and link.target_issue_id in valid_ids:
            graph[link.source_issue_id].append(link.target_issue_id)
            in_degree[link.target_issue_id] += 1

    # Duration of each node in days
    def get_duration(i_id: str) -> int:
        issue = issue_map[i_id]
        if issue.start_date and issue.due_date:
            return max(1, (issue.due_date - issue.start_date).days + 1)
        return max(1, int(issue.story_points or 1))

    # Topological sort using Kahn's algorithm
    queue = deque([i_id for i_id, deg in in_degree.items() if deg == 0])
    topo_order: list[str] = []
    while queue:
        u = queue.popleft()
        topo_order.append(u)
        for v in graph[u]:
            in_degree[v] -= 1
            if in_degree[v] == 0:
                queue.append(v)

    if not topo_order:
        return []

    # Dynamic programming for longest path
    dist: dict[str, int] = {i_id: get_duration(i_id) for i_id in valid_ids}
    parent: dict[str, str | None] = {i_id: None for i_id in valid_ids}

    for u in topo_order:
        u_dist = dist[u]
        for v in graph[u]:
            v_dur = get_duration(v)
            if u_dist + v_dur > dist[v]:
                dist[v] = u_dist + v_dur
                parent[v] = u

    # Find the node with the maximum distance
    if not dist:
        return []

    max_node = max(dist, key=dist.get)
    if dist[max_node] <= 1 and not graph:
        return []

    # Reconstruct path
    path: list[str] = []
    curr: str | None = max_node
    while curr is not None:
        path.append(curr)
        curr = parent[curr]

    path.reverse()
    return path if len(path) > 1 else []


def _to_timeline_item(
    issue: Issue,
    space: Space,
    statuses: dict[str, WorkflowStatus],
    lists: dict[str, ProjectList],
    users: dict[str, User],
    epics: dict[str, Issue],
    critical_path_ids: set[str],
) -> TimelineIssueItem:
    st = statuses.get(issue.status_id)
    ls = lists.get(issue.list_id)
    usr = users.get(issue.assignee_id) if issue.assignee_id else None
    ep = epics.get(issue.parent_id) if issue.parent_id else None

    duration_days = 1
    if issue.start_date and issue.due_date:
        duration_days = max(1, (issue.due_date - issue.start_date).days + 1)

    return TimelineIssueItem(
        id=issue.id,
        key=format_issue_key(space.key, issue.number),
        title=issue.title,
        type=issue.type,
        status_id=issue.status_id,
        status_name=st.name if st else "Unknown",
        status_category=st.category if st else "todo",
        priority=issue.priority,
        start_date=issue.start_date,
        due_date=issue.due_date,
        duration_days=duration_days,
        story_points=issue.story_points,
        assignee_id=issue.assignee_id,
        assignee_name=usr.display_name if usr else (usr.username if usr else None),
        epic_id=issue.parent_id if ep else None,
        epic_title=ep.title if ep else None,
        list_id=issue.list_id,
        list_name=ls.name if ls else "Default",
        is_blocked=bool(issue.blocked),
        is_critical_path=issue.id in critical_path_ids,
    )


def get_timeline(
    db: Session,
    workspace_id: str,
    space_id: str,
    start_date: date | None = None,
    end_date: date | None = None,
    zoom: str = "week",
    group_by: str = "none",
) -> TimelineResponse:
    space = (
        db.query(Space)
        .filter(Space.id == space_id, Space.workspace_id == workspace_id, Space.deleted == 0)
        .first()
    )
    if not space:
        raise ValueError("Space not found")

    issues_query = db.query(Issue).filter(
        Issue.workspace_id == workspace_id,
        Issue.space_id == space_id,
        Issue.deleted == 0,
    )
    all_issues = issues_query.all()

    issue_ids = [i.id for i in all_issues]
    links: list[IssueLink] = []
    if issue_ids:
        links = (
            db.query(IssueLink)
            .filter(
                IssueLink.workspace_id == workspace_id,
                (IssueLink.source_issue_id.in_(issue_ids) | IssueLink.target_issue_id.in_(issue_ids)),
            )
            .all()
        )

    critical_path = _calculate_critical_path(all_issues, links)
    critical_path_set = set(critical_path)

    statuses = {
        s.id: s
        for s in db.query(WorkflowStatus)
        .filter(WorkflowStatus.workspace_id == workspace_id, WorkflowStatus.space_id == space_id)
        .all()
    }
    lists = {
        l.id: l
        for l in db.query(ProjectList)
        .filter(ProjectList.workspace_id == workspace_id, ProjectList.space_id == space_id)
        .all()
    }
    assignee_ids = [i.assignee_id for i in all_issues if i.assignee_id]
    users = {u.id: u for u in db.query(User).filter(User.id.in_(assignee_ids)).all()} if assignee_ids else {}
    epics = {
        i.id: i
        for i in db.query(Issue)
        .filter(Issue.workspace_id == workspace_id, Issue.space_id == space_id, Issue.type == "epic")
        .all()
    }

    issue_map = {i.id: i for i in all_issues}

    # Filter issues by date range if provided
    filtered_issues = []
    for issue in all_issues:
        if start_date and issue.due_date and issue.due_date < start_date:
            continue
        if end_date and issue.start_date and issue.start_date > end_date:
            continue
        filtered_issues.append(issue)

    items = [
        _to_timeline_item(i, space, statuses, lists, users, epics, critical_path_set)
        for i in filtered_issues
    ]

    dependencies = []
    for link in links:
        src = issue_map.get(link.source_issue_id)
        tgt = issue_map.get(link.target_issue_id)
        is_conflict = False
        if link.kind == "blocks" and src and tgt and src.due_date and tgt.start_date:
            is_conflict = src.due_date > tgt.start_date
        dependencies.append(
            TimelineDependencyItem(
                id=link.id,
                source_issue_id=link.source_issue_id,
                target_issue_id=link.target_issue_id,
                kind=link.kind,
                is_conflict=is_conflict,
            )
        )

    # Grouping
    groups: list[TimelineGroup] = []
    if group_by == "epic":
        epic_groups: dict[str, list[str]] = defaultdict(list)
        for it in items:
            key = it.epic_title or "No Epic"
            epic_groups[key].append(it.id)
        groups = [TimelineGroup(id=k, title=k, issue_ids=v) for k, v in epic_groups.items()]
    elif group_by == "list":
        list_groups: dict[str, list[str]] = defaultdict(list)
        for it in items:
            list_groups[it.list_name].append(it.id)
        groups = [TimelineGroup(id=k, title=k, issue_ids=v) for k, v in list_groups.items()]
    elif group_by == "assignee":
        assignee_groups: dict[str, list[str]] = defaultdict(list)
        for it in items:
            key = it.assignee_name or "Unassigned"
            assignee_groups[key].append(it.id)
        groups = [TimelineGroup(id=k, title=k, issue_ids=v) for k, v in assignee_groups.items()]

    return TimelineResponse(
        space_id=space_id,
        zoom=zoom,
        start_date=start_date,
        end_date=end_date,
        items=items,
        dependencies=dependencies,
        critical_path_issue_ids=critical_path,
        groups=groups,
    )


def reschedule_issue(
    db: Session,
    workspace_id: str,
    issue_id: str,
    payload: RescheduleRequest,
    actor_id: str,
) -> RescheduleResponse:
    if payload.due_date < payload.start_date:
        raise ValueError("due_date must be greater than or equal to start_date")

    issue = (
        db.query(Issue)
        .filter(Issue.id == issue_id, Issue.workspace_id == workspace_id, Issue.deleted == 0)
        .first()
    )
    if not issue:
        raise ValueError("Issue not found")

    space = (
        db.query(Space)
        .filter(Space.id == issue.space_id, Space.workspace_id == workspace_id)
        .first()
    )
    if not space:
        raise ValueError("Space not found")

    block = write_guard.space_id_block(db, workspace_id, issue.space_id)
    if block:
        raise ValueError(block.message)

    old_start = issue.start_date
    old_due = issue.due_date

    delta_days = 0
    if old_due:
        delta_days = (payload.due_date - old_due).days
    elif old_start:
        delta_days = (payload.start_date - old_start).days

    issue.start_date = payload.start_date
    issue.due_date = payload.due_date

    # Record IssueEvent
    event = IssueEvent(
        workspace_id=workspace_id,
        issue_id=issue.id,
        actor_id=actor_id,
        actor_kind="user",
        event="updated",
        from_value=f"{old_start}..{old_due}",
        to_value=f"{payload.start_date}..{payload.due_date}",
    )
    db.add(event)

    cascaded_entities: list[Issue] = []

    if payload.cascade and delta_days > 0:
        queue = [(issue, payload.due_date)]
        visited = {issue.id}

        while queue:
            curr, curr_due = queue.pop(0)
            downstream_links = (
                db.query(IssueLink)
                .filter(
                    IssueLink.workspace_id == workspace_id,
                    IssueLink.source_issue_id == curr.id,
                    IssueLink.kind == "blocks",
                )
                .all()
            )

            for link in downstream_links:
                target_id = link.target_issue_id
                if target_id in visited:
                    continue
                visited.add(target_id)

                dep = (
                    db.query(Issue)
                    .filter(Issue.id == target_id, Issue.workspace_id == workspace_id, Issue.deleted == 0)
                    .first()
                )
                if not dep:
                    continue

                if payload.cascade_mode == "push_all_dependents":
                    if dep.start_date and dep.due_date:
                        dep_dur = (dep.due_date - dep.start_date).days
                        dep.start_date = dep.start_date + timedelta(days=delta_days)
                        dep.due_date = dep.start_date + timedelta(days=dep_dur)
                    else:
                        dep.start_date = curr_due + timedelta(days=1)
                        dep.due_date = dep.start_date + timedelta(days=1)
                    cascaded_entities.append(dep)
                    queue.append((dep, dep.due_date))
                elif payload.cascade_mode == "push_conflicts_only":
                    if dep.start_date is None or dep.start_date <= curr_due:
                        dep_dur = (dep.due_date - dep.start_date).days if (dep.due_date and dep.start_date) else 1
                        dep.start_date = curr_due + timedelta(days=1)
                        dep.due_date = dep.start_date + timedelta(days=dep_dur)
                        cascaded_entities.append(dep)
                        queue.append((dep, dep.due_date))

    db.commit()
    db.refresh(issue)

    # Load lookup maps to construct response items
    space_ids = {issue.space_id} | {c.space_id for c in cascaded_entities}
    all_updated = [issue] + cascaded_entities
    statuses = {
        s.id: s
        for s in db.query(WorkflowStatus)
        .filter(WorkflowStatus.workspace_id == workspace_id, WorkflowStatus.space_id.in_(space_ids))
        .all()
    }
    lists = {
        l.id: l
        for l in db.query(ProjectList)
        .filter(ProjectList.workspace_id == workspace_id, ProjectList.space_id.in_(space_ids))
        .all()
    }
    user_ids = [i.assignee_id for i in all_updated if i.assignee_id]
    users = {u.id: u for u in db.query(User).filter(User.id.in_(user_ids)).all()} if user_ids else {}
    epics = {
        i.id: i
        for i in db.query(Issue)
        .filter(Issue.workspace_id == workspace_id, Issue.type == "epic")
        .all()
    }

    item = _to_timeline_item(issue, space, statuses, lists, users, epics, set())
    cascaded_items = [
        _to_timeline_item(c, space, statuses, lists, users, epics, set())
        for c in cascaded_entities
    ]

    return RescheduleResponse(
        issue=item,
        cascaded_issues=cascaded_items,
        delta_days=delta_days,
    )
