"""Issues, the board, the backlog and comments — the transaction boundary for all of them.

Every lookup here goes through `services/issue.py` with the workspace id from the resolved
`WorkspaceAccess`, so an issue id from another tenant is a 404 like any other unknown id.

**Every write asks `services/write_guard.py` first** (docs/design/09-restructuring.md §3.4):
an archived list, folder or space, or a space frozen for a migration wave, answers 423
before anything else is validated — telling someone their parent is the wrong type when the
issue cannot be changed at all would send them to fix the wrong thing. Reads are never
refused. Requesting an AI summary (controllers/ai_controller.py) writes nothing to the issue
and is deliberately not guarded.

The order of refusals in every write is the order a person can act on them: not found
(404), locked (423), stale (409), then the rules (422) — the hierarchy, the epic's owner and
criteria, the blocked reason, the assignee — and last the WIP limit, which is the only one
whose answer depends on everybody else's work.
"""
from datetime import UTC, datetime

from sqlalchemy import asc, desc
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from dependencies.workspace import WorkspaceAccess
from models.hierarchy import WorkflowStatus
from models.issue import Issue, IssueComment
from schemas.issues import (
    BacklogRank,
    CommentCreate,
    CommentUpdate,
    IssueCreate,
    IssueFilters,
    IssueMove,
    IssueUpdate,
)
from schemas.response import SuccessResponse, error_json
from services import governance, key_resolution, transitions, write_guard
from services import issue as svc
from services import sprint as sprint_svc
from services import workspace as ws
from utils.workflow_rules import OPEN_CATEGORIES

#: What the list view may sort by. A column name taken from a query string is an injection
#: vector unless it is looked up in a fixed table like this one.
_SORTABLE = {
    "number": Issue.number,
    "updated_at": Issue.updated_at,
    "due_date": Issue.due_date,
    "priority": Issue.priority,
}

#: A page is capped so one request cannot ask a tenant's whole history of the database.
MAX_PAGE_SIZE = 200

#: The ordered backlog returns at most this many cards (09-restructuring.md §5.2). A person
#: plans from the top of a backlog; five hundred items is far past what any planning session
#: reads. A card is about 670 bytes of JSON (measured 2026-10-06: a 45-character title, an
#: assignee, points, no blocker), so the backlog part of the answer stays near 340 KB, and
#: under 1 MB even when every title is long. Past it, `truncated` says so and the board's
#: filters narrow the list.
MAX_BACKLOG_CARDS = 500

#: The delivery items a backlog orders: the plan's Scrum policy commits Story, Task and Bug
#: items to sprints. Epics are outcomes, listed beside the backlog for filtering; subtasks
#: are execution detail of an item that is already in it.
BACKLOG_TYPES: tuple[str, ...] = ("story", "task", "bug")

_NOT_FOUND = "Issue not found"


def _points_text(points: int | None) -> str | None:
    """Story points as an `issue_events` value: the number, or NULL for "not estimated"."""
    return None if points is None else str(points)


def _with_wip_warning(message: str, status: WorkflowStatus,
                      breach: tuple[int, int] | None) -> str:
    """A success message, plus the WIP limit it went over under the `warn` policy."""
    if breach is None:
        return message
    return f"{message}. {svc.wip_warning(status, *breach)}"


# ---- create -------------------------------------------------------------------------------


