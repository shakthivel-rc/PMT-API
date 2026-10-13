"""Payloads of the issue, board and comment routes (`routes/issues.py`).

**Every view is a projection of the same `IssueCard`.** The board groups cards by status, the
list view pages them, the backlog orders them by `backlog_rank`, a future calendar places them
by `due_date` and a Gantt by `start_date`/`due_date` — none of them needs a different shape of
issue, only a different grouping of the same one. That is what makes a view switch in the SPA
a regrouping of data it already holds rather than a new fetch (docs/design/04-api-and-views.md).
"""
from datetime import date, datetime

from pydantic import BaseModel, Field

from schemas.responses.sprints import SprintSummary
from schemas.responses.workspaces import StatusSummary
from schemas.storage import AttachmentSummary, UserRef


class IssueCard(BaseModel):
    """An issue at the depth every view draws it."""

    id: str = Field(description="Opaque id; the key of every `/issues/{issue_id}` route.")
    key: str = Field(description="Human key, `PAY-142`. What branches and commits quote.")
    type: str = Field(description="epic, story, task, subtask or bug.")
    title: str = Field(description="One line.")
    status_id: str = Field(description="Its column.")
    list_id: str = Field(description="Its list.")
    parent_id: str | None = Field(description="Its parent in the hierarchy, or null.")
    priority: str = Field(description="urgent, high, medium, low or none.")
    story_points: int | None = Field(description="Estimate, or null when not estimated.")
    sprint_id: str | None = Field(default=None,
                                  description="The sprint it is in now, or null for the "
                                              "backlog.")
    time_estimate_seconds: int | None = Field(description="Time estimate in seconds, or null.")
    time_spent_seconds: int = Field(
        description="Time logged on this issue itself, in seconds — not its children's; the "
                    "drawer's time panel has the roll-up.",
    )
    assignee: UserRef | None = Field(description="Who it is assigned to, or null.")
    rank: str = Field(description="Sort key within the column. Opaque; compare as strings.")
    backlog_rank: str = Field(
        description="Sort key within the space's backlog — one order over the whole space, "
                    "kept apart from `rank`. Opaque; compare as strings.")
    start_date: date | None = Field(description="For timeline and Gantt views.")
    due_date: date | None = Field(description="For calendar, timeline and Gantt views.")
    pull_request_state: str | None = Field(
        description="The most advanced linked PR: `merged`, else `open`, else `closed`; "
                    "null when none is linked.",
    )
    blocked: bool = Field(description="Blocked work stays in its stage and still counts toward "
                                      "WIP; this flag and the reason make it visible.")
    blocked_reason: str | None = Field(
        description="What blocks it, who owns the blocker and the next action; null unless "
                    "blocked.")
    status_entered_at: datetime | None = Field(
        description="When it entered its current stage, UTC — the work-item age a Kanban "
                    "team inspects. Null only for a row nothing has moved since before it was "
                    "recorded.")
    version: int = Field(description="Send this back on PATCH; see `IssueUpdate.version`.")


class VcsLinkSummary(BaseModel):
    """A pull request (or branch, or commit) linked to an issue."""

    id: str = Field(description="Opaque id.")
    kind: str = Field(description="pull_request, branch or commit.")
    external_id: str = Field(description="`owner/name#123` for a pull request.")
    url: str = Field(description="Where it lives on the provider.")
    title: str | None = Field(description="PR title at the last delivery.")
    state: str | None = Field(description="open, closed or merged.")
    author_login: str | None = Field(description="Provider login of the author.")
    updated_at: datetime = Field(description="Last delivery that touched it, UTC.")


class IssueDetail(IssueCard):
    """An issue at the depth the detail drawer draws it."""

    description: str | None = Field(description="Markdown source. Render as text.")
    space_id: str = Field(description="Its space; fixes the key prefix and the workflow.")
    reporter: UserRef = Field(description="Who created it.")
    completion_criteria: str | None = Field(
        description="Markdown: what must be true for it to be done — an epic's Definition of "
                    "Done. Render as text.")
    previous_keys: list[str] = Field(
        description="Keys it had before moving to another space, oldest first. They still "
                    "resolve to it.")
    blocked_at: datetime | None = Field(description="When it was blocked, UTC; null unless "
                                                    "blocked.")
    blocked_by: UserRef | None = Field(description="Who marked it blocked; null unless blocked.")
    children: list[IssueCard] = Field(description="Direct children, in rank order.")
    links: list[VcsLinkSummary] = Field(description="Linked PRs, newest first.")
    created_at: datetime = Field(description="UTC.")
    updated_at: datetime = Field(description="UTC.")


