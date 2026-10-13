"""Asking for AI work, and reading its result. Never doing it.

The request path inserts a row and returns 202 in the time of one small transaction; the
model call happens in `workers/ai_worker.py`. That split is the whole design: an LLM call
takes seconds to minutes, an API request must not, and a burst of summary requests must
queue up behind the worker pool rather than occupy every API thread.
"""
from sqlalchemy.orm import Session

from ai import prompts
from ai.context import issue_thread
from core.config import get_settings
from dependencies.workspace import WorkspaceAccess
from schemas.response import SuccessResponse, error_json
from services import ai_jobs
from services import issue as issue_service

SUMMARY_KIND = "summarize_issue_thread"


def request_issue_summary(access: WorkspaceAccess, issue_id: str, db: Session):
    if not get_settings().ai_enabled:
        return error_json(503, "AI features are turned off on this deployment. An "
                               "administrator can enable them with AI_ENABLED.")
    issue = issue_service.get_issue(db, access.workspace_id, issue_id)
    if issue is None:
        return error_json(404, "Issue not found")

    rendered = prompts.render_thread(issue_thread(db, access.workspace_id, issue))
    if len(rendered) > prompts.MAX_THREAD_CHARS:
        return error_json(422, "This thread is too long to summarise in one pass.")
    digest = prompts.input_hash(rendered)

    reusable = ai_jobs.find_reusable(db, access.workspace_id, issue.id, SUMMARY_KIND, digest)
    if reusable is not None:
        return SuccessResponse(status_code=202, message="Nothing has changed since the last "
                                                        "summary, so it was reused",
                               data=ai_jobs.job_payload(reusable, cached=True))
    in_flight = ai_jobs.find_in_flight(db, access.workspace_id, issue.id, SUMMARY_KIND)
    if in_flight is not None:
        return SuccessResponse(status_code=202, message="A summary is already being written",
                               data=ai_jobs.job_payload(in_flight))

    job = ai_jobs.enqueue(db, workspace_id=access.workspace_id, kind=SUMMARY_KIND,
                          subject_id=issue.id, requested_by=access.user_id,
                          input_hash=digest, prompt_version=prompts.PROMPT_VERSION)
    db.commit()
    return SuccessResponse(status_code=202, message="Summary requested",
                           data=ai_jobs.job_payload(job))


def get_ai_job(access: WorkspaceAccess, job_id: str, db: Session):
    job = ai_jobs.get_job(db, access.workspace_id, job_id)
    if job is None:
        return error_json(404, "Job not found")
    return SuccessResponse(status_code=200, message="Job fetched", data=ai_jobs.job_payload(job))
