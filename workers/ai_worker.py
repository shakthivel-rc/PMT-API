"""The AI worker: claim a job, call the model, write the answer back. Repeat.

    python -m workers.ai_worker

Run as its own process (the `ai-worker` compose service; a Deployment of its own on
Kubernetes), scaled independently of the API. It refuses to start when `AI_ENABLED` is off.

**No database connection is held during a model call.** A job is claimed in one short
transaction, the input is read in a second, the model is called with no session open, and
the answer is written in a third. A worker that held its connection for the length of a
model call would pin one pooled connection per in-flight summary, and a burst of summaries
would starve the API of connections to the same database — the exact coupling the queue
exists to prevent.

**At-least-once.** A worker killed mid-call leaves its job `running` with a lease; the next
claim after the lease lapses retakes it. The handlers are therefore safe to run twice, and
the finishing write is fenced on the attempt number (`services/ai_jobs.py`).

**Graceful shutdown.** SIGTERM (what Kubernetes and `docker compose stop` send) lets the
current job finish and then exits, so a rolling deploy does not waste the call in flight.
"""
from __future__ import annotations

import logging
import signal
import time
from collections.abc import Callable

from pydantic import ValidationError

import db.session as db_session
from ai import prompts
from ai.client import AIError, ClaudeJSONClient
from ai.context import issue_thread
from core.config import get_settings
from core.logging import configure_logging
from models.ai_job import AIJob
from schemas.responses.ai import ThreadSummary
from services import ai_jobs
from services import issue as issue_service

logger = logging.getLogger("base_project.ai_worker")

#: How long a claimed job is ours. Above the worst case of one call — 3 attempts x 180 s
#: (ai/client.py) is 9 minutes — so a live worker is never overtaken, and short enough that
#: a crashed worker's job is picked up within a quarter of an hour.
LEASE_SECONDS = 15 * 60

#: How long an idle worker sleeps between claims. The claim is one indexed query, so a
#: second costs the database next to nothing and keeps the wait a user sees short.
IDLE_POLL_SECONDS = 1.0


class _Stop:
    requested = False


def _summarize(job_id: str, attempt: int, client: ClaudeJSONClient, max_attempts: int) -> None:
    # 1. Read the input. Short transaction; closed before the model is called.
    with db_session.SessionLocal() as db:
        job = db.get(AIJob, job_id)
        issue = issue_service.get_issue(db, job.workspace_id, job.subject_id) if job else None
        if job is None or issue is None:
            if job is not None:
                ai_jobs.fail(db, job_id=job_id, attempt=attempt, retryable=False,
                             message="The issue was deleted before it could be summarised.",
                             max_attempts=max_attempts)
            return
        rendered = prompts.render_thread(issue_thread(db, job.workspace_id, issue))

    if len(rendered) > prompts.MAX_THREAD_CHARS:
        with db_session.SessionLocal() as db:
            ai_jobs.fail(db, job_id=job_id, attempt=attempt, retryable=False,
                         message="This thread is too long to summarise in one pass.",
                         max_attempts=max_attempts)
        return

    # 2. Call the model with no session open.
    try:
        completion = client.complete_json(system=prompts.SYSTEM, user=rendered,
                                          schema=prompts.OUTPUT_SCHEMA)
        summary = ThreadSummary.model_validate(completion.data)
    except (AIError, ValidationError) as exc:
        retryable = exc.retryable if isinstance(exc, AIError) else True
        message = (exc.user_message if isinstance(exc, AIError)
                   else "The model returned a summary in an unexpected shape.")
        with db_session.SessionLocal() as db:
            status = ai_jobs.fail(db, job_id=job_id, attempt=attempt, message=message,
                                  retryable=retryable, max_attempts=max_attempts)
        logger.warning("ai job attempt failed", extra={"extra_fields": {
            "job_id": job_id, "attempt": attempt, "retryable": retryable, "status": status}})
        return

    # 3. Write the answer, fenced on the attempt we claimed.
    with db_session.SessionLocal() as db:
        landed = ai_jobs.complete(
            db, job_id=job_id, attempt=attempt, result=summary.model_dump(),
            model=completion.model, input_tokens=completion.input_tokens,
            output_tokens=completion.output_tokens, input_hash=prompts.input_hash(rendered),
        )
    logger.info("ai job finished", extra={"extra_fields": {
        "job_id": job_id, "attempt": attempt, "landed": landed, "model": completion.model,
        "input_tokens": completion.input_tokens, "output_tokens": completion.output_tokens,
        "request_id": completion.request_id}})


HANDLERS: dict[str, Callable[[str, int, ClaudeJSONClient, int], None]] = {
    "summarize_issue_thread": _summarize,
}


def run_once(client: ClaudeJSONClient, max_attempts: int) -> bool:
    """Claim and process one job. Returns False when there was nothing to do."""
    with db_session.SessionLocal() as db:
        job = ai_jobs.claim_next(db, LEASE_SECONDS)
        if job is None:
            return False
        job_id, attempt, kind = job.id, job.attempts, job.kind

    handler = HANDLERS.get(kind)
    if handler is None:
        with db_session.SessionLocal() as db:
            ai_jobs.fail(db, job_id=job_id, attempt=attempt, retryable=False,
                         message="This kind of AI job is not supported.",
                         max_attempts=max_attempts)
        return True
    try:
        handler(job_id, attempt, client, max_attempts)
    except Exception:
        # Anything unforeseen: log it with the traceback (the redactor scrubs it) and let
        # the job retry, rather than killing the worker and every job behind this one.
        logger.exception("ai job crashed", extra={"extra_fields": {"job_id": job_id}})
        with db_session.SessionLocal() as db:
            ai_jobs.fail(db, job_id=job_id, attempt=attempt, retryable=True,
                         message="Something went wrong while writing the summary.",
                         max_attempts=max_attempts)
    return True


def main() -> int:
    settings = get_settings()
    configure_logging(settings.log_level, settings.log_extra_secrets)
    if not settings.ai_enabled:
        logger.info("AI_ENABLED is false; the AI worker has nothing to do and is exiting")
        return 0

    stop = _Stop()

    def _request_stop(signum, _frame):
        logger.info("stop requested; finishing the current job",
                    extra={"extra_fields": {"signal": signum}})
        stop.requested = True

    signal.signal(signal.SIGTERM, _request_stop)
    signal.signal(signal.SIGINT, _request_stop)

    client = ClaudeJSONClient(model=settings.ai_model, effort=settings.ai_effort)
    logger.info("ai worker started", extra={"extra_fields": {
        "model": settings.ai_model, "effort": settings.ai_effort}})
    while not stop.requested:
        if not run_once(client, settings.ai_max_attempts):
            time.sleep(IDLE_POLL_SECONDS)
    logger.info("ai worker stopped")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
