"""Request bodies for the issue, board and comment routes (`routes/issues.py`)."""
from datetime import date
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from utils.time_tracking import MAX_ESTIMATE_SECONDS

IssueType = Literal["epic", "story", "task", "subtask", "bug"]
Priority = Literal["urgent", "high", "medium", "low", "none"]

#: An epic's completion criteria — its Definition of Done (docs/design/09-restructuring.md
#: §2.3). The same ceiling as a comment's body: a checklist of what must be true is a few
#: paragraphs, and twenty thousand characters is ample for one while keeping a single field
#: from becoming the place a specification document gets pasted.
MAX_COMPLETION_CRITERIA = 20_000

#: `issues.blocked_reason` is VARCHAR(1000): what blocks it, who owns the blocker and the
#: next action (plan §4) is a sentence or three, not an essay.
MAX_BLOCKED_REASON = 1_000


class IssueCreate(BaseModel):
    """A new issue. It lands at the bottom of its status column."""

    model_config = ConfigDict(json_schema_extra={
        "examples": [{
            "list_id": "9f0c…", "type": "story", "title": "Refund to original card",
            "description": "As a customer I want…", "priority": "high", "story_points": 5,
        }],
    })

    list_id: str = Field(description="The list the issue belongs to. Fixes its space and key.")
    type: IssueType = Field(default="task", description="Constrains which parent it may have.")
    title: str = Field(min_length=1, max_length=500, description="One line.")
    description: str | None = Field(default=None, max_length=100_000,
                                    description="Markdown. Rendered as text, never as HTML.")
    parent_id: str | None = Field(
        default=None,
        description="Epic > story, task or bug > subtask: a story, task or bug may sit under "
                    "an epic or stand alone, and a subtask sits under a story, task or bug "
                    "and requires one. Must be in the same space.",
    )
    status_id: str | None = Field(
        default=None,
        description="A live stage of the list's space. Default: the space's initial stage. "
                    "In a restricted workflow only the initial stage is accepted.")
    assignee_id: str | None = Field(default=None,
                                    description="A member of this workspace, or null. An "
                                                "epic's assignee is its owner.")
    priority: Priority = Field(default="none", description="Sort and badge only.")
    story_points: int | None = Field(default=None, ge=0, le=100,
                                     description="Estimate; null means not estimated.")
    time_estimate_seconds: int | None = Field(
        default=None, ge=0, le=MAX_ESTIMATE_SECONDS,
        description="Time estimate in seconds, at most 1,000 hours; null means none.",
    )
    due_date: date | None = Field(default=None, description="ISO date, no time.")
    completion_criteria: str | None = Field(
        default=None, max_length=MAX_COMPLETION_CRITERIA,
        description="Markdown: what must be true for it to be done. An epic needs it, and an "
                    "assignee, while the workspace's governance setting "
                    "`require_epic_owner_and_criteria` is on (the default).")


class IssueUpdate(BaseModel):
    """A partial update. Only the fields present in the body change.

    To clear a nullable field send it as `null`; to leave it alone, omit it. The two are
    different requests, which is why the controller reads `model_fields_set` rather than
    comparing against None.
    """

    model_config = ConfigDict(json_schema_extra={
        "examples": [{"version": 3, "assignee_id": None, "story_points": 8},
                     {"version": 4, "blocked": True,
                      "blocked_reason": "Waiting on the payment provider's sandbox keys; "
                                        "Priya is chasing them, next check Thursday."}],
    })

    version: int = Field(ge=1, description="The `version` you read. A mismatch is a 409: "
                                           "someone changed the issue since.")
    title: str | None = Field(default=None, min_length=1, max_length=500,
                              description="One line.")
    description: str | None = Field(default=None, max_length=100_000, description="Markdown.")
    type: IssueType | None = Field(
        default=None,
        description="Re-type. Its relationship to its parent and every child's relationship "
                    "to it are checked against the standard rules.")
    parent_id: str | None = Field(
        default=None,
        description="New parent, or null to detach. The new relationship must follow the "
                    "standard rules; one left alone is never re-checked.")
    assignee_id: str | None = Field(default=None, description="A member, or null to unassign.")
    priority: Priority | None = Field(default=None, description="Sort and badge only.")
    story_points: int | None = Field(default=None, ge=0, le=100, description="Estimate.")
    time_estimate_seconds: int | None = Field(default=None, ge=0, le=MAX_ESTIMATE_SECONDS,
                                              description="Time estimate in seconds, or null "
                                                          "to clear.")
    due_date: date | None = Field(default=None, description="ISO date, or null to clear.")
    completion_criteria: str | None = Field(
        default=None, max_length=MAX_COMPLETION_CRITERIA,
        description="Markdown: what must be true for it to be done. An epic's cannot be "
                    "blanked while the governance setting is on.")
    blocked: bool | None = Field(
        default=None,
        description="true marks it blocked — it stays in its stage and keeps counting toward "
                    "WIP — and needs a reason, in this body or already stored. false clears "
                    "the flag and the reason.")
    blocked_reason: str | None = Field(
        default=None, max_length=MAX_BLOCKED_REASON,
        description="What blocks it, who owns the blocker, and the next action. Only a "
                    "blocked issue has one; changing it while blocked is recorded again.")


