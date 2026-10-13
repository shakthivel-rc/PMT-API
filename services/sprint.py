"""Sprint operations: planning, starting, closing, and the membership history.

The rules of `services/issue.py` hold here, plus one of this module's own:

* **This is the only writer of `issues.sprint_id` and `sprint_issues`**, and it writes both
  in the same flush together with a `sprint_added` / `sprint_removed` event. The sprint
  report's "added after start" and "removed" sections and the burndown's scope changes are
  all read from `sprint_issues`; a writer that set `sprint_id` alone would be invisible to
  them.
* **Membership changes do not bump `issues.version`.** Dragging an issue into a sprint is
  planning, not an edit of the issue; someone with the issue open must not get a 409 for it.
  The board shows a sprint chip from `IssueCard.sprint_id`, re-read on refresh.
* **One active sprint per project**, enforced under a lock on the space row
  (`SELECT … FOR UPDATE`), so two people pressing Start at once cannot both win. A partial
  unique index would say it in the schema, but MySQL 8 has none.
* **Three outcomes, by category, never by stage name** (docs/design/09-restructuring.md §9):
  an issue in a `done` stage is completed — it counts and stays in the sprint; one in a
  `canceled` stage is closed but not delivered — it stays, so the sprint keeps the record
  of what it decided not to do, but it never counts as completed and is never carried
  over; anything else is unfinished and leaves at the close. `split_by_outcome` is that
  rule, once, for the close and for the controller that guards what a close would carry.
"""
from collections.abc import Iterable
from datetime import date, datetime, timedelta
from typing import NamedTuple

from sqlalchemy import func
from sqlalchemy.orm import Session

from models.hierarchy import Space, WorkflowStatus
from models.issue import Issue
from models.sprint import Sprint, SprintIssue
from services import issue as issue_svc
from utils import workflow_rules as rules

#: A sprint started without an end date ends two weeks later — Jira's default length.
DEFAULT_LENGTH = timedelta(days=13)

_STATE_ORDER = {"active": 0, "planned": 1, "closed": 2}

#: The category of work closed without being delivered (utils/workflow_rules.py).
_CANCELED = "canceled"


class _Counts(NamedTuple):
    """What is in a sprint now, by outcome."""

    issues: int = 0
    points: int = 0
    done: int = 0
    done_points: int = 0
    canceled: int = 0


def get_sprint(db: Session, workspace_id: str, sprint_id: str) -> Sprint | None:
    return (
        db.query(Sprint)
        .filter(Sprint.workspace_id == workspace_id, Sprint.id == sprint_id, Sprint.deleted == 0)
        .first()
    )


def space_sprints(db: Session, workspace_id: str, space_id: str,
                  states: Iterable[str] | None = None) -> list[Sprint]:
    """The project's sprints: the active one, then planned in creation order, then closed,
    newest first — the order of Jira's backlog view."""
    query = db.query(Sprint).filter(Sprint.workspace_id == workspace_id,
                                    Sprint.space_id == space_id, Sprint.deleted == 0)
    wanted = list(states or [])
    if wanted:
        query = query.filter(Sprint.state.in_(wanted))
    rows = query.all()
    return sorted(rows, key=lambda s: (
        _STATE_ORDER.get(s.state, 3),
        -(_naive(s.closed_at).timestamp() if s.state == "closed" and s.closed_at else 0),
        _naive(s.created_at),
    ))


def _naive(value: datetime) -> datetime:
    # A row read back from the database is naive; one created in this session still holds
    # the aware default it was given. Sorting the two together would raise.
    return value.replace(tzinfo=None)


def active_sprint(db: Session, workspace_id: str, space_id: str) -> Sprint | None:
    return (
        db.query(Sprint)
        .filter(Sprint.workspace_id == workspace_id, Sprint.space_id == space_id,
                Sprint.state == "active", Sprint.deleted == 0)
        .first()
    )


def sprint_issues(db: Session, sprint: Sprint) -> list[Issue]:
    """The live issues in the sprint now."""
    return (
        db.query(Issue)
        .filter(Issue.workspace_id == sprint.workspace_id, Issue.sprint_id == sprint.id,
                Issue.deleted == 0)
        .order_by(Issue.rank)
        .all()
    )


