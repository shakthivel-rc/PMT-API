"""Payloads of the release and fix-version routes (`routes/releases.py`).

Progress and release notes are computed from the issues on every read, never stored
(models/release.py): an issue that moves to Done after a release is created must show up in
its progress without anyone recomputing anything.
"""
from datetime import date, datetime

from pydantic import BaseModel, Field

from schemas.responses.issues import IssueCard


class ReleaseProgress(BaseModel):
    """The release's live issues by outcome. `done + canceled + open == issues`."""

    issues: int = Field(description="Live issues with this fix version.")
    done: int = Field(description="In a `done` stage: delivered.")
    canceled: int = Field(description="In a `canceled` stage: closed, not delivered.")
    open: int = Field(description="In an open stage (`todo`, `in_progress`): still to do.")


class FixVersion(BaseModel):
    """A release as an issue's fix version — enough to draw the chip."""

    id: str = Field(description="Opaque id.")
    space_id: str = Field(description="The space it belongs to.")
    name: str = Field(description="`2.4`.")
    state: str = Field(description="unreleased, released or archived.")
    start_date: date | None = Field(description="When work on it starts.")
    release_date: date | None = Field(description="Planned, or — once released — actual.")
    released_at: datetime | None = Field(description="When it was released, UTC; null until "
                                                     "then.")


class ReleaseSummary(FixVersion):
    """A release, with what is in it."""

    description: str | None = Field(description="Plain text. Render as text.")
    created_at: datetime = Field(description="UTC.")
    updated_at: datetime = Field(description="UTC.")
    progress: ReleaseProgress = Field(description="Its issues by outcome, now.")


class ReleaseDetail(ReleaseSummary):
    """A release and its issues."""

    issues: list[IssueCard] = Field(description="Its live issues as board cards, in key "
                                                "order — at most 500 (`truncated`).")
    truncated: bool = Field(description="True when it holds more issues than `issues` "
                                        "carries; `progress` always counts them all.")


class ReleaseOutcome(BaseModel):
    """What releasing did."""

    release: ReleaseSummary = Field(description="The release, now released.")
    moved_issue_ids: list[str] = Field(description="Open issues moved to `moved_to`; empty "
                                                   "when none were asked to move.")
    moved_to: ReleaseSummary | None = Field(description="The release they moved to, or null.")


class ReleaseNoteItem(BaseModel):
    """One issue in the notes."""

    id: str = Field(description="Opaque id, for a link to the issue.")
    key: str = Field(description="Its current key.")
    title: str = Field(description="One line.")
    type: str = Field(description="Its issue type.")
    outcome: str = Field(description="`done`, `canceled` or `open`.")


class ReleaseNotesSection(BaseModel):
    """One section of the notes."""

    type: str = Field(description="An issue type (`epic`, `story`, `task`, `bug`, "
                                  "`subtask`) for a section of delivered work; `canceled` or "
                                  "`open` for the sections of what did not ship.")
    title: str = Field(description="The section's heading, as it appears in `text`.")
    issues: list[ReleaseNoteItem] = Field(description="In key order.")


class ReleaseNotes(BaseModel):
    """Release notes, generated from the release's issues and their outcome."""

    release: ReleaseSummary = Field(description="The release.")
    text: str = Field(description="Plain text, ready to paste: a heading line, then one "
                                  "section per issue type of delivered work with a `KEY "
                                  "title` line per issue, then Canceled and Still open.")
    sections: list[ReleaseNotesSection] = Field(description="The same, structured. Empty "
                                                            "sections are left out.")