def create_issue(access: WorkspaceAccess, body: IssueCreate, db: Session):
    task_list = ws.get_list(db, access.workspace_id, body.list_id)
    if task_list is None:
        return error_json(404, "List not found")
    block = write_guard.list_block(db, access.workspace_id, task_list)
    if block is not None:
        return block.response()
    space = ws.get_space(db, access.workspace_id, task_list.space_id)
    if space is None:
        return error_json(404, "List not found")

    # The stage a new issue starts in (09-restructuring.md §4.3): the space's initial stage
    # unless one is named; a named one must be live and of this space, and a restricted
    # workflow accepts only its initial stage, the way Jira's does.
    initial = transitions.initial_status(db, access.workspace_id, space)
    if body.status_id is not None:
        status = svc.get_status(db, access.workspace_id, body.status_id)
        if status is None or status.space_id != space.id:
            return error_json(422, "That status is not part of this list's workflow.")
        problem = transitions.create_status_problem(space, status, initial)
        if problem:
            return error_json(422, problem)
    elif initial is None:
        return error_json(409, "This space has no workflow statuses to put the issue in.")
    else:
        status = initial

    parent = None
    if body.parent_id is not None:
        parent = svc.get_issue(db, access.workspace_id, body.parent_id)
        if parent is None:
            return error_json(404, "Parent issue not found")
    problem = svc.parent_problem(body.type, parent, space.id)
    if problem:
        return error_json(422, problem)

    if body.assignee_id is not None and not ws.is_member(db, access.workspace_id,
                                                         body.assignee_id):
        return error_json(422, "Issues can only be assigned to members of this workspace.")
    if body.type == "epic" and governance.load_settings(
            db, access.workspace_id).require_epic_owner_and_criteria:
        problem = svc.epic_problem(body.assignee_id, body.completion_criteria)
        if problem:
            return error_json(422, problem)

    # Creating straight into a stage is entering it, so the WIP limit applies as it does to
    # a move: otherwise the board's quick-add in a `block` space would be the way around it.
    breach = svc.stage_wip_breach(db, access.workspace_id, space.id, status, body.type)
    if breach is not None and space.wip_policy == "block":
        return error_json(409, svc.wip_refusal(status, breach[1]))

    try:
        # Allocating the number writes the space's row, which a publish holds while it
        # removes stages; with that lock held, the stage chosen above is read again as last
        # committed (services/issue.py, "writes racing a workflow publish").
        number = svc.allocate_number(db, space.id)
        if svc.locked_live_status(db, access.workspace_id, status.id) is None:
            stage_name = status.name
            db.rollback()
            return error_json(409, f"{stage_name} was removed from this workflow a moment "
                                   "ago. Reload the board and create the issue again.")
        issue = Issue(
            workspace_id=access.workspace_id,
            space_id=space.id,
            list_id=task_list.id,
            number=number,
            type=body.type,
            title=body.title.strip(),
            description=body.description,
            status_id=status.id,
            parent_id=parent.id if parent else None,
            assignee_id=body.assignee_id,
            reporter_id=access.user_id,
            priority=body.priority,
            story_points=body.story_points,
            time_estimate_seconds=body.time_estimate_seconds,
            rank=svc.bottom_rank(db, access.workspace_id, space.id, status.id),
            backlog_rank=svc.bottom_backlog_rank(db, access.workspace_id, space.id),
            status_entered_at=datetime.now(UTC),
            due_date=body.due_date,
            completion_criteria=body.completion_criteria,
            version=1,
        )
        db.add(issue)
        db.flush()
        svc.record_event(db, issue, "created", access.user_id, to_value=status.id)
        db.commit()
    except IntegrityError:
        db.rollback()
        return error_json(409, "Another issue was created at the same moment. Try again.")
    return SuccessResponse(status_code=201,
                           message=_with_wip_warning("Issue created", status, breach),
                           data=svc.detail(db, issue))


# ---- read ---------------------------------------------------------------------------------


def list_issues(access: WorkspaceAccess, db: Session, *, list_id: str | None,
                space_id: str | None, status_id: str | None, sprint_id: str | None,
                filters: IssueFilters, page: int, limit: int, sort_by: str, order: str):
    if sort_by not in _SORTABLE:
        return error_json(400, f"sort_by must be one of: {', '.join(sorted(_SORTABLE))}")
    limit = max(1, min(limit, MAX_PAGE_SIZE))

    query = db.query(Issue).filter(Issue.workspace_id == access.workspace_id,
                                   Issue.deleted == 0)
    if list_id:
        query = query.filter(Issue.list_id == list_id)
    if space_id:
        query = query.filter(Issue.space_id == space_id)
    if status_id:
        query = query.filter(Issue.status_id == status_id)
    if sprint_id == "none":
        query = query.filter(Issue.sprint_id.is_(None))
    elif sprint_id:
        query = query.filter(Issue.sprint_id == sprint_id)
    query = svc.apply_filters(db, query, access.workspace_id, access.user_id, filters)

    total = query.count()
    column = _SORTABLE[sort_by]
    ordering = asc(column) if order.lower() == "asc" else desc(column)
    rows = query.order_by(ordering, Issue.id).offset((page - 1) * limit).limit(limit).all()
    return SuccessResponse(status_code=200, message="Issues fetched",
                           data={"records": svc.cards(db, rows), "total": total})