def _status_ids(db: Session, workspace_id: str, space_id: str,
                categories: Iterable[str]) -> set[str]:
    """The space's stages in `categories`, removed ones included: an issue can still sit in
    a stage removed since, until a publish moves it (09-restructuring.md §4.4)."""
    rows = db.query(WorkflowStatus.id).filter(
        WorkflowStatus.workspace_id == workspace_id, WorkflowStatus.space_id == space_id,
        WorkflowStatus.category.in_(list(categories))).all()
    return {status_id for (status_id,) in rows}


def done_status_ids(db: Session, workspace_id: str, space_id: str) -> set[str]:
    return _status_ids(db, workspace_id, space_id, [rules.COMPLETED_CATEGORY])


def canceled_status_ids(db: Session, workspace_id: str, space_id: str) -> set[str]:
    return _status_ids(db, workspace_id, space_id, [_CANCELED])


def _counts(db: Session, sprints: list[Sprint]) -> dict[str, _Counts]:
    """Per sprint: issues, points, done issues, done points, canceled issues — two queries,
    whatever the number of sprints. Done is the `done` category only: a canceled issue is
    closed but was not delivered, so it is counted on its own and never as done."""
    ids = [s.id for s in sprints]
    if not ids:
        return {}
    categories = dict(
        db.query(WorkflowStatus.id, WorkflowStatus.category)
        .filter(WorkflowStatus.workspace_id == sprints[0].workspace_id,
                WorkflowStatus.category.in_([rules.COMPLETED_CATEGORY, _CANCELED]))
        .all()
    )
    rows = (
        db.query(Issue.sprint_id, Issue.status_id, func.count(Issue.id),
                 func.coalesce(func.sum(Issue.story_points), 0))
        .filter(Issue.workspace_id == sprints[0].workspace_id, Issue.sprint_id.in_(ids),
                Issue.deleted == 0)
        .group_by(Issue.sprint_id, Issue.status_id)
        .all()
    )
    totals: dict[str, list[int]] = {sprint_id: [0, 0, 0, 0, 0] for sprint_id in ids}
    for sprint_id, status_id, count, points in rows:
        entry = totals[sprint_id]
        entry[0] += int(count)
        entry[1] += int(points or 0)
        category = categories.get(status_id)
        if category == rules.COMPLETED_CATEGORY:
            entry[2] += int(count)
            entry[3] += int(points or 0)
        elif category == _CANCELED:
            entry[4] += int(count)
    return {key: _Counts(*values) for key, values in totals.items()}


def payloads(db: Session, sprints: list[Sprint]) -> list[dict]:
    counts = _counts(db, sprints)
    return [
        {
            "id": s.id, "space_id": s.space_id, "name": s.name, "goal": s.goal,
            "state": s.state, "starts_on": s.starts_on, "ends_on": s.ends_on,
            "committed_points": s.committed_points, "completed_points": s.completed_points,
            "started_at": s.started_at, "closed_at": s.closed_at, "created_at": s.created_at,
            "issue_count": counts.get(s.id, _Counts()).issues,
            "points": counts.get(s.id, _Counts()).points,
            "done_issue_count": counts.get(s.id, _Counts()).done,
            "done_points": counts.get(s.id, _Counts()).done_points,
            "canceled_issue_count": counts.get(s.id, _Counts()).canceled,
        }
        for s in sprints
    ]


def split_by_outcome(db: Session, sprint: Sprint) -> tuple[list[Issue], list[Issue],
                                                           list[Issue]]:
    """The live issues in the sprint now, as (done, canceled, unfinished) — what a close
    keeps and counts, keeps and does not count, and moves out. Three queries."""
    done = done_status_ids(db, sprint.workspace_id, sprint.space_id)
    canceled = canceled_status_ids(db, sprint.workspace_id, sprint.space_id)
    issues = sprint_issues(db, sprint)
    return ([issue for issue in issues if issue.status_id in done],
            [issue for issue in issues if issue.status_id in canceled],
            [issue for issue in issues
             if issue.status_id not in done and issue.status_id not in canceled])


def create_sprint(db: Session, workspace_id: str, space_id: str, created_by: str, *,
                  name: str, goal: str | None, starts_on: date | None,
                  ends_on: date | None) -> Sprint:
    sprint = Sprint(workspace_id=workspace_id, space_id=space_id, name=name.strip(),
                    goal=goal, starts_on=starts_on, ends_on=ends_on, state="planned",
                    created_by=created_by)
    db.add(sprint)
    db.flush()
    return sprint