class IssueMove(BaseModel):
    """A drag-and-drop on the board: a column, and the two cards the issue now sits between."""

    model_config = ConfigDict(json_schema_extra={
        "examples": [{"status_id": "3b7e…", "after_id": "c1…", "before_id": None}],
    })

    status_id: str = Field(description="The destination column.")
    after_id: str | None = Field(
        default=None,
        description="The card now directly ABOVE the moved one, or null for the top.",
    )
    before_id: str | None = Field(
        default=None,
        description="The card now directly BELOW the moved one, or null for the bottom.",
    )


class BacklogRank(BaseModel):
    """A drag in the space's backlog: the two issues the moved one now sits between.

    The same anchoring as a board move (`IssueMove`): the issue named in `after_id` is
    trusted and the one really below it is read on the server, so a stale backlog still drops
    where the person aimed.
    """

    model_config = ConfigDict(json_schema_extra={
        "examples": [{"after_id": "c1…", "before_id": "d4…"}],
    })

    after_id: str | None = Field(
        default=None,
        description="The issue now directly ABOVE the moved one, or null.")
    before_id: str | None = Field(
        default=None,
        description="The issue now directly BELOW the moved one, or null. Both null: the "
                    "bottom of the backlog.")


class IssueFilters(BaseModel):
    """The board's filters (docs/design/09-restructuring.md §5.1), read from the query string.

    One model for the board, the backlog and the list view, so the three cannot disagree
    about what "assigned to nobody" or "blocked" means — and so a saved view's filters
    (§5.4) apply the same way whichever view it opens. The field names are the query
    parameters' own, repeatable ones included (`?type=story&type=bug` is `type: [story,
    bug]`), so a stored filter document validates here and turns back into a query string
    without a mapping table.
    """

    model_config = ConfigDict(frozen=True)

    assignee_id: str | None = Field(
        default=None, description="An account id, `me`, or `none` for unassigned work.")
    type: list[IssueType] = Field(default_factory=list,
                                  description="Any of these types. Empty: every type.")
    priority: list[Priority] = Field(default_factory=list,
                                     description="Any of these priorities. Empty: all.")
    q: str | None = Field(
        default=None, max_length=200,
        description="A substring of the title, or an issue key — old keys included.")
    blocked: bool | None = Field(
        default=None, description="true: only blocked work. false: only unblocked work.")
    epic_id: str | None = Field(default=None,
                                description="The issue and everything beneath it.")

    def active(self) -> bool:
        """Whether any filter narrows the result — the board's `filtered` flag."""
        return bool(self.assignee_id or self.type or self.priority
                    or (self.q and self.q.strip()) or self.blocked is not None
                    or self.epic_id)


class CommentCreate(BaseModel):
    """A comment on an issue."""

    model_config = ConfigDict(json_schema_extra={
        "examples": [{"body": "Reproduced on staging — the 3DS redirect drops the cart id."}],
    })

    body: str = Field(min_length=1, max_length=20_000, description="Markdown.")
    attachment_ids: list[str] = Field(
        default_factory=list,
        description="IDs of uploaded attachments to associate with this comment.",
    )


class CommentUpdate(BaseModel):
    """Update a comment on an issue."""

    model_config = ConfigDict(json_schema_extra={
        "examples": [{"body": "Updated comment details with fix notes."}],
    })

    body: str = Field(min_length=1, max_length=20_000, description="Markdown.")
    attachment_ids: list[str] | None = Field(
        default=None,
        description="Optional attachment IDs to associate with this comment.",
    )