def get_issue(access: WorkspaceAccess, issue_id: str, db: Session):
    issue = svc.get_issue(db, access.workspace_id, issue_id)
    if issue is None:
        return error_json(404, _NOT_FOUND)
    return SuccessResponse(status_code=200, message="Issue fetched", data=svc.detail(db, issue))


def resolve_issue_key(access: WorkspaceAccess, key: str, db: Session):
    """A deep link (09-restructuring.md §7.3): the issue a key names now, through the one
    resolver, so `PAY-7` quoted before the issue moved — or before its space changed key —
    still opens it, and the SPA can redirect to `current_key`."""
    parsed = svc.parse_issue_key(key)
    found = (key_resolution.resolve_key(db, access.workspace_id, *parsed)
             if parsed is not None else None)
    if found is None:
        return error_json(404, "No issue in this workspace has that key.")
    issue, resolved_via = found
    card = svc.cards(db, [issue])[0]
    return SuccessResponse(status_code=200, message="Issue found", data={
        "issue": card, "list_id": issue.list_id, "space_id": issue.space_id,
        "current_key": card["key"], "resolved_via": resolved_via,
    })


# ---- update -------------------------------------------------------------------------------


def _hierarchy_refusal(db: Session, access: WorkspaceAccess, issue: Issue, new_type: str,
                       new_parent_id: str | None, parent_changed: bool):
    """The 404/422 for a change of parent or type, or None (09-restructuring.md §2.2).

    Only called when one of the two changes: a relationship left alone is never re-checked,
    however it was made, so a legacy Story > Task tree stays editable until someone decides
    what it means. A changed one follows the standard — the issue against its (new) parent
    and, when the type changes, every child against the new type.
    """
    parent = None
    if new_parent_id is not None:
        parent = svc.get_issue(db, access.workspace_id, new_parent_id)
        if parent_changed:
            if parent is None:
                return error_json(404, "Parent issue not found")
            if svc.is_ancestor(db, access.workspace_id, parent.id, issue.id):
                return error_json(422, "An issue cannot sit under itself or its own children.")
    problem = svc.parent_problem(new_type, parent, issue.space_id)
    if problem:
        return error_json(422, problem)
    if new_type != issue.type:
        problem = svc.children_type_problem(db, access.workspace_id, issue, new_type)
        if problem:
            return error_json(422, problem)
    return None


def _epic_problem(db: Session, access: WorkspaceAccess, issue: Issue, body: IssueUpdate,
                  changed: set[str], new_type: str) -> str | None:
    """The epic rule for an update (09-restructuring.md §2.3), or None.

    Re-typing into an epic needs an owner and criteria in the resulting state; an epic may
    not lose either; any other edit of an epic that never had them — written before the rule
    — is allowed, so legacy epics stay editable and the inventory can count them.
    """
    if new_type != "epic":
        return None
    if issue.type == "epic" and not changed & {"assignee_id", "completion_criteria"}:
        return None
    if not governance.load_settings(db, access.workspace_id).require_epic_owner_and_criteria:
        return None
    assignee_id = body.assignee_id if "assignee_id" in changed else issue.assignee_id
    criteria = (body.completion_criteria if "completion_criteria" in changed
                else issue.completion_criteria)
    if issue.type != "epic":
        return svc.epic_problem(assignee_id, criteria)
    if assignee_id is None and issue.assignee_id is not None:
        return ("An epic must keep an owner: assign it to someone else rather than "
                "unassigning it.")
    if svc.is_blank(criteria) and not svc.is_blank(issue.completion_criteria):
        return "An epic's completion criteria cannot be removed: edit them instead."
    return None


def _blocked_state(issue: Issue, body: IssueUpdate, changed: set[str]) -> tuple[bool, str]:
    """Whether the issue is blocked after this update, and its (stripped) reason."""
    blocked = bool(body.blocked) if "blocked" in changed else bool(issue.blocked)
    reason = body.blocked_reason if "blocked_reason" in changed else issue.blocked_reason
    return blocked, (reason or "").strip()


