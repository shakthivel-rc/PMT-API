"""Issue, board, backlog, comment and AI routes, all under `/workspaces/{workspace_id}`.

**The views share one resource.** The board (`GET …/board`), the list (`GET …/issues`) and
the backlog (`GET …/spaces/{space_id}/backlog`) return the same `IssueCard`; they differ in
grouping, ordering and paging, not in what an issue is, and they take the same filters
(`issue_filters`). docs/design/04-api-and-views.md explains how that keeps a view switch in
the SPA free; docs/design/09-restructuring.md §5 is what each view is for.
"""
from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from controllers import ai_controller, issue_controller
from db.session import get_db
from dependencies.workspace import WorkspaceAccess, as_guest, as_member
from routes.workspaces import NOT_A_MEMBER
from schemas.errors import COMMON_ERRORS, VALIDATION_ERROR
from schemas.issues import (
    BacklogRank,
    CommentCreate,
    CommentUpdate,
    IssueCreate,
    IssueFilters,
    IssueMove,
    IssueType,
    IssueUpdate,
    Priority,
)
from schemas.response import ErrorResponse, SuccessResponse
from schemas.responses.ai import AIJobSummary
from schemas.responses.issues import (
    Backlog,
    Board,
    CommentSummary,
    IssueCard,
    IssueDetail,
    IssueKeyResolution,
    IssuePage,
)

router = APIRouter(prefix="/workspaces/{workspace_id}", tags=["Issues"])

UNPROCESSABLE = {422: {"model": ErrorResponse,
                       "description": "The request is well-formed but breaks a rule — a "
                                      "parent of the wrong type, an epic without an owner or "
                                      "completion criteria, blocking without a reason, an "
                                      "assignee who is not a member, a status from another "
                                      "workflow, a move the workflow does not allow."}}
STALE = {409: {"model": ErrorResponse,
               "description": "`version` is stale: someone changed the issue since you read "
                              "it. `data.current_version` says what it is now."}}
#: docs/design/09-restructuring.md §3.4: the one write guard's answer.
LOCKED = {423: {"model": ErrorResponse,
                "description": "Read-only: the issue's list, folder or space is archived, or "
                               "its space is frozen while a migration runs. The message names "
                               "the container and what would lift it."}}
WIP_LIMIT = {409: {"model": ErrorResponse,
                   "description": "The destination stage is at its WIP limit and the space's "
                                  "WIP policy is `block`. Under `warn` the request succeeds "
                                  "and the message says the stage is over."}}
CREATE_CONFLICT = {409: {"model": ErrorResponse,
                         "description": "The stage is at its WIP limit under a `block` policy "
                                        "(under `warn` the create succeeds and the message "
                                        "says so); the space has no stages; or another issue "
                                        "was numbered at the same moment — try again."}}


def issue_filters(
    assignee_id: str | None = Query(None, description="An account id, `me`, or `none` for "
                                                      "unassigned work."),
    type: list[IssueType] | None = Query(None, description="Only these types. Repeat it for "
                                                           "several."),
    priority: list[Priority] | None = Query(None, description="Only these priorities. Repeat "
                                                              "it for several."),
    q: str | None = Query(None, max_length=200,
                          description="A substring of the title, or an issue key — an old "
                                      "key still finds the issue that moved."),
    blocked: bool | None = Query(None, description="true: only blocked work. false: only "
                                                   "unblocked work."),
    epic_id: str | None = Query(None, description="An issue — usually an epic — and "
                                                  "everything beneath it."),
) -> IssueFilters:
    """The board's filters (09-restructuring.md §5.1), shared by every view so a saved view
    means the same thing whichever one it opens."""
    return IssueFilters(assignee_id=assignee_id, type=list(type or []),
                        priority=list(priority or []), q=q, blocked=blocked, epic_id=epic_id)


