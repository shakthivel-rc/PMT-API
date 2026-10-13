"""Payloads of the AI routes: a job, and what a finished summary job holds."""
from datetime import datetime

from pydantic import BaseModel, Field


class ActionItem(BaseModel):
    """Something the thread says someone should do."""

    owner: str | None = Field(description="Who, as the thread names them, or null if nobody "
                                          "was named. A name, never an account id.")
    text: str = Field(description="What.")


class ThreadSummary(BaseModel):
    """A summary of an issue's description, comments and linked pull requests.

    Structured, not prose, so the SPA renders each part as text in its own place: a summary
    is model output built from user-written input, and treating it as HTML or markdown would
    hand whoever wrote a comment a way to inject markup into everyone else's screen.
    """

    tldr: str = Field(description="Two or three sentences: where this stands now.")
    decisions: list[str] = Field(description="What the thread settled, one per item.")
    open_questions: list[str] = Field(description="What is still undecided or blocked.")
    action_items: list[ActionItem] = Field(description="Who should do what next.")


class AIJobSummary(BaseModel):
    """One queued, running or finished AI job. Poll it until `status` is terminal."""

    id: str = Field(description="Opaque id; poll `GET /workspaces/{workspace_id}/ai-jobs/{id}`.")
    kind: str = Field(description="`summarize_issue_thread` in Phase 1.")
    subject_id: str = Field(description="The issue summarised.")
    status: str = Field(description="queued, running, succeeded or failed. The last two "
                                    "are terminal; stop polling.")
    result: ThreadSummary | None = Field(description="Present once `succeeded`.")
    error: str | None = Field(description="Present once `failed`; written to be shown.")
    model: str | None = Field(description="The model that answered, once it has.")
    cached: bool = Field(description="True when this answer was reused because the thread "
                                     "has not changed since it was produced.")
    created_at: datetime = Field(description="When it was requested, UTC.")
    finished_at: datetime | None = Field(description="When it reached a terminal status.")
