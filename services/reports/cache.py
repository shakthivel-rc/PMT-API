"""`report_cache`: a minute of memory for a computed report (docs/design/08-reports.md §9).

The key is a SHA-256 of the workspace, the report type, the normalised parameters, the
viewer (time visibility differs per person) and a **data version** — a cheap fingerprint of
everything a report reads. Any new event, time entry, sprint change or settings change moves
the version, so a cached result is never served after the data under it changed; the TTL
only bounds how long an unchanged result is kept. A closed sprint does not change, so its
reports are kept for a day.

**Workflows are part of the data** since 2026-10-05 (docs/design/09-restructuring.md §9).
Reports reason about stage categories, and history stores stage ids, so re-categorising a
populated stage — Done to Canceled, say — changes every report over its issues, today's and
history's alike, without writing a single event or touching an issue. The version therefore
includes the spaces' `workflow_version`, which every publish increments. A closed sprint's
day-long entry is keyed on it too: its frozen points do not move, but its rows' outcomes do.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta

from fastapi.encoders import jsonable_encoder
from sqlalchemy import func
from sqlalchemy.orm import Session

from models.hierarchy import Space
from models.issue import Issue, IssueEvent
from models.reporting import ReportCache
from models.sprint import Sprint, SprintIssue
from models.time_tracking import TimeEntry
from models.workspace import Workspace, WorkspaceMember

LIVE_TTL = timedelta(seconds=60)
CLOSED_SPRINT_TTL = timedelta(hours=24)


def _workflow_versions(db: Session, workspace_id: str) -> str:
    """The workspace's workflow versions, as one number that moves whenever any of them does.

    A count and a sum rather than a hash of every `(space, version)` pair, because the sum
    cannot repeat: a version only ever increments (a publish is `+1`, 09-restructuring.md
    §4.4) and a space row is never removed — deleting one is a soft delete — so any publish
    anywhere in the workspace strictly raises it, and one aggregate row answers it whatever
    the number of spaces. The count catches a new space, which brings a workflow of its own.
    """
    count, total = db.query(func.count(Space.id), func.sum(Space.workflow_version)).filter(
        Space.workspace_id == workspace_id).one()
    return f"{count}:{int(total or 0)}"


def data_version(db: Session, workspace_id: str) -> str:
    """Counts and latest timestamps of what reports read. Six indexed aggregate queries (the
    sixth, the workflow versions, since 2026-10-05)."""
    parts = []
    for model, stamp in ((IssueEvent, IssueEvent.created_at), (TimeEntry, TimeEntry.updated_at),
                         (Sprint, Sprint.updated_at), (Issue, Issue.updated_at)):
        count, latest = db.query(func.count(), func.max(stamp)).select_from(model).filter(
            model.workspace_id == workspace_id).one()
        parts.append(f"{count}:{latest}")
    parts.append(str(db.query(func.count(SprintIssue.id), func.max(SprintIssue.removed_at))
                     .filter(SprintIssue.workspace_id == workspace_id).one()))
    parts.append(_workflow_versions(db, workspace_id))
    workspace = db.query(Workspace.updated_at, Workspace.settings).filter(
        Workspace.id == workspace_id).one()
    parts.append(str(workspace.updated_at))
    parts.append(json.dumps(workspace.settings or {}, sort_keys=True, default=str))
    parts.append(str(db.query(func.count(WorkspaceMember.user_id)).filter(
        WorkspaceMember.workspace_id == workspace_id).scalar()))
    return hashlib.sha256("|".join(parts).encode()).hexdigest()


def cache_key(workspace_id: str, report_type: str, params: dict, viewer: str,
              version: str, today: str) -> str:
    # `today` is part of the key: "this week" means another range tomorrow.
    raw = json.dumps([workspace_id, report_type, params, viewer, version, today],
                     sort_keys=True, default=str)
    return hashlib.sha256(raw.encode()).hexdigest()


def get(db: Session, key: str, now: datetime) -> dict | None:
    row = db.query(ReportCache).filter(ReportCache.key == key,
                                       ReportCache.expires_at > now).first()
    if row is None:
        return None
    try:
        return json.loads(row.payload_json)
    except ValueError:
        return None


def put(db: Session, key: str, workspace_id: str, report_type: str, params: dict,
        payload: dict, now: datetime, ttl: timedelta) -> None:
    """Store a result. `merge`, so a concurrent writer of the same key updates rather than
    collides; the controller still treats a failed commit here as a miss, not an error."""
    db.merge(ReportCache(key=key, workspace_id=workspace_id, report_type=report_type,
                         params_json=params,
                         payload_json=json.dumps(jsonable_encoder(payload)),
                         computed_at=now, expires_at=now + ttl))
    db.flush()


def purge_expired(db: Session, now: datetime) -> int:
    return db.query(ReportCache).filter(ReportCache.expires_at <= now).delete(
        synchronize_session=False)