def _blocked_problem(issue: Issue, body: IssueUpdate, changed: set[str]) -> str | None:
    """Blocking needs a reason, in the body or already stored (§5.3): a flag that says only
    "blocked" is the one the daily stand-up cannot do anything with."""
    if not changed & {"blocked", "blocked_reason"}:
        return None
    blocked, reason = _blocked_state(issue, body, changed)
    if blocked and not reason:
        return ("A blocked issue needs a reason: say what blocks it, who owns the blocker, "
                "and the next action.")
    if not blocked and "blocked_reason" in changed and reason:
        return "Only a blocked issue has a blocked reason. Send blocked: true with it."
    return None


def _apply_blocked(db: Session, issue: Issue, body: IssueUpdate, changed: set[str],
                   user_id: str) -> None:
    """Write the blocked flag and its events. Blocking stamps who and when; a new reason on an
    issue that is already blocked is recorded again but keeps the original time, because the
    age of a blocker is what the plan's daily inspection looks at; unblocking clears all of
    it."""
    if not changed & {"blocked", "blocked_reason"}:
        return
    blocked, reason = _blocked_state(issue, body, changed)
    if blocked and not issue.blocked:
        issue.blocked = True
        issue.blocked_reason = reason
        issue.blocked_at = datetime.now(UTC)
        issue.blocked_by_id = user_id
        svc.record_event(db, issue, "blocked", user_id, to_value=reason)
    elif blocked and reason != (issue.blocked_reason or ""):
        issue.blocked_reason = reason
        svc.record_event(db, issue, "blocked", user_id, to_value=reason)
    elif not blocked and issue.blocked:
        svc.record_event(db, issue, "unblocked", user_id, from_value=issue.blocked_reason)
        issue.blocked = False
        issue.blocked_reason = None
        issue.blocked_at = None
        issue.blocked_by_id = None


def update_issue(access: WorkspaceAccess, issue_id: str, body: IssueUpdate, db: Session):
    issue = svc.get_issue(db, access.workspace_id, issue_id)
    if issue is None:
        return error_json(404, _NOT_FOUND)
    block = write_guard.issue_block(db, issue)
    if block is not None:
        return block.response()
    if issue.version != body.version:
        return error_json(409, "Someone else changed this issue since you opened it. "
                               "Reload it and apply your change again.",
                          data={"current_version": issue.version})

    changed = body.model_fields_set - {"version"}
    for field in ("title", "type", "priority", "blocked"):
        if field in changed and getattr(body, field) is None:
            return error_json(422, f"{field} cannot be empty.")

    # A field sent with the value it already has changes no relationship: an edit form that
    # sends every field back must not re-check a legacy parent nobody touched.
    new_type = str(body.type) if "type" in changed else str(issue.type)
    new_parent_id = body.parent_id if "parent_id" in changed else issue.parent_id
    type_changed = new_type != issue.type
    parent_changed = new_parent_id != issue.parent_id
    if type_changed or parent_changed:
        refusal = _hierarchy_refusal(db, access, issue, new_type, new_parent_id,
                                     parent_changed)
        if refusal is not None:
            return refusal

    problem = (_epic_problem(db, access, issue, body, changed, new_type)
               or _blocked_problem(issue, body, changed))
    if problem:
        return error_json(422, problem)
    if "assignee_id" in changed and body.assignee_id is not None and not ws.is_member(
            db, access.workspace_id, body.assignee_id):
        return error_json(422, "Issues can only be assigned to members of this workspace.")

    # Structural events beside `updated` (§2.4): a reorganisation is recorded, not inferred
    # later from a diff nobody kept.
    if parent_changed:
        svc.record_event(db, issue, "parent_changed", access.user_id,
                         from_value=issue.parent_id, to_value=new_parent_id)
    if type_changed:
        svc.record_event(db, issue, "type_changed", access.user_id,
                         from_value=issue.type, to_value=new_type)
    if "assignee_id" in changed and body.assignee_id != issue.assignee_id:
        svc.record_event(db, issue, "assigned", access.user_id,
                         from_value=issue.assignee_id, to_value=body.assignee_id)
    if "story_points" in changed and body.story_points != issue.story_points:
        # Its own event, with the values, so a sprint report can show `3 → 5` and a
        # burndown can replay the points of any past day (docs/design/08-reports.md §5.5).
        svc.record_event(db, issue, "points_changed", access.user_id,
                         from_value=_points_text(issue.story_points),
                         to_value=_points_text(body.story_points))
    if ("time_estimate_seconds" in changed
            and body.time_estimate_seconds != issue.time_estimate_seconds):
        # The same, for the burndown counted in hours of estimate.
        svc.record_event(db, issue, "estimate_changed", access.user_id,
                         from_value=_points_text(issue.time_estimate_seconds),
                         to_value=_points_text(body.time_estimate_seconds))
    _apply_blocked(db, issue, body, changed, access.user_id)
    for field in changed - {"blocked", "blocked_reason"}:
        value = getattr(body, field)
        setattr(issue, field, value.strip() if field == "title" else value)
    if changed:
        issue.version = issue.version + 1
        svc.record_event(db, issue, "updated", access.user_id,
                         to_value=",".join(sorted(changed)))
    db.commit()
    return SuccessResponse(status_code=200, message="Issue updated", data=svc.detail(db, issue))