@router.post(
    "/issues",
    response_model=SuccessResponse[IssueDetail],
    status_code=201,
    responses={**COMMON_ERRORS, **VALIDATION_ERROR, **NOT_A_MEMBER, **UNPROCESSABLE,
               **CREATE_CONFLICT, **LOCKED},
    summary="Create an issue at the bottom of its column",
    operation_id="create_issue",
)
def create_issue(body: IssueCreate,
                 access: WorkspaceAccess = Depends(as_member),
                 db: Session = Depends(get_db)):
    """It starts in the space's initial stage unless `status_id` names another live one; a
    restricted workflow accepts only the initial stage. Creating into a stage with a WIP
    limit is held to it as a move is."""
    return issue_controller.create_issue(access, body, db)


@router.get(
    "/issues",
    response_model=SuccessResponse[IssuePage],
    responses={**COMMON_ERRORS, **VALIDATION_ERROR, **NOT_A_MEMBER},
    summary="List issues, filtered and paged — the list view",
    operation_id="list_issues",
)
def list_issues(
    access: WorkspaceAccess = Depends(as_guest),
    db: Session = Depends(get_db),
    filters: IssueFilters = Depends(issue_filters),
    list_id: str | None = Query(None, description="Only this list."),
    space_id: str | None = Query(None, description="Only this space."),
    status_id: str | None = Query(None, description="Only this column."),
    sprint_id: str | None = Query(None, description="Only this sprint, or `none` for "
                                                    "unscheduled work."),
    page: int = Query(1, ge=1),
    limit: int = Query(50, ge=1, le=200),
    sort_by: str = Query("number", description="number, updated_at, due_date or priority."),
    order: str = Query("desc", pattern="^(asc|desc)$"),
):
    """Phase 2 replaces the individual filters with one `pql` query string
    (docs/design/04-api-and-views.md, "Query language"); these stay as its simple form."""
    return issue_controller.list_issues(
        access, db, list_id=list_id, space_id=space_id, status_id=status_id,
        sprint_id=sprint_id, filters=filters, page=page, limit=limit, sort_by=sort_by,
        order=order,
    )


@router.get(
    "/board",
    response_model=SuccessResponse[Board],
    responses={**COMMON_ERRORS, **VALIDATION_ERROR, **NOT_A_MEMBER,
               400: {"model": ErrorResponse,
                     "description": "Not exactly one of list_id, space_id and sprint_id."}},
    summary="Get the board view of a list, a whole space or a sprint",
    operation_id="get_board",
)
def get_board(
    access: WorkspaceAccess = Depends(as_guest),
    db: Session = Depends(get_db),
    filters: IssueFilters = Depends(issue_filters),
    list_id: str | None = Query(None, description="Draw this list. Give exactly one of "
                                                  "list_id, space_id and sprint_id."),
    space_id: str | None = Query(None, description="Draw every list of this space."),
    sprint_id: str | None = Query(None, description="Draw this sprint's issues, across "
                                                    "lists, whatever its state."),
):
    """Every column of the space's workflow, including empty ones, cards in rank order. Each
    column's `wip` is counted across the whole space without the filters: WIP belongs to the
    team, not to whoever is filtering."""
    return issue_controller.get_board(access, db, list_id=list_id, space_id=space_id,
                                      sprint_id=sprint_id, filters=filters)


@router.get(
    "/spaces/{space_id}/backlog",
    response_model=SuccessResponse[Backlog],
    responses={**COMMON_ERRORS, **VALIDATION_ERROR, **NOT_A_MEMBER},
    summary="Get a space's backlog: its open sprints, then the ordered backlog",
    operation_id="get_space_backlog",
)
def get_backlog(space_id: str,
                access: WorkspaceAccess = Depends(as_guest),
                db: Session = Depends(get_db),
                filters: IssueFilters = Depends(issue_filters),
                include_closed: bool = Query(False, description="Include closed story, task "
                                                                "and bug issues that are in "
                                                                "no sprint.")):
    """The active sprint, then planned sprints oldest first, each with its issues; then the
    story, task and bug issues in no sprint, in backlog order, at most 500; then the space's
    open epics. Reorder with `POST …/issues/{issue_id}/backlog-rank`; plan with the sprint
    routes."""
    return issue_controller.get_backlog(access, space_id, filters, include_closed, db)