def add_issue(db: Session, sprint: Sprint, issue: Issue, actor_id: str, now: datetime) -> bool:
    """Put `issue` in `sprint`, taking it out of any other open sprint first. False when it
    was already there, so a repeated request changes nothing and writes no event."""
    if issue.sprint_id == sprint.id:
        return False
    if issue.sprint_id is not None:
        current = get_sprint(db, sprint.workspace_id, issue.sprint_id)
        if current is not None:
            remove_issue(db, current, issue, actor_id, now)
    db.add(SprintIssue(workspace_id=sprint.workspace_id, sprint_id=sprint.id,
                       issue_id=issue.id, points_at_add=issue.story_points, added_at=now))
    issue.sprint_id = sprint.id
    issue_svc.record_event(db, issue, "sprint_added", actor_id, to_value=sprint.id)
    db.flush()
    return True


def remove_issue(db: Session, sprint: Sprint, issue: Issue, actor_id: str,
                 now: datetime) -> bool:
    """Take `issue` out of `sprint`, closing its open membership row."""
    if issue.sprint_id != sprint.id:
        return False
    db.query(SprintIssue).filter(
        SprintIssue.workspace_id == sprint.workspace_id, SprintIssue.sprint_id == sprint.id,
        SprintIssue.issue_id == issue.id, SprintIssue.removed_at.is_(None),
    ).update({SprintIssue.removed_at: now}, synchronize_session=False)
    issue.sprint_id = None
    issue_svc.record_event(db, issue, "sprint_removed", actor_id, from_value=sprint.id)
    db.flush()
    return True


def lock_space(db: Session, sprint: Sprint) -> None:
    """Serialise starts in one project. SQLite ignores FOR UPDATE; it has one writer anyway."""
    db.query(Space.id).filter(Space.id == sprint.space_id).with_for_update().one()


def start_sprint(db: Session, sprint: Sprint, *, today: date, now: datetime,
                 starts_on: date | None, ends_on: date | None) -> None:
    """Make `sprint` the active one and freeze its commitment: the points in it now.

    An issue already canceled when the sprint starts is not committed. Nobody is going to
    do it, so committing it would only lower every "completed of committed" the velocity
    chart shows, and the burndown — which takes canceled work out of its scope — would start
    from a different number than the commitment says. One canceled during the sprint was
    committed and stays committed: that is the record of a commitment not kept.
    """
    sprint.starts_on = starts_on or sprint.starts_on or today
    sprint.ends_on = ends_on or sprint.ends_on or (sprint.starts_on + DEFAULT_LENGTH)
    sprint.state = "active"
    sprint.started_at = now
    canceled = canceled_status_ids(db, sprint.workspace_id, sprint.space_id)
    sprint.committed_points = sum(issue.story_points or 0 for issue in sprint_issues(db, sprint)
                                  if issue.status_id not in canceled)
    db.flush()


def close_sprint(db: Session, sprint: Sprint, actor_id: str, now: datetime,
                 carry_to: Sprint | None) -> dict:
    """Close `sprint`: freeze the points completed, and move every unfinished issue to the
    backlog or to `carry_to`. Done issues keep `sprint_id`, so "what did Sprint 12 deliver"
    stays one indexed query.

    Canceled issues keep it too (09-restructuring.md §9): they are closed, so there is
    nothing to carry over, and the sprint is where the decision not to do them was made.
    They are not completed, so their points are not in `completed_points` — the number the
    velocity chart reads for good once this returns.
    """
    finished, canceled, unfinished = split_by_outcome(db, sprint)
    sprint.completed_points = sum(issue.story_points or 0 for issue in finished)
    for issue in unfinished:
        remove_issue(db, sprint, issue, actor_id, now)
        if carry_to is not None:
            add_issue(db, carry_to, issue, actor_id, now)
    sprint.state = "closed"
    sprint.closed_at = now
    db.flush()
    return {"completed_issues": len(finished), "canceled_issues": len(canceled),
            "carried_over": len(unfinished), "completed_points": sprint.completed_points}


def delete_sprint(db: Session, sprint: Sprint, actor_id: str, now: datetime) -> int:
    """Soft-delete a sprint that never ran; its issues go back to the backlog."""
    issues = sprint_issues(db, sprint)
    for issue in issues:
        remove_issue(db, sprint, issue, actor_id, now)
    sprint.deleted = 1
    sprint.deleted_at = now
    db.flush()
    return len(issues)