# ---- move, rank, delete -------------------------------------------------------------------


def move_issue(access: WorkspaceAccess, issue_id: str, body: IssueMove, db: Session):
    issue = svc.get_issue(db, access.workspace_id, issue_id)
    if issue is None:
        return error_json(404, _NOT_FOUND)
    # The space, then the issue, then the destination as last committed — so a publish
    # removing that stage either moves this issue with the others or is seen to have removed
    # it (services/issue.py, "writes racing a workflow publish").
    svc.lock_space_shared(db, access.workspace_id, str(issue.space_id))
    issue = svc.lock_issue(db, access.workspace_id, issue_id)
    if issue is None:
        return error_json(404, _NOT_FOUND)
    block = write_guard.issue_block(db, issue)
    if block is not None:
        return block.response()
    space = ws.get_space(db, access.workspace_id, issue.space_id)
    if space is None:
        return error_json(404, _NOT_FOUND)
    status = svc.locked_live_status(db, access.workspace_id, body.status_id)
    if status is None or status.space_id != issue.space_id:
        return error_json(422, "That column is not part of this issue's workflow.")

    # The workflow's flow (§4.3) — the same answer the webhook gets, naming where the issue
    # can go — and then the WIP limit of where it is going (§4.7).
    current = svc.any_status(db, access.workspace_id, issue.status_id)
    if current is not None:
        problem = transitions.transition_problem(db, access.workspace_id, space, current,
                                                 status)
        if problem:
            return error_json(422, problem)
    breach = svc.wip_breach(db, issue, status)
    if breach is not None and space.wip_policy == "block":
        return error_json(409, svc.wip_refusal(status, breach[1]))

    neighbours = {}
    for name, neighbour_id in (("after", body.after_id), ("before", body.before_id)):
        if neighbour_id is None or neighbour_id == issue.id:
            neighbours[name] = None
            continue
        neighbour = svc.get_issue(db, access.workspace_id, neighbour_id)
        # A neighbour that has since left the column (someone else moved it) is ignored
        # rather than refused: the drop still lands, next to whichever anchor is still true.
        valid = (neighbour is not None and neighbour.status_id == status.id
                 and neighbour.space_id == issue.space_id)
        neighbours[name] = neighbour if valid else None

    rank = svc.rank_for_drop(db, issue, status.id, neighbours["after"], neighbours["before"])
    svc.set_status(db, issue, status, rank, access.user_id)
    db.commit()
    return SuccessResponse(status_code=200,
                           message=_with_wip_warning("Issue moved", status, breach),
                           data=svc.cards(db, [issue])[0])


