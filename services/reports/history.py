"""What was true at a past instant — replayed from `issue_events` and `sprint_issues`.

The rule (docs/design/08-reports.md §5.5): the value of a field at instant T is the
`from_value` of the first change to it *after* T, or the current value if it has not changed
since. Replaying backwards from the present like this needs no complete history: an issue
created before the event log existed (2026-09-29) simply reports its current value for the
days before its first recorded change, and the report says from when its data is exact
(`meta.data_from`).

Status changes carry status ids, `points_changed` and `estimate_changed` the numbers, and
`assigned` the user ids, so each field replays the same way. Sprint membership is not
replayed from events at all: `sprint_issues` holds the intervals themselves.
"""
from __future__ import annotations

from bisect import bisect_right
from collections import defaultdict
from collections.abc import Iterable
from datetime import UTC, datetime

from sqlalchemy.orm import Session

from models.issue import Issue, IssueEvent
from models.sprint import SprintIssue

_FIELD_EVENTS = {
    "status_changed": "status_id",
    "points_changed": "story_points",
    "estimate_changed": "time_estimate_seconds",
    "assigned": "assignee_id",
}

_INT_FIELDS = {"story_points", "time_estimate_seconds"}

#: How many ids go into one `IN (…)`. Well below MySQL's packet limits and SQLite's
#: 32,766-variable ceiling, so a large project costs a few queries rather than one failure.
_CHUNK = 900


def naive(value: datetime | None) -> datetime | None:
    """Naive UTC: what a DateTime column returns, whatever this session put into it."""
    if value is None:
        return None
    if value.tzinfo is not None:
        return value.astimezone(UTC).replace(tzinfo=None)
    return value


def chunks(ids: list[str]) -> Iterable[list[str]]:
    for start in range(0, len(ids), _CHUNK):
        yield ids[start:start + _CHUNK]


def _int(value: str | None) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except ValueError:
        return None


class History:
    """The event history of a fixed set of issues, loaded once, answered from memory."""

    def __init__(self, db: Session, workspace_id: str, issues: list[Issue]):
        self.issues = {issue.id: issue for issue in issues}
        ids = list(self.issues)
        # field -> issue -> [(at, from, to)], ordered by time
        self._changes: dict[str, dict[str, list[tuple[datetime, str | None, str | None]]]] = {
            name: defaultdict(list) for name in _FIELD_EVENTS.values()}
        self._times: dict[str, dict[str, list[datetime]]] = {
            name: {} for name in _FIELD_EVENTS.values()}
        self._memberships: dict[str, list[tuple[str, datetime, datetime | None]]] = (
            defaultdict(list))
        self.earliest_event: datetime | None = None
        for part in chunks(ids):
            rows = (
                db.query(IssueEvent.issue_id, IssueEvent.event, IssueEvent.from_value,
                         IssueEvent.to_value, IssueEvent.created_at)
                .filter(IssueEvent.workspace_id == workspace_id,
                        IssueEvent.issue_id.in_(part),
                        IssueEvent.event.in_(list(_FIELD_EVENTS)))
                .order_by(IssueEvent.created_at, IssueEvent.id)
                .all()
            )
            for issue_id, event, from_value, to_value, at in rows:
                at = naive(at)
                self._changes[_FIELD_EVENTS[event]][issue_id].append((at, from_value, to_value))
                if self.earliest_event is None or at < self.earliest_event:
                    self.earliest_event = at
            members = (
                db.query(SprintIssue.issue_id, SprintIssue.sprint_id, SprintIssue.added_at,
                         SprintIssue.removed_at)
                .filter(SprintIssue.workspace_id == workspace_id,
                        SprintIssue.issue_id.in_(part))
                .all()
            )
            for issue_id, sprint_id, added_at, removed_at in members:
                self._memberships[issue_id].append((sprint_id, naive(added_at),
                                                    naive(removed_at)))
        for name, per_issue in self._changes.items():
            for issue_id, changes in per_issue.items():
                changes.sort(key=lambda change: change[0])
                self._times[name][issue_id] = [change[0] for change in changes]

    # ---- point-in-time values ------------------------------------------------------------

    def existed(self, issue_id: str, at: datetime) -> bool:
        created = naive(self.issues[issue_id].created_at)
        return created is not None and created <= at

    def value_at(self, issue_id: str, name: str, at: datetime):
        """The field's value at `at`: the `from_value` of the first change after it, else the
        current value."""
        times = self._times[name].get(issue_id)
        if times:
            index = bisect_right(times, at)
            if index < len(times):
                raw = self._changes[name][issue_id][index][1]
                return _int(raw) if name in _INT_FIELDS else raw
        return getattr(self.issues[issue_id], name)

    def status_at(self, issue_id: str, at: datetime) -> str | None:
        if not self.existed(issue_id, at):
            return None
        return self.value_at(issue_id, "status_id", at)

    def points_at(self, issue_id: str, at: datetime) -> int | None:
        return self.value_at(issue_id, "story_points", at)

    def estimate_at(self, issue_id: str, at: datetime) -> int | None:
        return self.value_at(issue_id, "time_estimate_seconds", at)

    def assignee_at(self, issue_id: str, at: datetime) -> str | None:
        return self.value_at(issue_id, "assignee_id", at)

    def sprint_at(self, issue_id: str, at: datetime) -> str | None:
        """The sprint the issue was in at `at`, from the membership intervals."""
        for sprint_id, added_at, removed_at in self._memberships.get(issue_id, []):
            if added_at <= at and (removed_at is None or removed_at > at):
                return sprint_id
        return None

    def in_sprint_at(self, issue_id: str, sprint_id: str, at: datetime) -> bool:
        return any(
            sid == sprint_id and added_at <= at and (removed_at is None or removed_at > at)
            for sid, added_at, removed_at in self._memberships.get(issue_id, [])
        )

    def status_changes(self, issue_id: str) -> list[tuple[datetime, str | None, str | None]]:
        return self._changes["status_id"].get(issue_id, [])

    # ---- derived dates -------------------------------------------------------------------

    def resolved_at(self, issue_id: str, done_ids: set[str]) -> datetime | None:
        """The last move into a done status, if the issue is done now. Reopening clears it,
        and so does canceling: `done_ids` is the `done` category only, and canceled work is
        closed but never resolved (docs/design/09-restructuring.md §9).
        An issue created straight into done, or done before the log existed, resolves at
        its creation: the earliest instant the data can vouch for."""
        issue = self.issues[issue_id]
        if issue.status_id not in done_ids:
            return None
        last = None
        for at, from_value, to_value in self.status_changes(issue_id):
            if to_value in done_ids and from_value not in done_ids:
                last = at
        return last or naive(issue.created_at)

    def first_done_at(self, issue_id: str, done_ids: set[str]) -> datetime | None:
        issue = self.issues[issue_id]
        for at, from_value, to_value in self.status_changes(issue_id):
            if to_value in done_ids and from_value not in done_ids:
                return at
        return naive(issue.created_at) if issue.status_id in done_ids else None

    def first_entered(self, issue_id: str, status_ids: set[str]) -> datetime | None:
        """The first move into any of `status_ids`, or creation if it was created in one."""
        changes = self.status_changes(issue_id)
        if changes and changes[0][1] in status_ids:
            return naive(self.issues[issue_id].created_at)
        if not changes and self.issues[issue_id].status_id in status_ids:
            return naive(self.issues[issue_id].created_at)
        for at, _from_value, to_value in changes:
            if to_value in status_ids:
                return at
        return None
