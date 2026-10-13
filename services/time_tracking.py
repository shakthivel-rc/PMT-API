"""Time-tracking operations: logging, timers, totals and roll-ups, and who may see what.

The rules of `services/issue.py` hold here too — every query carries `workspace_id`, and
every change to an issue's time writes its `issue_events` row in the same transaction. On
top of those:

* **`log_time` is the one place an entry is created**, for a person typing it in, a timer
  being stopped and (later) an automation rule alike, the way `set_status` is the one place a
  status changes. A writer that bypasses it is a writer whose time the reports never see.
* **Nothing here writes the `issues` row.** Totals are summed on read (models/time_tracking.py
  says why), so logging time can never bump `issues.version` under someone editing the issue.
* **Visibility is decided here, once.** Under a workspace's `own` setting a member sees only
  their own entries; `visible_user_id` is the single function every read asks.
"""
from collections.abc import Iterable
from datetime import date, datetime

from sqlalchemy import func
from sqlalchemy.orm import Session

from dependencies.workspace import WorkspaceAccess
from models.hierarchy import Space
from models.issue import Issue
from models.time_tracking import ActiveTimer, TimeEntry
from models.user import User
from models.workspace import WORKSPACE_ROLES, Workspace, WorkspaceMember
from schemas.time_tracking import TimeTrackingSettings
from services import issue as issue_svc
from utils import time_tracking as tt
from utils.issue_keys import format_issue_key

#: The key under `workspaces.settings` that holds this feature's document.
SETTINGS_KEY = "time_tracking"

#: How many entries the issue drawer receives in one read. An issue with more than this is
#: rare (Jira caps an issue at 10,000 worklogs and searches only the latest 1,000); the
#: drawer shows the newest and the count, and the full history is a paged
#: `GET …/time-entries?issue_id=` away.
ISSUE_LOG_LIMIT = 200

#: The deepest a roll-up walks. The type rules bound the hierarchy at five levels
#: (models/issue.py); this is the same guard `services/issue.is_ancestor` keeps against
#: rows written before those rules existed.
_MAX_DEPTH = 16


# ---- settings and permissions ---------------------------------------------------------------


def settings_for(workspace: Workspace | None) -> TimeTrackingSettings:
    """The workspace's settings, with defaults for anything never saved.

    A stored document that no longer validates — written by an older release, or by hand —
    falls back to the defaults rather than failing every time-tracking read in the tenant.
    """
    document = (workspace.settings or {}) if workspace is not None else {}
    stored = document.get(SETTINGS_KEY) if isinstance(document, dict) else None
    try:
        return TimeTrackingSettings.model_validate(stored or {})
    except ValueError:
        return TimeTrackingSettings()


def load_settings(db: Session, workspace_id: str) -> TimeTrackingSettings:
    return settings_for(db.query(Workspace).filter(Workspace.id == workspace_id).first())


def save_settings(workspace: Workspace, settings: TimeTrackingSettings) -> None:
    """Store `settings`, keeping every other feature's key in the document.

    A new dict is assigned rather than the old one mutated: a JSON column only notices
    reassignment, so an in-place change would be silently dropped at commit.
    """
    document = dict(workspace.settings or {}) if isinstance(workspace.settings, dict) else {}
    document[SETTINGS_KEY] = settings.model_dump()
    workspace.settings = document


def can_log(access: WorkspaceAccess, settings: TimeTrackingSettings) -> bool:
    """Whether the caller may log their own time: a member or above, with tracking on."""
    return settings.enabled and access.at_least("member")


def can_log_for_others(access: WorkspaceAccess, settings: TimeTrackingSettings) -> bool:
    """Logging on someone's behalf is an admin capability of its own (YouTrack's "Create Not
    Own Work Item", Tempo's "Log Work for Others"), not a side effect of editing rights."""
    return settings.enabled and access.at_least("admin")


def can_edit(access: WorkspaceAccess, entry_user_id: str) -> bool:
    """Your own entry while you can log time at all; anyone's if you are an admin."""
    if access.at_least("admin"):
        return True
    return entry_user_id == access.user_id and access.at_least("member")


def visible_user_id(access: WorkspaceAccess, settings: TimeTrackingSettings) -> str | None:
    """The only person whose entries the caller may see, or None for everyone.

    Under `own` visibility a member's view of other people's time is the issue totals and
    nothing more — the setting exists because "every member sees everyone's worklogs" is
    Jira's most persistent complaint, and works councils have a say over tools that let
    colleagues watch each other's hours (docs/design/07-time-tracking.md).
    """
    if settings.visibility == "everyone" or access.at_least("admin"):
        return None
    return access.user_id


def member_role(db: Session, workspace_id: str, user_id: str) -> str | None:
    """The person's role in a live workspace, or None if they are not in it."""
    role = (
        db.query(WorkspaceMember.role)
        .join(Workspace, Workspace.id == WorkspaceMember.workspace_id)
        .filter(WorkspaceMember.workspace_id == workspace_id,
                WorkspaceMember.user_id == user_id, Workspace.deleted == 0)
        .scalar()
    )
    return str(role) if role is not None else None