@router.get(
    "/issue-keys/{key}",
    response_model=SuccessResponse[IssueKeyResolution],
    responses={**COMMON_ERRORS, **NOT_A_MEMBER,
               404: {"model": ErrorResponse,
                     "description": "No issue in this workspace has that key, or it is not "
                                    "a key."}},
    summary="Find the issue a key names, including keys it had before it moved",
    operation_id="resolve_issue_key",
)
def resolve_issue_key(key: str,
                      access: WorkspaceAccess = Depends(as_guest),
                      db: Session = Depends(get_db)):
    """What a deep link opens. `PAY-7` resolves by the live key, then by the space's old
    keys, then by the issue's own old keys; `current_key` is the one to redirect to."""
    return issue_controller.resolve_issue_key(access, key, db)


@router.get(
    "/issues/{issue_id}",
    response_model=SuccessResponse[IssueDetail],
    responses={**COMMON_ERRORS, **NOT_A_MEMBER},
    summary="Get one issue with its children and linked pull requests",
    operation_id="get_issue",
)
def get_issue(issue_id: str,
              access: WorkspaceAccess = Depends(as_guest),
              db: Session = Depends(get_db)):
    return issue_controller.get_issue(access, issue_id, db)


@router.patch(
    "/issues/{issue_id}",
    response_model=SuccessResponse[IssueDetail],
    responses={**COMMON_ERRORS, **VALIDATION_ERROR, **NOT_A_MEMBER, **STALE, **UNPROCESSABLE,
               **LOCKED},
    summary="Change some fields of an issue",
    operation_id="update_issue",
)
def update_issue(issue_id: str, body: IssueUpdate,
                 access: WorkspaceAccess = Depends(as_member),
                 db: Session = Depends(get_db)):
    """Only the fields present in the body change; `version` must be the one you read. A new
    parent or type follows the standard hierarchy; a relationship left alone is never
    re-checked."""
    return issue_controller.update_issue(access, issue_id, body, db)


@router.post(
    "/issues/{issue_id}/move",
    response_model=SuccessResponse[IssueCard],
    responses={**COMMON_ERRORS, **VALIDATION_ERROR, **NOT_A_MEMBER, **UNPROCESSABLE,
               **WIP_LIMIT, **LOCKED},
    summary="Move an issue on the board: a column, and a place in it",
    operation_id="move_issue",
)
def move_issue(issue_id: str, body: IssueMove,
               access: WorkspaceAccess = Depends(as_member),
               db: Session = Depends(get_db)):
    """Rewrites one row whatever the column's length (utils/rank.py). The card named in
    `after_id` is the anchor; the server finds the card below it itself, so a stale board
    still drops where the person aimed. A restricted workflow refuses a move it does not
    allow, naming where the issue can go."""
    return issue_controller.move_issue(access, issue_id, body, db)


@router.post(
    "/issues/{issue_id}/backlog-rank",
    response_model=SuccessResponse[IssueCard],
    responses={**COMMON_ERRORS, **VALIDATION_ERROR, **NOT_A_MEMBER, **LOCKED},
    summary="Move an issue in its space's backlog order",
    operation_id="rank_issue_in_backlog",
)
def rank_in_backlog(issue_id: str, body: BacklogRank,
                    access: WorkspaceAccess = Depends(as_member),
                    db: Session = Depends(get_db)):
    """Rewrites one row's `backlog_rank`, anchored like a board move. Planning, not an edit:
    `version` does not change."""
    return issue_controller.rank_in_backlog(access, issue_id, body, db)


