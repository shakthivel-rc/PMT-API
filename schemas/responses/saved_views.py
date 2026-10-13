"""Payloads of the saved board filter routes (`routes/saved_views.py`)."""
from datetime import datetime

from pydantic import BaseModel, Field

from schemas.responses.issues import UserRef
from schemas.saved_views import ViewFilters


class SavedViewSummary(BaseModel):
    """A saved view, as the view picker lists it and as a board opens it."""

    id: str = Field(description="Opaque id.")
    name: str = Field(description="What it is called in the picker.")
    view_type: str = Field(description="board, list or backlog.")
    scope_kind: str = Field(description="list, space or sprint.")
    scope_id: str = Field(description="The list, space or sprint it shows.")
    scope_name: str | None = Field(description="That list's, space's or sprint's name now; "
                                               "null when it has since been deleted, which "
                                               "leaves the view with nothing to open.")
    filters: ViewFilters = Field(description="What it shows of the scope.")
    visibility: str = Field(description="`private` (its owner only) or `workspace` (every "
                                        "member).")
    owner: UserRef = Field(description="Who saved it.")
    can_edit: bool = Field(description="Whether you may change it: its owner only.")
    can_delete: bool = Field(description="Whether you may delete it: its owner, or an admin "
                                         "when it is shared with the workspace.")
    created_at: datetime = Field(description="UTC.")
    updated_at: datetime = Field(description="UTC.")