def rank_in_backlog(access: WorkspaceAccess, issue_id: str, body: BacklogRank, db: Session):
    """Reorder the space's backlog (09-restructuring.md §5.2). Planning, like sprint
    membership: it writes `backlog_rank` and nothing else, bumps no version — someone with
    the issue open must not get a 409 because a lead reordered the backlog — and records no
    event, as a reorder inside a board column does not."""
    issue = svc.get_issue(db, access.workspace_id, issue_id)
    if issue is None:
        return error_json(404, _NOT_FOUND)
    block = write_guard.issue_block(db, issue)
    if block is not None:
        return block.response()

    neighbours = {}
    for name, neighbour_id in (("after", body.after_id), ("before", body.before_id)):
        if neighbour_id is None or neighbour_id == issue.id:
            neighbours[name] = None
            continue
        neighbour = svc.get_issue(db, access.workspace_id, neighbour_id)
        # As on the board: a neighbour that has gone, or that is in another space, is
        # ignored rather than refused, and the drop lands by whichever anchor is still true.
        valid = neighbour is not None and neighbour.space_id == issue.space_id
        neighbours[name] = neighbour if valid else None

    rank = svc.backlog_rank_for_drop(db, issue, neighbours["after"], neighbours["before"])
    svc.set_backlog_rank(db, issue, rank)
    db.commit()
    return SuccessResponse(status_code=200, message="Backlog order changed",
                           data=svc.cards(db, [issue])[0])


def delete_issue(access: WorkspaceAccess, issue_id: str, db: Session):
    issue = svc.get_issue(db, access.workspace_id, issue_id)
    if issue is None:
        return error_json(404, _NOT_FOUND)
    block = write_guard.issue_block(db, issue)
    if block is not None:
        return block.response()
    has_children = db.query(Issue.id).filter(
        Issue.workspace_id == access.workspace_id, Issue.parent_id == issue.id,
        Issue.deleted == 0).first()
    if has_children:
        return error_json(409, "This issue has child issues. Move or delete them first.")
    issue.deleted = 1
    issue.deleted_at = datetime.now(UTC)
    svc.record_event(db, issue, "deleted", access.user_id)
    db.commit()
    return SuccessResponse(status_code=200, message="Issue deleted", data={"id": issue.id})


# ---- views --------------------------------------------------------------------------------


def get_board(access: WorkspaceAccess, db: Session, *, list_id: str | None,
              space_id: str | None, sprint_id: str | None, filters: IssueFilters):
    """Every column of the space, including empty ones, each with its cards in rank order.

    Three scopes (09-restructuring.md §5.1): one list (the delivery Kanban), every list of a
    space (the space overview), or one sprint of any state (the sprint board) — columns are
    always the space's live stages.

    One query for the statuses, one for the issues, four for the card projection (the fourth
    is the time logged, since 2026-10-05; docs/design/01-architecture.md budgets five for the
    board itself, and this stays one over it whatever the number of cards). Since the
    restructuring the board also carries its workflow and the team's WIP: the scope lookup,
    the space, one grouped WIP count, the transitions and the initial stage. Measured on
    2026-10-06: eleven queries for a list or a sprint board, ten for a space, plus the
    session and membership checks every route makes — and the same with two cards or two
    hundred. Only the filters
    that need them add more: the key lookup of `q` (at most three) and one per level of the
    `epic_id` subtree. Grouping happens here rather than in SQL so an empty column still
    appears — a board with a missing column is a board you cannot drop a card into.
    """
    if sum(1 for scope_value in (list_id, space_id, sprint_id) if scope_value) != 1:
        return error_json(400, "Give exactly one of list_id, space_id or sprint_id.")
    query = db.query(Issue).filter(Issue.workspace_id == access.workspace_id,
                                   Issue.deleted == 0)
    if list_id:
        task_list = ws.get_list(db, access.workspace_id, list_id)
        if task_list is None:
            return error_json(404, "List not found")
        scope, scope_id, board_space_id = "list", str(task_list.id), str(task_list.space_id)
        query = query.filter(Issue.space_id == board_space_id, Issue.list_id == scope_id)
    elif space_id:
        scope, scope_id, board_space_id = "space", space_id, space_id
        query = query.filter(Issue.space_id == board_space_id)
    else:
        sprint = sprint_svc.get_sprint(db, access.workspace_id, sprint_id or "")
        if sprint is None:
            return error_json(404, "Sprint not found")
        scope, scope_id, board_space_id = "sprint", str(sprint.id), str(sprint.space_id)
        query = query.filter(Issue.space_id == board_space_id, Issue.sprint_id == scope_id)
    space = ws.get_space(db, access.workspace_id, board_space_id)
    if space is None:
        return error_json(404, "Space not found")

    statuses = transitions.live_statuses(db, access.workspace_id, space.id)
    issues = (svc.apply_filters(db, query, access.workspace_id, access.user_id, filters)
              .order_by(Issue.rank, Issue.id).all())
    projected = svc.cards(db, issues)
    wip = svc.wip_counts(db, access.workspace_id, space.id)
    initial = transitions.initial_status(db, access.workspace_id, space)

    by_status: dict[str, list[dict]] = {status.id: [] for status in statuses}
    for card in projected:
        # A card whose stage is not a live one of this space has no column to be drawn in.
        if card["status_id"] in by_status:
            by_status[card["status_id"]].append(card)
    position = {status.id: index for index, status in enumerate(statuses)}
    pairs = sorted(
        ((source, target) for source, target in
         transitions.transition_pairs(db, access.workspace_id, space.id)
         if target in position and (source is None or source in position)),
        key=lambda pair: (-1 if pair[0] is None else position[pair[0]], position[pair[1]]))
    return SuccessResponse(
        status_code=200,
        message="Board fetched",
        data={
            "scope": scope,
            "scope_id": scope_id,
            "space_id": space.id,
            "transition_mode": space.transition_mode,
            "initial_status_id": initial.id if initial is not None else None,
            "transitions": [{"from_status_id": source, "to_status_id": target}
                            for source, target in pairs],
            "wip_policy": space.wip_policy,
            "filtered": filters.active(),
            "total": sum(len(cards) for cards in by_status.values()),
            "columns": [{"status": ws.status_payload(status),
                         "wip": svc.column_wip(status, wip),
                         "cards": by_status[status.id]}
                        for status in statuses],
        },
    )