class IssuePage(BaseModel):
    """One page of issues — the list view."""

    records: list[IssueCard] = Field(description="The issues on this page.")
    total: int = Field(description="How many issues match the filters across ALL pages — not "
                                   "the length of `records`.")


class ColumnWip(BaseModel):
    """A column's work in progress, counted for the team rather than for whoever is looking."""

    count: int = Field(description="Story, task and bug issues in this stage across the whole "
                                   "space, whatever the board's scope and filters.")
    limit: int | None = Field(description="The stage's WIP limit, or null when it has none or "
                                          "does not count toward WIP.")
    over_limit: bool = Field(description="`count` is above `limit`.")


class BoardColumn(BaseModel):
    """One status column and its cards."""

    status: StatusSummary = Field(description="The column.")
    wip: ColumnWip = Field(description="Its WIP count and limit; never narrowed by filters.")
    cards: list[IssueCard] = Field(description="Its issues, top to bottom (ascending rank).")


class BoardTransition(BaseModel):
    """One allowed move of a restricted workflow."""

    from_status_id: str | None = Field(description="The stage it leaves, or null for a global "
                                                   "transition: from any stage.")
    to_status_id: str = Field(description="The stage it enters.")


class Board(BaseModel):
    """The board view of one list, a whole space, or a sprint."""

    scope: str = Field(description="`list`, `space` or `sprint` — what `scope_id` names.")
    scope_id: str = Field(description="The list, space or sprint drawn.")
    space_id: str = Field(description="The space whose workflow the columns are.")
    transition_mode: str = Field(
        description="`any`: every stage reaches every other. `restricted`: only `transitions` "
                    "are allowed, so a drag can be checked before it is sent.")
    initial_status_id: str | None = Field(
        description="Where new issues start. In a restricted workflow, the only stage a "
                    "create may name.")
    transitions: list[BoardTransition] = Field(
        description="The space's stored transitions. Consulted only while `transition_mode` "
                    "is `restricted`; a reorder inside a stage is always allowed.")
    wip_policy: str = Field(description="`warn`: a move over a limit succeeds and says so. "
                                        "`block`: it is refused.")
    filtered: bool = Field(description="Whether any filter narrowed the cards.")
    total: int = Field(description="Cards returned, across every column.")
    columns: list[BoardColumn] = Field(description="Every status of the space, left to right, "
                                                   "including empty ones.")


class SprintPlan(BaseModel):
    """A sprint in the backlog view, with its issues."""

    sprint: SprintSummary = Field(description="The sprint and its live counts.")
    cards: list[IssueCard] = Field(description="Its issues, in backlog order, the filters "
                                               "applied.")


class BacklogCards(BaseModel):
    """The ordered backlog: work of the space that is in no sprint."""

    cards: list[IssueCard] = Field(description="Story, task and bug issues in backlog order, "
                                               "at most 500.")
    total: int = Field(description="How many match, including any past the first 500.")
    truncated: bool = Field(description="More matched than came back; filter to narrow it.")


class Backlog(BaseModel):
    """A space's planning view: its open sprints, then its ordered backlog."""

    space_id: str = Field(description="The space planned.")
    sprints: list[SprintPlan] = Field(description="The active sprint first, then planned "
                                                  "sprints, oldest first.")
    backlog: BacklogCards = Field(description="Open delivery items in no sprint, in order.")
    epics: list[IssueCard] = Field(description="The space's open epics, for filtering by "
                                               "`epic_id`. Never narrowed by filters.")


class IssueKeyResolution(BaseModel):
    """Where a key — current or old — points now: what a deep link opens."""

    issue: IssueCard = Field(description="The issue.")
    list_id: str = Field(description="Its list now.")
    space_id: str = Field(description="Its space now.")
    current_key: str = Field(description="Its key now; redirect a link that used another.")
    resolved_via: str = Field(
        description="`current` (the key it has), `space_alias` (its space's old key) or "
                    "`issue_alias` (a key it had before it moved).")


class CommentSummary(BaseModel):
    """A comment on an issue."""

    id: str = Field(description="Opaque id.")
    author: UserRef = Field(description="Who wrote it.")
    body: str = Field(description="Markdown source. Render as text.")
    created_at: datetime = Field(description="UTC.")
    attachments: list[AttachmentSummary] = Field(
        default_factory=list, description="Files attached to this comment."
    )