def can_hold_time(db: Session, workspace_id: str, user_id: str) -> bool:
    """Whether time may be logged *for* this person here: a live account that is a member
    with a role that logs time. `services/workspace.is_member` alone would also accept a
    soft-deleted account, which can no longer stand behind its hours."""
    role = member_role(db, workspace_id, user_id)
    if role is None or WORKSPACE_ROLES.index(role) < WORKSPACE_ROLES.index("member"):
        return False
    return db.query(User.id).filter(User.id == user_id, User.deleted == 0).first() is not None


# ---- lookups and payloads ---------------------------------------------------------------------


def get_entry(db: Session, workspace_id: str, entry_id: str) -> TimeEntry | None:
    """A live entry on a live issue in this workspace. Time on a deleted issue goes with it."""
    return (
        db.query(TimeEntry)
        .join(Issue, Issue.id == TimeEntry.issue_id)
        .filter(TimeEntry.workspace_id == workspace_id, TimeEntry.id == entry_id,
                TimeEntry.deleted == 0, Issue.deleted == 0)
        .first()
    )


def issue_refs(db: Session, issue_ids: Iterable[str]) -> dict[str, dict]:
    """`TimeIssueRef` payloads by issue id. One query, whatever the number of issues."""
    ids = set(issue_ids)
    if not ids:
        return {}
    rows = (
        db.query(Issue.id, Issue.workspace_id, Issue.list_id, Issue.number, Issue.title,
                 Space.key)
        .join(Space, Space.id == Issue.space_id)
        .filter(Issue.id.in_(ids))
        .all()
    )
    return {
        issue_id: {"id": issue_id, "workspace_id": workspace_id, "list_id": list_id,
                   "key": format_issue_key(key, number), "title": title}
        for issue_id, workspace_id, list_id, number, title, key in rows
    }


def _ref(users: dict[str, User], user_id: str) -> dict:
    return issue_svc.user_ref(users.get(user_id)) or {"id": user_id, "display_name": ""}


def entry_payloads(db: Session, access: WorkspaceAccess, rows: list[TimeEntry]) -> list[dict]:
    """`TimeEntrySummary` payloads, in the order given. Two queries, whatever N is."""
    users = issue_svc.users_by_id(db, [r.user_id for r in rows] + [r.created_by for r in rows])
    issues = issue_refs(db, (row.issue_id for row in rows))
    return [
        {
            "id": row.id,
            "issue": issues[row.issue_id],
            "user": _ref(users, row.user_id),
            "created_by": _ref(users, row.created_by),
            "work_date": row.work_date,
            "started_at": row.started_at,
            "duration_seconds": row.duration_seconds,
            "note": row.note,
            "source": row.source,
            "billable": bool(row.billable),
            "created_at": row.created_at,
            "updated_at": row.updated_at,
            "can_edit": can_edit(access, row.user_id),
        }
        for row in rows
    ]


def entries_query(db: Session, workspace_id: str):
    """Live entries on live issues in this workspace — the base of every list and total."""
    return (
        db.query(TimeEntry)
        .join(Issue, Issue.id == TimeEntry.issue_id)
        .filter(TimeEntry.workspace_id == workspace_id, TimeEntry.deleted == 0,
                Issue.deleted == 0)
    )


def newest_first(query):
    """The work-log order: latest day first, then latest start, then latest typed. `id` last,
    because MySQL stores whole seconds and two entries can tie on everything else."""
    return query.order_by(TimeEntry.work_date.desc(), TimeEntry.started_at.desc(),
                          TimeEntry.created_at.desc(), TimeEntry.id)


# ---- totals and roll-ups ---------------------------------------------------------------------


def descendant_ids(db: Session, workspace_id: str, issue_id: str) -> list[str]:
    """Every live issue beneath `issue_id`, at any depth. One query per level, at most five
    levels by the type rules — the same bounded walk `services/issue.is_ancestor` does, in
    the other direction. Each level is tenant-filtered, so a corrupted `parent_id` pointing
    across tenants cannot pull someone else's time into a roll-up."""
    found: list[str] = []
    seen = {issue_id}
    frontier = [issue_id]
    for _ in range(_MAX_DEPTH):
        if not frontier:
            break
        children = [
            child_id for (child_id,) in
            db.query(Issue.id)
            .filter(Issue.workspace_id == workspace_id, Issue.parent_id.in_(frontier),
                    Issue.deleted == 0)
            .all()
            if child_id not in seen
        ]
        seen.update(children)
        found.extend(children)
        frontier = children
    return found