def get_backlog(access: WorkspaceAccess, space_id: str, filters: IssueFilters,
                include_closed: bool, db: Session):
    """The space's planning view (09-restructuring.md §5.2): its active and planned sprints
    with their issues, the ordered backlog, and its open epics.

    The board's filters narrow the sprints' cards and the backlog alike — a quick filter that
    hid an item from the backlog but not from the sprint it is in would make planning by it
    misleading — but never the epics, which are the list a person picks `epic_id` from. One
    card projection for all three parts, so the cost does not grow with the number of
    sprints.
    """
    space = ws.get_space(db, access.workspace_id, space_id)
    if space is None:
        return error_json(404, "Space not found")
    sprints = sprint_svc.space_sprints(db, access.workspace_id, space.id,
                                       ["active", "planned"])
    # Removed stages too: an issue left in one still has a category to be judged by.
    categories = dict(db.query(WorkflowStatus.id, WorkflowStatus.category).filter(
        WorkflowStatus.workspace_id == access.workspace_id,
        WorkflowStatus.space_id == space.id).all())
    open_ids = sorted(status_id for status_id, category in categories.items()
                      if category in OPEN_CATEGORIES)
    order = (Issue.backlog_rank, Issue.id)
    live = db.query(Issue).filter(Issue.workspace_id == access.workspace_id,
                                  Issue.space_id == space.id, Issue.deleted == 0)
    filtered = svc.apply_filters(db, live, access.workspace_id, access.user_id, filters)

    in_sprints = (filtered.filter(Issue.sprint_id.in_([sprint.id for sprint in sprints]))
                  .order_by(*order).all() if sprints else [])
    backlog = filtered.filter(Issue.sprint_id.is_(None), Issue.type.in_(BACKLOG_TYPES))
    if not include_closed:
        backlog = backlog.filter(Issue.status_id.in_(open_ids))
    total = backlog.count()
    rows = backlog.order_by(*order).limit(MAX_BACKLOG_CARDS).all()
    epics = (live.filter(Issue.type == "epic", Issue.status_id.in_(open_ids))
             .order_by(*order).all())

    projected = svc.cards(db, [*in_sprints, *rows, *epics])
    sprint_cards: dict[str, list[dict]] = {sprint.id: [] for sprint in sprints}
    for issue, card in zip(in_sprints, projected, strict=False):
        sprint_cards[issue.sprint_id].append(card)
    backlog_cards = projected[len(in_sprints):len(in_sprints) + len(rows)]
    epic_cards = projected[len(in_sprints) + len(rows):]
    return SuccessResponse(status_code=200, message="Backlog fetched", data={
        "space_id": space.id,
        "sprints": [{"sprint": summary, "cards": sprint_cards[sprint.id]}
                    for sprint, summary in zip(sprints, sprint_svc.payloads(db, sprints),
                                               strict=True)],
        "backlog": {"cards": backlog_cards, "total": total, "truncated": total > len(rows)},
        "epics": epic_cards,
    })


