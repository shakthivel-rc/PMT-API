"""Issues, their comments, and the event history every report is computed from.

**One table for every issue type.** Epics, stories, tasks, sub-tasks and bugs differ in
which parent they may have and in how the UI draws them, not in what they store, so they
share a table and a `type` column. A table per type would turn "everything assigned to me"
into a five-way UNION and make re-typing an issue (a task that turns out to be a story) a
cross-table move.

**The hierarchy is an adjacency list** (`parent_id`). Depth is bounded by the type rules
(utils/hierarchy_rules.py): three levels under the standard convention — epic > story, task
or bug > sub-task — and at most five for trees written under the legacy rules before
2026-10-05, so the recursive query that walks it is bounded too. A closure table or `ltree`
path would make "every descendant of this epic" a single indexed read; that is the Phase 2
change if epic roll-ups measure slow, and it is additive (docs/design/02-data-model.md).

**Custom fields are not here yet.** They are Phase 2, as typed definitions plus a JSONB
value map on PostgreSQL; see the target DDL in docs/design/schema.sql.
"""
import uuid
from datetime import UTC, datetime

from sqlalchemy import (
    CHAR,
    Boolean,
    Column,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)

from db.session import Base

# `issues.sprint_id` references `sprints`; importing the module here registers that table on
# the metadata wherever issues are, so a process that imports only this module (a worker, a
# script) can still flush an issue.
from models import sprint as _sprint  # noqa: F401
from utils import hierarchy_rules

ISSUE_TYPES: tuple[str, ...] = hierarchy_rules.ISSUE_TYPES

#: The standard convention (docs/design/09-restructuring.md §2.1): Story, Task and Bug are
#: peers under an optional Epic, and a Subtask sits under any of the three. Every
#: relationship that is created or changed must satisfy it.
STANDARD_PARENT_TYPES: dict[str, frozenset[str]] = hierarchy_rules.STANDARD_PARENT_TYPES

#: The rules before 2026-10-05: a bug could attach anywhere, including under a sub-task,
#: and a task under a story. Rows written under them are kept as they are until someone
#: changes them (§2.2) — never used to accept a new relationship.
LEGACY_PARENT_TYPES: dict[str, frozenset[str]] = hierarchy_rules.LEGACY_PARENT_TYPES

#: Which types each type may sit under when a relationship is created or changed. An empty
#: set means "top level only". It was the legacy table until 2026-10-05, when the issue
#: routes moved to the standard convention (09-restructuring.md §2; services/issue
#: .parent_problem asks utils/hierarchy_rules directly); kept, pointing at the standard, so
#: anything that still imports the old name gets today's rule rather than an ImportError or
#: yesterday's. New code reads `STANDARD_PARENT_TYPES`.
ALLOWED_PARENT_TYPES: dict[str, frozenset[str]] = STANDARD_PARENT_TYPES
PARENT_REQUIRED: frozenset[str] = hierarchy_rules.PARENT_REQUIRED

#: The kinds of typed dependency between two issues (09-restructuring.md §10). `blocks`,
#: `duplicates` and `clones` read from source to target ("A blocks B"); `relates` is
#: symmetric and stored once.
DEPENDENCY_KINDS: tuple[str, ...] = ("blocks", "relates", "duplicates", "clones")

PRIORITIES: tuple[str, ...] = ("urgent", "high", "medium", "low", "none")

#: Who caused an `issue_events` row. `activity_logs.user_id` is NOT NULL, so work with no
#: local user behind it — a GitHub webhook moving an issue to Done — cannot be recorded
#: there (services/activity_log.py says why it must not invent one). This table can.
#: `migration` (2026-10-05) is a migration plan's runner: the actor id is the admin who ran
#: it, and the kind keeps its changes apart from that person's everyday ones in the history.
ACTOR_KINDS: tuple[str, ...] = ("user", "integration", "automation", "migration")


def _now() -> datetime:
    return datetime.now(UTC)


