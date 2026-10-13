"""The AI work queue: one row per model call a user asked for.

**Why a table and not a broker.** A request that wants a summary inserts a row in the same
transaction as nothing else and returns 202; a worker process claims it. The table *is* the
queue in Phase 1, for three reasons that outweigh its throughput ceiling:

* it adds no infrastructure — the base runs its whole test suite with zero services, and a
  queue that needs Redis to exist would end that;
* it is transactional with the data it describes — there is no window in which a job exists
  for an issue that rolled back;
* it is its own audit log: who asked for what, which model and prompt version answered, how
  many tokens it cost, and how long it waited.

Workers claim with `SELECT … FOR UPDATE SKIP LOCKED` and a lease (`lease_expires_at`), so N
replicas never take the same row and a row held by a killed replica is retaken when its
lease lapses. At-least-once, therefore, and the handlers are written to be re-runnable.
The broker (Redis Streams, then Kafka for the event bus) arrives when the measured claim
rate says it must — docs/design/03-ai-strategy.md gives the number.
"""
import uuid
from datetime import UTC, datetime

from sqlalchemy import CHAR, JSON, Column, DateTime, ForeignKey, Index, Integer, String, Text

from db.session import Base

AI_JOB_KINDS: tuple[str, ...] = ("summarize_issue_thread",)
AI_JOB_STATUSES: tuple[str, ...] = ("queued", "running", "succeeded", "failed")


def _now() -> datetime:
    return datetime.now(UTC)


class AIJob(Base):
    __tablename__ = "ai_jobs"
    __table_args__ = (
        # The claim query: oldest runnable job first.
        Index("ix_ai_jobs_status_available", "status", "available_at"),
        # The dedupe/cache lookup: "is there already an answer for exactly this input?"
        Index("ix_ai_jobs_subject", "workspace_id", "subject_id", "kind"),
    )

    id = Column(CHAR(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    workspace_id = Column(CHAR(36), ForeignKey("workspaces.id"), nullable=False)
    kind = Column(String(40), nullable=False)
    subject_type = Column(String(20), nullable=False, default="issue")
    subject_id = Column(CHAR(36), nullable=False)
    status = Column(String(20), nullable=False, default="queued")
    # SHA-256 of the exact text sent (or to be sent) to the model, plus the prompt version.
    # A new request whose hash matches a succeeded job is answered from that job: the
    # thread has not changed, so neither would the summary, and the call is not free.
    input_hash = Column(String(64), nullable=True)
    prompt_version = Column(String(40), nullable=True)
    model = Column(String(60), nullable=True)
    result = Column(JSON, nullable=True)
    # Shown to the person who asked. Never a stack trace, a key or a raw provider body.
    error = Column(Text, nullable=True)
    attempts = Column(Integer, nullable=False, default=0)
    input_tokens = Column(Integer, nullable=True)
    output_tokens = Column(Integer, nullable=True)
    requested_by = Column(CHAR(36), ForeignKey("users.id"), nullable=False)
    # Not before this time — how a retry backs off without a worker sleeping on it.
    available_at = Column(DateTime, default=_now, nullable=False)
    lease_expires_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=_now, nullable=False)
    started_at = Column(DateTime, nullable=True)
    finished_at = Column(DateTime, nullable=True)
