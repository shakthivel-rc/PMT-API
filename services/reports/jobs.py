"""Report jobs: the table-backed queue behind large exports and scheduled emails.

The pattern is `services/ai_jobs.py`'s, and so are its two guarantees: a claim takes the
oldest runnable row with `FOR UPDATE SKIP LOCKED` and a lease, and every finishing write is
fenced on the attempt number it claimed with, so a worker that outlived its lease cannot
overwrite the work of the one that took the job over.

The file is kept in the row (`file_blob`): the container writes nothing to disk
(docker-compose.yml), and a file shared between API replicas and workers needs shared
storage either way. It is dropped when the job expires, a day after it finished.
"""
from __future__ import annotations

import hashlib
import hmac
import logging
from datetime import datetime, timedelta

from sqlalchemy import and_, or_, update
from sqlalchemy.orm import Session

from core.config import get_settings
from dependencies.workspace import WorkspaceAccess
from models.reporting import ReportJob
from schemas.reports import ReportParams
from services import time_tracking as tt_svc
from services.reports import engine
from services.reports.exporters import export
from utils import time_tracking as tt

logger = logging.getLogger("base_project.reports")

#: Back-off before retry N, as in ai_jobs. Report failures are mostly a database blip.
RETRY_BACKOFF_SECONDS = (30, 120, 300)
MAX_ATTEMPTS = 3
#: How long a finished file and its download link live (docs/design/08-reports.md §6).
FILE_TTL = timedelta(hours=24)
#: Finished job rows are kept a week for the job list, then deleted.
ROW_TTL = timedelta(days=7)
#: Exports up to this many rows are answered at once; bigger ones become a job.
SYNC_EXPORT_ROWS = 5_000
#: Above this, an emailed report links to the file instead of attaching it.
MAX_ATTACHMENT_BYTES = 5 * 1024 * 1024


def total_rows(result: dict) -> int:
    return len(result["rows"]) + sum(len(s["rows"]) for s in result.get("sections") or [])


def options_of(body) -> dict:
    return {"date_format": body.date_format, "duration_format": body.duration_format}


# ---- queue -------------------------------------------------------------------------------


def enqueue(db: Session, *, workspace_id: str, requested_by: str, kind: str, report_type: str,
            params: dict, fmt: str, options: dict, schedule_id: str | None = None) -> ReportJob:
    now = tt.utc_now()
    job = ReportJob(workspace_id=workspace_id, requested_by=requested_by, kind=kind,
                    report_type=report_type, params=params, options=options, format=fmt,
                    schedule_id=schedule_id, status="queued", attempts=0, available_at=now,
                    created_at=now)
    db.add(job)
    db.flush()
    return job


def get_job(db: Session, workspace_id: str, job_id: str) -> ReportJob | None:
    return db.query(ReportJob).filter(ReportJob.workspace_id == workspace_id,
                                      ReportJob.id == job_id).first()


def can_read(job: ReportJob, user_id: str) -> bool:
    """The person who asked for it, or — for a scheduled report — one of its recipients."""
    if job.requested_by == user_id:
        return True
    recipients = (job.options or {}).get("recipients") or []
    return job.kind == "schedule" and user_id in recipients