class Issue(Base):
    __tablename__ = "issues"
    __table_args__ = (
        UniqueConstraint("space_id", "number", name="uq_issues_space_number"),
        # The board query: one list's issues, grouped by status, in rank order. With the
        # workspace first it is also the index a tenant-scoped RLS predicate can use.
        Index("ix_issues_workspace_list_status_rank",
              "workspace_id", "list_id", "status_id", "rank"),
        # The same board drawn for a whole space.
        Index("ix_issues_space_status_rank", "space_id", "status_id", "rank"),
        # "My work" — the most-read query after the board.
        Index("ix_issues_workspace_assignee", "workspace_id", "assignee_id"),
        # Reports: a space's issues by status, inside one tenant (docs/design/08-reports.md
        # §5.2). The board's ix_issues_space_status_rank leads with space_id and cannot serve
        # a tenant-first predicate.
        Index("ix_issues_workspace_space_status", "workspace_id", "space_id", "status_id"),
        # The sprint board and every sprint report: "the issues in this sprint".
        Index("ix_issues_workspace_sprint", "workspace_id", "sprint_id"),
        # The space's ordered backlog (09-restructuring.md §5.2).
        Index("ix_issues_space_backlog_rank", "space_id", "backlog_rank"),
    )

    id = Column(CHAR(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    workspace_id = Column(CHAR(36), ForeignKey("workspaces.id"), nullable=False)
    space_id = Column(CHAR(36), ForeignKey("spaces.id"), nullable=False)
    list_id = Column(CHAR(36), ForeignKey("lists.id"), nullable=False)
    # Per space, from `spaces.issue_counter`. The human key is `{space.key}-{number}`, and
    # it is computed rather than stored so renaming a space key is one row, not every issue.
    number = Column(Integer, nullable=False)
    type = Column(String(20), nullable=False, default="task")
    title = Column(String(500), nullable=False)
    description = Column(Text, nullable=True)
    status_id = Column(CHAR(36), ForeignKey("workflow_statuses.id"), nullable=False)
    parent_id = Column(CHAR(36), ForeignKey("issues.id"), nullable=True, index=True)
    assignee_id = Column(CHAR(36), ForeignKey("users.id"), nullable=True)
    reporter_id = Column(CHAR(36), ForeignKey("users.id"), nullable=False)
    priority = Column(String(10), nullable=False, default="none")
    story_points = Column(Integer, nullable=True)
    # The one time estimate, in seconds (models/time_tracking.py says why seconds). There is
    # deliberately no stored "remaining estimate": Jira's decouples from the original the
    # moment work is logged and is its most-reported source of numbers that do not add up,
    # so remaining is derived — estimate minus time logged — wherever it is shown.
    time_estimate_seconds = Column(Integer, nullable=True)
    # The sprint the issue is in now, or NULL for the backlog. A convenience for filtering;
    # the history — when it came in and left — is `sprint_issues` (models/sprint.py), and
    # only `services/sprint.py` writes either, in the same transaction.
    sprint_id = Column(CHAR(36), ForeignKey("sprints.id"), nullable=True)
    # Order within a board column: a base-36 fractional index (utils/rank.py). Moving a
    # card rewrites this one column on one row, whatever the size of the column. 128 is the
    # ceiling; utils/rank.REBALANCE_LENGTH triggers a column rebalance long before it.
    rank = Column(String(128), nullable=False)
    start_date = Column(Date, nullable=True)
    due_date = Column(Date, nullable=True)
    # The epic's Definition of Done (09-restructuring.md §2.3). Any type may carry it; an epic
    # created or re-typed while the governance setting is on must.
    completion_criteria = Column(Text, nullable=True)
    # Blocked work stays in its stage and keeps counting toward WIP (§5.3); the flag and the
    # reason make it visible. The reason says what blocks it, who owns the blocker, and the
    # next action.
    blocked = Column(Boolean, nullable=False, default=False, server_default=text("0"))
    blocked_reason = Column(String(1000), nullable=True)
    blocked_at = Column(DateTime, nullable=True)
    blocked_by_id = Column(CHAR(36), ForeignKey("users.id"), nullable=True)
    # When it entered its current stage: the work-item age a Kanban team inspects daily.
    # Written by every status change (services/issue.set_status) and backfilled from the last
    # `status_changed` event; null only for a row no writer has touched since.
    status_entered_at = Column(DateTime, nullable=True)
    # Order in the space's backlog: a fractional rank over the whole space (utils/rank.py),
    # kept apart from `rank`, which orders one board column (§5.2).
    backlog_rank = Column(String(128), nullable=False)
    # Optimistic concurrency. A PATCH names the version it read; a mismatch is a 409 and
    # the client re-reads, instead of two people's edits silently overwriting each other.
    version = Column(Integer, nullable=False, default=1)
    created_at = Column(DateTime, default=_now, nullable=False)
    updated_at = Column(DateTime, default=_now, onupdate=_now, nullable=False)
    deleted = Column(Integer, nullable=False, default=0)
    deleted_at = Column(DateTime, nullable=True)


class IssueComment(Base):
    __tablename__ = "issue_comments"
    __table_args__ = (Index("ix_issue_comments_issue_created", "issue_id", "created_at"),)

    id = Column(CHAR(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    workspace_id = Column(CHAR(36), ForeignKey("workspaces.id"), nullable=False)
    issue_id = Column(CHAR(36), ForeignKey("issues.id"), nullable=False)
    author_id = Column(CHAR(36), ForeignKey("users.id"), nullable=False)
    body = Column(Text, nullable=False)
    created_at = Column(DateTime, default=_now, nullable=False)
    updated_at = Column(DateTime, default=_now, onupdate=_now, nullable=False)
    deleted = Column(Integer, nullable=False, default=0)


class IssueEvent(Base):
    """An append-only record of what changed on an issue, by whom, and when.

    Written in the same transaction as the change it describes, so it cannot disagree with
    the issue row. It is the input to every Agile report: a burndown is the sum of story
    points whose `status_changed` event entered a `done` status, per day; a cumulative flow
    diagram is the count of issues per status category at each day's end. Recomputing those
    from the current issue rows is impossible — the current row has forgotten yesterday.
    """

    __tablename__ = "issue_events"
    __table_args__ = (
        Index("ix_issue_events_issue_created", "issue_id", "created_at"),
        Index("ix_issue_events_workspace_created", "workspace_id", "created_at"),
        # Reports read one kind of event across a tenant: "every status change since …".
        Index("ix_issue_events_workspace_event_created", "workspace_id", "event",
              "created_at"),
    )

    id = Column(CHAR(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    workspace_id = Column(CHAR(36), ForeignKey("workspaces.id"), nullable=False)
    issue_id = Column(CHAR(36), ForeignKey("issues.id"), nullable=False)
    actor_id = Column(CHAR(36), ForeignKey("users.id"), nullable=True)
    actor_kind = Column(String(20), nullable=False, default="user")
    # created | status_changed | assigned | updated | commented | linked | deleted
    # | time_logged | time_updated | time_deleted | points_changed | estimate_changed
    # | sprint_added | sprint_removed. `points_changed` carries the story points before and
    # after, so a sprint report can show `3 → 5` exactly, and `estimate_changed` the time
    # estimate in seconds for the burndown in hours; the sprint events carry the sprint id in
    # `to_value` / `from_value`. The time events carry the entry's
    # duration in seconds before (`from_value`) and after (`to_value`) the change — never
    # its note, which is free text and does not fit 255 characters.
    #
    # Added 2026-10-05 (docs/design/09-restructuring.md §2.4, §6.2, §7, §10): the structural
    # events `parent_changed`, `type_changed`, `list_changed`, `space_changed` (ids) and
    # `key_changed` (keys) record a reorganisation instead of rewriting history; `blocked`
    # (`to_value` the reason, truncated) and `unblocked`; `automation_failed` (`from_value`
    # the stage the issue stayed in, `to_value` why the merge action could not run);
    # `dependency_added` / `dependency_removed` (`to_value` `<kind>:<other issue id>`); and
    # `fix_version_added` / `fix_version_removed` (the release id).
    event = Column(String(40), nullable=False)
    from_value = Column(String(255), nullable=True)
    to_value = Column(String(255), nullable=True)
    created_at = Column(DateTime, default=_now, nullable=False)


class IssueKeyAlias(Base):
    """A key an issue used to have, before it moved to another space (09-restructuring.md
    §7.3). `PAY-7` keeps resolving to the issue now called `OPS-3`, for every commit,
    branch, pull request and pasted link that already quotes it.

    Unique per workspace: numbers are never reused (`spaces.issue_counter` only increments)
    and a retired key is never handed out again, so one key names at most one issue.
    """

    __tablename__ = "issue_key_aliases"
    __table_args__ = (
        UniqueConstraint("workspace_id", "space_key", "number", name="uq_issue_key_aliases_key"),
        Index("ix_issue_key_aliases_issue", "issue_id"),
    )

    id = Column(CHAR(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    workspace_id = Column(CHAR(36), ForeignKey("workspaces.id"), nullable=False)
    issue_id = Column(CHAR(36), ForeignKey("issues.id"), nullable=False)
    # The space the issue had this key in, and the key as it was spelled then.
    space_id = Column(CHAR(36), ForeignKey("spaces.id"), nullable=False)
    space_key = Column(String(10), nullable=False)
    number = Column(Integer, nullable=False)
    created_at = Column(DateTime, default=_now, nullable=False)


class IssueLink(Base):
    """A typed dependency between two issues of one workspace, possibly in different spaces —
    how cross-space coordination is recorded without a cross-space parent (§10)."""

    __tablename__ = "issue_links"
    __table_args__ = (
        UniqueConstraint("source_issue_id", "target_issue_id", "kind",
                         name="uq_issue_links_pair_kind"),
        Index("ix_issue_links_workspace_source", "workspace_id", "source_issue_id"),
        Index("ix_issue_links_target", "target_issue_id"),
    )

    id = Column(CHAR(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    workspace_id = Column(CHAR(36), ForeignKey("workspaces.id"), nullable=False)
    source_issue_id = Column(CHAR(36), ForeignKey("issues.id"), nullable=False)
    target_issue_id = Column(CHAR(36), ForeignKey("issues.id"), nullable=False)
    kind = Column(String(20), nullable=False)
    created_by = Column(CHAR(36), ForeignKey("users.id"), nullable=False)
    created_at = Column(DateTime, default=_now, nullable=False)
