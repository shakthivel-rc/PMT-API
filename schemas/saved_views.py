"""Saved board filters (`routes/saved_views.py`, docs/design/09-restructuring.md §5.4).

**`ViewFilters` is the one filter vocabulary** of the board, the list view and the backlog
(models/saved_view.py): who it is assigned to, which types and priorities, a search, blocked or
not, which sprint, which outcome, which epic. A saved view is a scope, a view type and one of
these documents. The column does not constrain the document, so every write goes through this
model and every read does too (services/saved_views.filters_of), the way the
`workspaces.settings` documents are handled.

Filters say what to *show*. None of them changes a stage, a WIP count or a transition — the
plan's "board filters do not create separate task states" (§4.6).
"""
from typing import Annotated, Literal

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)

from schemas.issues import IssueType, Priority

ViewType = Literal["board", "list", "backlog"]
ScopeKind = Literal["list", "space", "sprint"]
Visibility = Literal["private", "workspace"]
#: Every category a stage can have (utils/workflow_rules.STATUS_CATEGORIES), `canceled`
#: included — a "what did we drop" view is a filter like any other.
StatusCategory = Literal["todo", "in_progress", "done", "canceled"]

#: `saved_views.name` is VARCHAR(120).
MAX_VIEW_NAME = 120
#: A filter naming more people than this is a request for everyone, which is no filter;
#: fifty is a large team, and more than a board's assignee picker shows.
MAX_ASSIGNEES = 50
#: The board's and the list view's own cap on `q` (routes/issues.list_issues).
MAX_QUERY = 200
#: An id is a UUID, 36 characters; `me`, `none` and `active` are shorter.
MAX_REF = 36

#: An account id, `me` (whoever is looking — so one shared view works for everyone), or
#: `none` (unassigned).
AssigneeRef = Annotated[str, Field(min_length=1, max_length=MAX_REF)]


def _distinct(values: list) -> list:
    """In first-seen order: the same filter written twice is still one filter."""
    return list(dict.fromkeys(values))


class ViewFilters(BaseModel):
    """What a board, list or backlog shows. Every field is optional; an empty document shows
    everything in the scope."""

    model_config = ConfigDict(extra="forbid", json_schema_extra={
        "examples": [{"assignee_ids": ["me", "none"], "types": ["story", "bug"],
                      "blocked": True, "sprint": "active",
                      "status_categories": ["todo", "in_progress"]}],
    })

    assignee_ids: list[AssigneeRef] = Field(
        default_factory=list, max_length=MAX_ASSIGNEES,
        description="Account ids, `me` (the viewer) or `none` (unassigned); any of them.")
    types: list[IssueType] = Field(default_factory=list, description="Any of these types.")
    priorities: list[Priority] = Field(default_factory=list,
                                       description="Any of these priorities.")
    q: str | None = Field(default=None, max_length=MAX_QUERY,
                          description="Title, or an issue key — old keys included.")
    blocked: bool | None = Field(default=None,
                                 description="`true` only blocked work, `false` only unblocked "
                                             "work, null either.")
    sprint: str | None = Field(default=None, min_length=1, max_length=MAX_REF,
                               description="`none` (the backlog), `active` (the space's active "
                                           "sprint, whichever it is when the view is opened), "
                                           "a sprint id, or null for any.")
    status_categories: list[StatusCategory] = Field(
        default_factory=list, description="Any of these stage categories.")
    epic_id: str | None = Field(default=None, min_length=1, max_length=MAX_REF,
                                description="The epic and everything under it.")

    @field_validator("assignee_ids", "types", "priorities", "status_categories")
    @classmethod
    def _no_repeats(cls, values: list) -> list:
        return _distinct(values)

    @field_validator("q")
    @classmethod
    def _blank_is_none(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return value.strip() or None


def scope_problem(view_type: str, scope_kind: str) -> str | None:
    """Why a view of `view_type` cannot be scoped to a `scope_kind`, or None.

    A backlog is one ordered list per space (§5.2), so a backlog view is of a space. Boards
    are drawn for a list, a space or a sprint (§5.1), and the list view filters any of them.
    """
    if view_type == "backlog" and scope_kind != "space":
        return "A backlog view is scoped to a space: a backlog is one ordered list per space."
    return None


def _clean_name(value: str) -> str:
    name = value.strip()
    if not name:
        raise ValueError("A saved view needs a name.")
    return name


ViewName = Annotated[str, Field(min_length=1, max_length=MAX_VIEW_NAME),
                     AfterValidator(_clean_name)]


class SavedViewCreate(BaseModel):
    """A named scope, view and filter set."""

    model_config = ConfigDict(extra="forbid", json_schema_extra={
        "examples": [{"name": "My blocked work", "view_type": "board", "scope_kind": "space",
                      "scope_id": "<space id>",
                      "filters": {"assignee_ids": ["me"], "blocked": True},
                      "visibility": "private"}],
    })

    name: ViewName = Field(description="What the view is called in the picker.")
    view_type: ViewType = Field(default="board",
                                description="board, list or backlog. A backlog view is of a "
                                            "space.")
    scope_kind: ScopeKind = Field(description="What `scope_id` names: a list, a space or a "
                                              "sprint.")
    scope_id: str = Field(min_length=1, max_length=MAX_REF,
                          description="A list, space or sprint of this workspace.")
    filters: ViewFilters = Field(default_factory=ViewFilters,
                                 description="What the view shows of its scope. Omitted: "
                                             "everything.")
    visibility: Visibility = Field(default="private",
                                   description="`private`: only you see it. `workspace`: every "
                                               "member sees it; only you change it, and an "
                                               "admin may delete it.")

    @model_validator(mode="after")
    def _scope_fits_the_view(self) -> "SavedViewCreate":
        problem = scope_problem(self.view_type, self.scope_kind)
        if problem:
            raise ValueError(problem)
        return self


class SavedViewUpdate(BaseModel):
    """A partial update: only the fields present change. `scope_kind` and `scope_id` change
    together or not at all, because one without the other names a different kind of thing."""

    model_config = ConfigDict(extra="forbid", json_schema_extra={
        "examples": [{"filters": {"types": ["bug"]}}, {"visibility": "workspace"}]})

    name: ViewName | None = Field(default=None, description="A new name. Cannot be null.")
    view_type: ViewType | None = Field(default=None,
                                       description="board, list or backlog. Cannot be null.")
    scope_kind: ScopeKind | None = Field(default=None,
                                         description="With `scope_id`: what it now names.")
    scope_id: str | None = Field(default=None, min_length=1, max_length=MAX_REF,
                                 description="With `scope_kind`: a list, space or sprint of "
                                             "this workspace.")
    filters: ViewFilters | None = Field(default=None,
                                        description="The whole new filter document — it "
                                                    "replaces the old one, it is not merged.")
    visibility: Visibility | None = Field(default=None,
                                          description="private or workspace. Cannot be null.")

    @model_validator(mode="after")
    def _scope_together(self) -> "SavedViewUpdate":
        if ("scope_kind" in self.model_fields_set) != ("scope_id" in self.model_fields_set):
            raise ValueError("Change scope_kind and scope_id together.")
        return self