def claim_next(db: Session, lease_seconds: int, now: datetime | None = None) -> ReportJob | None:
    now = now or tt.utc_now()
    job = (
        db.query(ReportJob)
        .filter(or_(
            and_(ReportJob.status == "queued", ReportJob.available_at <= now),
            and_(ReportJob.status == "running", ReportJob.lease_expires_at < now),
        ))
        .order_by(ReportJob.available_at, ReportJob.created_at)
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


def complete(db: Session, *, job_id: str, attempt: int, content: bytes, content_type: str,
             file_name: str, row_count: int) -> bool:
    now = tt.utc_now()
    outcome = db.execute(
        update(ReportJob)
        .where(ReportJob.id == job_id, ReportJob.status == "running",
               ReportJob.attempts == attempt)
        .values(status="done", file_blob=content, content_type=content_type,
                file_name=file_name, size_bytes=len(content), row_count=row_count, error=None,
                finished_at=now, expires_at=now + FILE_TTL, lease_expires_at=None)
    )
    db.commit()
    return outcome.rowcount == 1


def fail(db: Session, *, job_id: str, attempt: int, message: str, retryable: bool) -> str | None:
    now = tt.utc_now()
    if retryable and attempt < MAX_ATTEMPTS:
        delay = RETRY_BACKOFF_SECONDS[min(attempt, len(RETRY_BACKOFF_SECONDS)) - 1]
        values = {"status": "queued", "error": message, "lease_expires_at": None,
                  "available_at": now + timedelta(seconds=delay)}
    else:
        values = {"status": "failed", "error": message, "lease_expires_at": None,
                  "finished_at": now}
    outcome = db.execute(
        update(ReportJob)
        .where(ReportJob.id == job_id, ReportJob.status == "running",
               ReportJob.attempts == attempt)
        .values(**values)
    )
    db.commit()
    return str(values["status"]) if outcome.rowcount == 1 else None


def cleanup(db: Session, now: datetime | None = None) -> tuple[int, int]:
    """Drop expired files, then delete week-old finished rows. Returns (files, rows)."""
    now = now or tt.utc_now()
    files = db.query(ReportJob).filter(ReportJob.expires_at <= now,
                                       ReportJob.file_blob.isnot(None)).update(
        {ReportJob.file_blob: None}, synchronize_session=False)
    rows = db.query(ReportJob).filter(ReportJob.status.in_(("done", "failed")),
                                      ReportJob.finished_at <= now - ROW_TTL).delete(
        synchronize_session=False)
    db.commit()
    return files, rows


# ---- signed download links -----------------------------------------------------------------


def _signature(job_id: str, user_id: str, expires: int) -> str:
    key = get_settings().secret_key.encode()
    message = f"report-download:{job_id}:{user_id}:{expires}".encode()
    return hmac.new(key, message, hashlib.sha256).hexdigest()


def download_token(job_id: str, user_id: str, now: datetime | None = None) -> str:
    """A link token tied to one job and one person, valid for `FILE_TTL`."""
    expires = int(((now or tt.utc_now()) + FILE_TTL).timestamp())
    return f"{expires}.{_signature(job_id, user_id, expires)}"


def token_valid(token: str | None, job_id: str, user_id: str,
                now: datetime | None = None) -> bool:
    if not token or "." not in token:
        return False
    raw_expires, signature = token.split(".", 1)
    try:
        expires = int(raw_expires)
    except ValueError:
        return False
    if expires < int((now or tt.utc_now()).timestamp()):
        return False
    return hmac.compare_digest(signature, _signature(job_id, user_id, expires))


def payload(job: ReportJob, viewer_id: str) -> dict:
    """`ReportJobSummary`. The download path carries a fresh token for the viewer."""
    available = job.status == "done" and job.file_blob is not None
    return {
        "id": job.id, "kind": job.kind, "report_type": job.report_type, "format": job.format,
        "status": job.status, "error": job.error if job.status == "failed" else None,
        "file_name": job.file_name, "size_bytes": job.size_bytes, "row_count": job.row_count,
        "params": job.params or {}, "created_at": job.created_at,
        "finished_at": job.finished_at, "expires_at": job.expires_at,
        "download_path": (f"/workspaces/{job.workspace_id}/report-jobs/{job.id}/download"
                          f"?token={download_token(job.id, viewer_id)}") if available else None,
    }


# ---- running a job -----------------------------------------------------------------------


def access_for(db: Session, workspace_id: str, user_id: str) -> WorkspaceAccess | None:
    """The person's standing in the workspace now — not when they asked. Someone who left
    since gets nothing."""
    role = tt_svc.member_role(db, workspace_id, user_id)
    return WorkspaceAccess(workspace_id, user_id, role) if role else None


def render(db: Session, access: WorkspaceAccess, report_type: str, params: dict, fmt: str,
           options: dict) -> tuple[bytes, str, str, int, dict]:
    """Compute and render one report: `(content, content_type, file_name, rows, result)`."""
    result = engine.compute(db, access, report_type, ReportParams.model_validate(params or {}),
                            max_rows=engine.MAX_EXPORT_ROWS)
    content, content_type, file_name = export(result, fmt, options)
    return content, content_type, file_name, total_rows(result), result