@router.delete(
    "/issues/{issue_id}",
    response_model=SuccessResponse[dict],
    responses={**COMMON_ERRORS, **NOT_A_MEMBER, **LOCKED,
               409: {"model": ErrorResponse, "description": "It still has child issues."}},
    summary="Delete an issue",
    operation_id="delete_issue",
)
def delete_issue(issue_id: str,
                 access: WorkspaceAccess = Depends(as_member),
                 db: Session = Depends(get_db)):
    """A soft delete: the row and its history stay, for the reports."""
    return issue_controller.delete_issue(access, issue_id, db)


@router.get(
    "/issues/{issue_id}/comments",
    response_model=SuccessResponse[list[CommentSummary]],
    responses={**COMMON_ERRORS, **NOT_A_MEMBER},
    summary="List an issue's comments, oldest first",
    operation_id="list_issue_comments",
)
def list_comments(issue_id: str,
                  access: WorkspaceAccess = Depends(as_guest),
                  db: Session = Depends(get_db)):
    return issue_controller.list_comments(access, issue_id, db)


@router.post(
    "/issues/{issue_id}/comments",
    response_model=SuccessResponse[CommentSummary],
    status_code=201,
    responses={**COMMON_ERRORS, **VALIDATION_ERROR, **NOT_A_MEMBER, **LOCKED},
    summary="Comment on an issue",
    operation_id="create_issue_comment",
)
def create_comment(issue_id: str, body: CommentCreate,
                   access: WorkspaceAccess = Depends(as_member),
                   db: Session = Depends(get_db)):
    return issue_controller.create_comment(access, issue_id, body, db)


@router.patch(
    "/issues/{issue_id}/comments/{comment_id}",
    response_model=SuccessResponse[CommentSummary],
    responses={**COMMON_ERRORS, **VALIDATION_ERROR, **NOT_A_MEMBER, **LOCKED},
    summary="Update a comment",
    operation_id="update_issue_comment",
)
def update_comment(issue_id: str, comment_id: str, body: CommentUpdate,
                   access: WorkspaceAccess = Depends(as_member),
                   db: Session = Depends(get_db)):
    return issue_controller.update_comment(access, issue_id, comment_id, body, db)


@router.delete(
    "/issues/{issue_id}/comments/{comment_id}",
    response_model=SuccessResponse[dict],
    responses={**COMMON_ERRORS, **NOT_A_MEMBER, **LOCKED},
    summary="Delete a comment",
    operation_id="delete_issue_comment",
)
def delete_comment(issue_id: str, comment_id: str,
                   access: WorkspaceAccess = Depends(as_member),
                   db: Session = Depends(get_db)):
    return issue_controller.delete_comment(access, issue_id, comment_id, db)


@router.post(
    "/issues/{issue_id}/summary",
    response_model=SuccessResponse[AIJobSummary],
    status_code=202,
    responses={**COMMON_ERRORS, **NOT_A_MEMBER,
               422: {"model": ErrorResponse, "description": "The thread is too long."},
               503: {"model": ErrorResponse,
                     "description": "AI features are turned off on this deployment."}},
    summary="Ask for an AI summary of an issue's thread",
    operation_id="request_issue_summary",
)
def request_summary(issue_id: str,
                    access: WorkspaceAccess = Depends(as_member),
                    db: Session = Depends(get_db)):
    """Queues the work and returns the job; poll `GET …/ai-jobs/{id}`. If nothing in the
    thread has changed since the last summary, that job comes back with `cached: true`
    and no model is called. `member` rather than `guest` because every call costs money."""
    return ai_controller.request_issue_summary(access, issue_id, db)


@router.get(
    "/ai-jobs/{job_id}",
    response_model=SuccessResponse[AIJobSummary],
    responses={**COMMON_ERRORS, **NOT_A_MEMBER},
    summary="Get an AI job's status and, once finished, its result",
    operation_id="get_ai_job",
)
def get_ai_job(job_id: str,
               access: WorkspaceAccess = Depends(as_guest),
               db: Session = Depends(get_db)):
    return ai_controller.get_ai_job(access, job_id, db)
