"""The table-backed AI work queue: enqueue, claim, finish. See `models/ai_job.py` for why a
table.

**Fencing.** A worker that is slower than its lease can have its job re-claimed by another
worker and then finish anyway. Every finishing write therefore names the `attempts` value
it claimed with and only lands if that is still the row's value, so the late worker's write
matches nothing and is discarded. Without it two workers can both "succeed" and the slower
one silently overwrites the faster one's answer.
"""
from datetime import UTC, datetime, timedelta

from sqlalchemy import and_, or_, update
from sqlalchemy.orm import Session

from models.ai_job import AIJob

#: Back-off before retry N (1-based), in seconds. Short first, because most transient
#: failures are a rate limit that clears within the minute; capped, because a user is
#: watching a spinner.
RETRY_BACKOFF_SECONDS = (30, 120, 300)

TERMINAL = frozenset({"succeeded", "failed"})


def _now() -> datetime:
    return datetime.now(UTC)


def find_reusable(db: Session, workspace_id: str, subject_id: str, kind: str,
                  input_hash: str) -> AIJob | None:
    """A succeeded job for exactly this input, newest first."""
    return (
        db.query(AIJob)
        .filter(AIJob.workspace_id == workspace_id, AIJob.subject_id == subject_id,
                AIJob.kind == kind, AIJob.status == "succeeded",
                AIJob.input_hash == input_hash)
        .order_by(AIJob.finished_at.desc())
        .first()
    )


def find_in_flight(db: Session, workspace_id: str, subject_id: str, kind: str) -> AIJob | None:
    """A queued or running job for the same subject — asking twice should not pay twice."""
    return (
        db.query(AIJob)
        .filter(AIJob.workspace_id == workspace_id, AIJob.subject_id == subject_id,
                AIJob.kind == kind, AIJob.status.in_(("queued", "running")))
        .order_by(AIJob.created_at.desc())
        .first()
    )


def get_job(db: Session, workspace_id: str, job_id: str) -> AIJob | None:
    return (
        db.query(AIJob)
        .filter(AIJob.workspace_id == workspace_id, AIJob.id == job_id)
        .first()
    )


def enqueue(db: Session, *, workspace_id: str, kind: str, subject_id: str, requested_by: str,
            input_hash: str, prompt_version: str) -> AIJob:
    job = AIJob(workspace_id=workspace_id, kind=kind, subject_type="issue",
                subject_id=subject_id, status="queued", requested_by=requested_by,
                input_hash=input_hash, prompt_version=prompt_version, attempts=0,
                available_at=_now())
    db.add(job)
    db.flush()
    return job


def claim_next(db: Session, lease_seconds: int, now: datetime | None = None) -> AIJob | None:
    """Take the oldest runnable job, or return None. Commits.

    Runnable means queued and due, or running with a lapsed lease (its worker died).
    `FOR UPDATE SKIP LOCKED` makes concurrent claimers step over each other's rows instead of
    queueing behind them; SQLite has no row locks and ignores it, which is correct for a
    single-process test.
    """
    now = now or _now()
    job = (
        db.query(AIJob)
        .filter(or_(
            and_(AIJob.status == "queued", AIJob.available_at <= now),
            and_(AIJob.status == "running", AIJob.lease_expires_at < now),
        ))
        .order_by(AIJob.available_at, AIJob.created_at)
        .with_for_update(skip_locked=True)
        .first()
    )
    if job is None:
        db.rollback()
        return None
    job.status = "running"
    job.attempts = (job.attempts or 0) + 1
    job.started_at = now
    job.lease_expires_at = now + timedelta(seconds=lease_seconds)
    db.commit()
    return job


def complete(db: Session, *, job_id: str, attempt: int, result: dict, model: str,
             input_tokens: int, output_tokens: int, input_hash: str) -> bool:
    """Record success if this worker still holds the job. Returns whether it did."""
    outcome = db.execute(
        update(AIJob)
        .where(AIJob.id == job_id, AIJob.status == "running", AIJob.attempts == attempt)
        .values(status="succeeded", result=result, model=model, input_tokens=input_tokens,
                output_tokens=output_tokens, input_hash=input_hash, error=None,
                finished_at=_now(), lease_expires_at=None)
    )
    db.commit()
    return outcome.rowcount == 1


def fail(db: Session, *, job_id: str, attempt: int, message: str, retryable: bool,
         max_attempts: int) -> str | None:
    """Put the job back with a back-off, or mark it failed. Returns the new status, or None
    if this worker no longer held it."""
    now = _now()
    if retryable and attempt < max_attempts:
        delay = RETRY_BACKOFF_SECONDS[min(attempt, len(RETRY_BACKOFF_SECONDS)) - 1]
        values = {"status": "queued", "error": message, "lease_expires_at": None,
                  "available_at": now + timedelta(seconds=delay)}
    else:
        values = {"status": "failed", "error": message, "lease_expires_at": None,
                  "finished_at": now}
    outcome = db.execute(
        update(AIJob)
        .where(AIJob.id == job_id, AIJob.status == "running", AIJob.attempts == attempt)
        .values(**values)
    )
    db.commit()
    return str(values["status"]) if outcome.rowcount == 1 else None


def job_payload(job: AIJob, cached: bool = False) -> dict:
    """`AIJobSummary`. An error is only shown once the job has given up: a retry in progress
    reports `queued`, and the message from the attempt that failed would read as final."""
    return {
        "id": job.id,
        "kind": job.kind,
        "subject_id": job.subject_id,
        "status": job.status,
        "result": job.result if job.status == "succeeded" else None,
        "error": job.error if job.status == "failed" else None,
        "model": job.model,
        "cached": cached,
        "created_at": job.created_at,
        "finished_at": job.finished_at,
    }