def issue_summary(db: Session, access: WorkspaceAccess, issue: Issue,
                  settings: TimeTrackingSettings) -> dict:
    """`IssueTimeSummary`: own and rolled-up time and estimates, and who logged what."""
    workspace_id = issue.workspace_id
    descendants = descendant_ids(db, workspace_id, issue.id)
    subtree = [issue.id, *descendants]

    spent_by_issue = issue_svc.time_spent_by_issue(db, workspace_id, subtree)
    own_spent = spent_by_issue.get(issue.id, 0)
    estimates = [issue.time_estimate_seconds]
    if descendants:
        estimates += [
            value for (value,) in
            db.query(Issue.time_estimate_seconds)
            .filter(Issue.workspace_id == workspace_id, Issue.id.in_(descendants))
            .all()
        ]

    per_user = (
        db.query(TimeEntry.user_id, func.sum(TimeEntry.duration_seconds),
                 func.count(TimeEntry.id))
        .filter(TimeEntry.workspace_id == workspace_id, TimeEntry.issue_id == issue.id,
                TimeEntry.deleted == 0)
        .group_by(TimeEntry.user_id)
        .all()
    )
    # MySQL's SUM over an INTEGER is a DECIMAL; SQLite's is an int. `int()` makes both the
    # same before anything compares or serialises them.
    totals = [(user_id, int(seconds or 0), int(count or 0)) for user_id, seconds, count in per_user]
    only = visible_user_id(access, settings)
    shown = sorted((t for t in totals if only is None or t[0] == only),
                   key=lambda t: (-t[1], t[0]))
    users = issue_svc.users_by_id(db, (t[0] for t in shown))
    return {
        "spent_seconds": own_spent,
        "rollup_spent_seconds": sum(spent_by_issue.values()),
        "estimate_seconds": issue.time_estimate_seconds,
        "rollup_estimate_seconds": tt.sum_present(estimates),
        "remaining_seconds": tt.remaining_seconds(issue.time_estimate_seconds, own_spent),
        "entry_count": sum(t[2] for t in totals),
        "by_user": [{"user": _ref(users, user_id), "seconds": seconds}
                    for user_id, seconds, _count in shown],
    }


# ---- writes ---------------------------------------------------------------------------------


def log_time(db: Session, issue: Issue, *, user_id: str, created_by: str, work_date: date,
             started_at: datetime | None, duration_seconds: int, note: str | None,
             source: str = "manual", actor_kind: str = "user",
             billable: bool | None = None) -> TimeEntry:
    """Create an entry and its `time_logged` event. The caller commits.

    `billable=None` takes the workspace's `billable_default`, so a timer and an automation
    file time the same way a person typing it in without choosing would.
    """
    if billable is None:
        billable = load_settings(db, issue.workspace_id).billable_default
    entry = TimeEntry(
        workspace_id=issue.workspace_id, issue_id=issue.id, user_id=user_id,
        created_by=created_by, work_date=work_date, started_at=tt.to_naive_utc(started_at),
        duration_seconds=duration_seconds, note=clean_note(note), source=source,
        billable=billable,
    )
    db.add(entry)
    db.flush()
    issue_svc.record_event(db, issue, "time_logged", created_by, actor_kind,
                           to_value=str(duration_seconds))
    return entry


def clean_note(note: str | None) -> str | None:
    """A note with its surrounding whitespace gone; an empty one is no note."""
    if note is None:
        return None
    stripped = note.strip()
    return stripped or None


# ---- timers ---------------------------------------------------------------------------------


def active_timer(db: Session, user_id: str) -> ActiveTimer | None:
    return db.query(ActiveTimer).filter(ActiveTimer.user_id == user_id).first()


def timer_payload(db: Session, timer: ActiveTimer, now: datetime) -> dict:
    return {
        "issue": issue_refs(db, [timer.issue_id])[timer.issue_id],
        "started_at": timer.started_at,
        "elapsed_seconds": tt.elapsed_seconds(timer.started_at, now),
        "work_date": timer.work_date,
        "note": timer.note,
    }


def finish_timer(db: Session, timer: ActiveTimer, now: datetime,
                 note: str | None = None) -> tuple[TimeEntry | None, bool]:
    """Turn a running timer into an entry and delete it. The caller commits.

    Returns the entry and whether its duration was cut to the one-day ceiling. When the
    timer's issue has been deleted, or its owner can no longer log time in that workspace,
    there is nowhere honest to put the time: the timer is discarded and the entry is None.
    """
    issue = issue_svc.get_issue(db, timer.workspace_id, timer.issue_id)
    entry: TimeEntry | None = None
    capped = False
    if issue is not None and can_hold_time(db, timer.workspace_id, timer.user_id):
        seconds, capped = tt.timer_entry_seconds(tt.elapsed_seconds(timer.started_at, now))
        entry = log_time(
            db, issue, user_id=timer.user_id, created_by=timer.user_id,
            work_date=timer.work_date, started_at=timer.started_at,
            duration_seconds=seconds, note=note if note is not None else timer.note,
            source="timer",
        )
    db.delete(timer)
    db.flush()
    return entry, capped
