"""The report worker: large exports, scheduled emails, nightly snapshots. Repeat.

    python -m workers.report_worker

Its own process (the `report-worker` compose service), the same image as the API. The shape
is `workers/ai_worker.py`'s: claim a job in a short transaction, do the work, write the
result fenced on the attempt number, and let SIGTERM finish the job in hand before exiting.

Between jobs, once a minute (`TICK_SECONDS`), it also:

* enqueues every schedule that is due (`services/reports/scheduler.py`);
* writes yesterday's issue snapshot for every workspace whose yesterday — in the workspace's
  own time zone — has none yet (`services/reports/snapshot.py`). Idempotent, so a missed
  night is caught up on the next tick and two workers racing write the same rows;
* drops expired export files, week-old job rows and expired cache rows.

**At-least-once.** A worker killed mid-job leaves it `running` under a lease; the next claim
after the lease lapses retakes it. Rendering is a pure read, so running it twice is
harmless; an email can be sent twice in that one case, which is the trade every
at-least-once queue makes.
"""
from __future__ import annotations

import html
import logging
import os
import signal
import time
from datetime import timedelta

import db.session as db_session
from core.config import get_settings
from core.logging import configure_logging
from integrations.aws_ses import send_email
from models.reporting import ReportJob
from models.user import User
from models.workspace import Workspace
from schemas.email_schema import EmailTemplate
from services.reports import cache, jobs, scheduler, snapshot
from services.reports import settings as report_settings
from services.reports.base import ReportError
from utils import report_math as rm
from utils import time_tracking as tt

logger = logging.getLogger("base_project.report_worker")

#: A 200,000-row export renders in well under a minute; ten is ample and still recovers a
#: crashed worker's job quickly.
LEASE_SECONDS = 10 * 60
IDLE_POLL_SECONDS = 1.0
TICK_SECONDS = 60.0


class _Stop:
    requested = False


def _summary_line(result: dict) -> str:
    meta = result["meta"]
    parts = [meta.get("scope_name") or ""]
    if meta.get("date_from"):
        parts.append(f"{meta['date_from']} - {meta['date_to']}")
    return " - ".join(p for p in parts if p)


def _email(job: ReportJob, result: dict, content: bytes, content_type: str,
           file_name: str) -> int:
    """Send the finished file to each recipient still in the workspace. Returns sends."""
    options = job.options or {}
    title = options.get("schedule_name") or result["meta"].get("title") or "Report"
    summary = _summary_line(result)
    base = (os.getenv("APP_BASE_URL") or "").rstrip("/")
    link = f"{base}/reports/jobs/{job.id}"
    attach = len(content) <= jobs.MAX_ATTACHMENT_BYTES
    body = (f"<p>Your scheduled report <strong>{html.escape(title)}</strong> is ready.</p>"
            f"<p>{html.escape(summary)}</p>")
    if attach:
        body += f"<p>It is attached as <code>{html.escape(file_name)}</code>.</p>"
    else:
        body += ("<p>It is too large to attach. "
                 f"<a href=\"{html.escape(link)}\">Download it here</a> within 24 hours.</p>")
    template = EmailTemplate(subject=f"[PMT] {title} - {summary}"[:200], body=body)
    sent = 0
    with db_session.SessionLocal() as db:
        for user_id in options.get("recipients") or []:
            if jobs.access_for(db, job.workspace_id, user_id) is None:
                continue
            user = db.get(User, user_id)
            if user is None or not user.email:
                continue
            outcome = send_email(user.email, template,
                                 [(file_name, content, content_type)] if attach else None)
            sent += 1 if outcome.get("status") else 0
    return sent


def process(job_id: str, attempt: int) -> None:
    with db_session.SessionLocal() as db:
        job = db.get(ReportJob, job_id)
        if job is None:
            return
        access = jobs.access_for(db, job.workspace_id, job.requested_by)
        if access is None:
            jobs.fail(db, job_id=job_id, attempt=attempt, retryable=False,
                      message="The person who asked for this report is no longer a member.")
            return
        try:
            content, content_type, file_name, rows, result = jobs.render(
                db, access, job.report_type, job.params or {}, job.format, job.options or {})
        except ReportError as exc:
            jobs.fail(db, job_id=job_id, attempt=attempt, retryable=False, message=exc.message)
            return
        db.expunge(job)
    with db_session.SessionLocal() as db:
        landed = jobs.complete(db, job_id=job_id, attempt=attempt, content=content,
                               content_type=content_type, file_name=file_name, row_count=rows)
    sent = _email(job, result, content, content_type, file_name) \
        if landed and job.kind == "schedule" else 0
    logger.info("report job finished", extra={"extra_fields": {
        "job_id": job_id, "attempt": attempt, "landed": landed, "rows": rows,
        "bytes": len(content), "emails": sent}})


def run_once() -> bool:
    """Claim and process one job. Returns False when there was nothing to do."""
    with db_session.SessionLocal() as db:
        job = jobs.claim_next(db, LEASE_SECONDS)
        if job is None:
            return False
        job_id, attempt = job.id, job.attempts
    try:
        process(job_id, attempt)
    except Exception:
        logger.exception("report job crashed", extra={"extra_fields": {"job_id": job_id}})
        with db_session.SessionLocal() as db:
            jobs.fail(db, job_id=job_id, attempt=attempt, retryable=True,
                      message="Something went wrong while generating the report.")
    return True


def snapshot_yesterday() -> int:
    """Write the snapshot of yesterday for every workspace missing one. Returns days written."""
    now = tt.utc_now()
    written = 0
    with db_session.SessionLocal() as db:
        workspaces = db.query(Workspace).filter(Workspace.deleted == 0).all()
        for workspace in workspaces:
            zone_name = report_settings.settings_for(workspace).timezone
            yesterday = rm.local_today(now, rm.zone(zone_name)) - timedelta(days=1)
            if snapshot.has_day(db, workspace.id, yesterday):
                continue
            snapshot.write_day(db, workspace.id, yesterday, zone_name)
            db.commit()
            written += 1
    return written


def tick() -> None:
    """The once-a-minute housekeeping. Each part is independent: one failing does not stop
    the others, and the next tick retries it."""
    for name, step in (("schedules", lambda db: scheduler.run_due(db)),
                       ("cleanup", lambda db: jobs.cleanup(db)),
                       ("cache", lambda db: (cache.purge_expired(db, tt.utc_now()),
                                             db.commit()))):
        try:
            with db_session.SessionLocal() as db:
                step(db)
        except Exception:
            logger.exception("report worker tick failed", extra={"extra_fields": {"step": name}})
    try:
        snapshot_yesterday()
    except Exception:
        logger.exception("report worker tick failed", extra={"extra_fields": {
            "step": "snapshot"}})


def main() -> int:
    settings = get_settings()
    configure_logging(settings.log_level, settings.log_extra_secrets)
    stop = _Stop()

    def _request_stop(signum, _frame):
        logger.info("stop requested; finishing the current job",
                    extra={"extra_fields": {"signal": signum}})
        stop.requested = True

    signal.signal(signal.SIGTERM, _request_stop)
    signal.signal(signal.SIGINT, _request_stop)
    logger.info("report worker started")
    last_tick = 0.0
    while not stop.requested:
        if time.monotonic() - last_tick >= TICK_SECONDS:
            tick()
            last_tick = time.monotonic()
        if not run_once():
            time.sleep(IDLE_POLL_SECONDS)
    logger.info("report worker stopped")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