# ---- comments -----------------------------------------------------------------------------


def list_comments(access: WorkspaceAccess, issue_id: str, db: Session):
    if svc.get_issue(db, access.workspace_id, issue_id) is None:
        return error_json(404, _NOT_FOUND)
    rows = svc.comments(db, access.workspace_id, issue_id)
    return SuccessResponse(status_code=200, message="Comments fetched",
                           data=svc.comment_payloads(db, rows))


def create_comment(access: WorkspaceAccess, issue_id: str, body: CommentCreate, db: Session):
    issue = svc.get_issue(db, access.workspace_id, issue_id)
    if issue is None:
        return error_json(404, _NOT_FOUND)
    block = write_guard.issue_block(db, issue)
    if block is not None:
        return block.response()
    comment = IssueComment(workspace_id=access.workspace_id, issue_id=issue.id,
                           author_id=access.user_id, body=body.body)
    db.add(comment)
    db.flush()
    if body.attachment_ids:
        from models.storage import IssueAttachment
        db.query(IssueAttachment).filter(
            IssueAttachment.id.in_(body.attachment_ids),
            IssueAttachment.issue_id == issue.id,
            IssueAttachment.workspace_id == access.workspace_id,
            IssueAttachment.deleted == 0,
        ).update({"comment_id": comment.id}, synchronize_session=False)
    svc.record_event(db, issue, "commented", access.user_id, to_value=comment.id)
    db.commit()
    return SuccessResponse(status_code=201, message="Comment added",
                           data=svc.comment_payloads(db, [comment])[0])


def update_comment(access: WorkspaceAccess, issue_id: str, comment_id: str,
                   body: CommentUpdate, db: Session):
    issue = svc.get_issue(db, access.workspace_id, issue_id)
    if issue is None:
        return error_json(404, _NOT_FOUND)
    block = write_guard.issue_block(db, issue)
    if block is not None:
        return block.response()
    comment = (
        db.query(IssueComment)
        .filter(
            IssueComment.id == comment_id,
            IssueComment.issue_id == issue.id,
            IssueComment.workspace_id == access.workspace_id,
            IssueComment.deleted == 0,
        )
        .first()
    )
    if comment is None:
        return error_json(404, "Comment not found")
    if comment.author_id != access.user_id and not access.at_least("admin"):
        return error_json(403, "You can only edit your own comments")
    comment.body = body.body
    comment.updated_at = datetime.now(UTC)
    if body.attachment_ids is not None:
        from models.storage import IssueAttachment
        db.query(IssueAttachment).filter(
            IssueAttachment.id.in_(body.attachment_ids),
            IssueAttachment.issue_id == issue.id,
            IssueAttachment.workspace_id == access.workspace_id,
            IssueAttachment.deleted == 0,
        ).update({"comment_id": comment.id}, synchronize_session=False)
    db.commit()
    return SuccessResponse(status_code=200, message="Comment updated",
                           data=svc.comment_payloads(db, [comment])[0])


def delete_comment(access: WorkspaceAccess, issue_id: str, comment_id: str, db: Session):
    issue = svc.get_issue(db, access.workspace_id, issue_id)
    if issue is None:
        return error_json(404, _NOT_FOUND)
    block = write_guard.issue_block(db, issue)
    if block is not None:
        return block.response()
    comment = (
        db.query(IssueComment)
        .filter(
            IssueComment.id == comment_id,
            IssueComment.issue_id == issue.id,
            IssueComment.workspace_id == access.workspace_id,
            IssueComment.deleted == 0,
        )
        .first()
    )
    if comment is None:
        return error_json(404, "Comment not found")
    if comment.author_id != access.user_id and not access.at_least("admin"):
        return error_json(403, "You can only delete your own comments")
    comment.deleted = 1
    from models.storage import IssueAttachment
    db.query(IssueAttachment).filter(
        IssueAttachment.comment_id == comment.id,
        IssueAttachment.workspace_id == access.workspace_id,
    ).update({"deleted": 1}, synchronize_session=False)
    db.commit()
    return SuccessResponse(status_code=200, message="Comment deleted", data={"id": comment_id})

