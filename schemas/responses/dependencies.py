"""Payloads of the typed dependency routes (`routes/dependencies.py`).

A dependency is always read from one issue's side: the same `issue_links` row is `blocks` from
its source and `is blocked by` from its target, so the payload says which end the reader is on
(`direction`) and gives the sentence to show (`label`) rather than leaving every client to
derive it from the kind.
"""
from datetime import datetime

from pydantic import BaseModel, Field

from schemas.responses.issues import UserRef


class DependencyIssue(BaseModel):
    """The issue at the other end, at the depth a dependency row draws it."""

    id: str = Field(description="Opaque id.")
    key: str = Field(description="Its current key, `OPS-12` — never an old one.")
    title: str = Field(description="One line.")
    type: str = Field(description="epic, story, task, subtask or bug.")
    status_id: str = Field(description="Its stage.")
    status_name: str = Field(description="The stage's name, for the chip.")
    category: str = Field(description="The stage's category: `todo`, `in_progress`, `done` "
                                      "or `canceled`. A `blocks` whose blocker is closed no "
                                      "longer blocks anything.")
    space_id: str = Field(description="Its space — often not the reader's: cross-space "
                                      "coordination is what dependencies are for.")
    list_id: str = Field(description="Its list.")


class DependencySummary(BaseModel):
    """One typed dependency, from the side of the issue it was read from."""

    id: str = Field(description="Opaque id; what `DELETE …/dependencies/{id}` takes.")
    kind: str = Field(description="blocks, relates, duplicates or clones — as stored, "
                                  "source to target.")
    direction: str = Field(description="`outward` when the issue read from is the source, "
                                       "`inward` when it is the target. Always `outward` for "
                                       "the issue that `relates` was created from.")
    label: str = Field(description="How it reads from this side: `blocks`, `is blocked by`, "
                                   "`relates to`, `duplicates`, `is duplicated by`, `clones` "
                                   "or `is cloned by`.")
    issue: DependencyIssue = Field(description="The issue at the other end.")
    created_at: datetime = Field(description="UTC.")
    created_by: UserRef = Field(description="Who linked them.")
