"""Sprints, and the history of which issues were in them — the input to every sprint report.

Ported from the PostgreSQL target in docs/design/schema.sql to the MySQL 8 schema that runs
(docs/design/08-reports.md §5.1 has the decisions). Three departures, each for a reason:

* **Points are INTEGER, not NUMERIC(7,1).** `issues.story_points` is an INTEGER, so every
  value frozen here is a sum of integers; a decimal column would only add SQLite's
  "Decimal is not supported natively" warning to every test run and a Decimal-vs-int
  comparison to every report.
* **`sprint_issues` has its own id** rather than the target's `(sprint_id, issue_id,
  added_at)` primary key. MySQL's DATETIME holds whole seconds, so removing an issue and
  adding it back inside one second — a mis-click undone — would collide on that key.
* **"At most one active sprint per space" is enforced in `services/sprint.start_sprint`**,
  under a row lock on the space, because the target's partial unique index needs a partial
  index and MySQL has none. The same reasoning made `active_timers` a table of its own.

**Points are frozen at start and at close** (`committed_points`, `completed_points`), so a
velocity chart shows what was true then, not what the issues say after someone re-estimates
them next month — the rule Jira's velocity chart follows and the reason it is trusted.
"""
import uuid
from datetime import UTC, datetime

from sqlalchemy import CHAR, Column, Date, DateTime, ForeignKey, Index, Integer, String, Text

from db.session import Base

#: A sprint's life: planned → active → closed. Only one `active` per space.
SPRINT_STATES: tuple[str, ...] = ("planned", "active", "closed")


def _now() -> datetime:
    return datetime.now(UTC)


class Sprint(Base):
    __tablename__ = "sprints"
    __table_args__ = (
        # The sprint list of a space and "the active sprint of this space".
        Index("ix_sprints_space_state", "space_id", "state"),
        Index("ix_sprints_workspace_space", "workspace_id", "space_id"),
    )

    id = Column(CHAR(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    workspace_id = Column(CHAR(36), ForeignKey("workspaces.id"), nullable=False)
    space_id = Column(CHAR(36), ForeignKey("spaces.id"), nullable=False)
    name = Column(String(100), nullable=False)
    goal = Column(Text, nullable=True)
    state = Column(String(20), nullable=False, default="planned")
    starts_on = Column(Date, nullable=True)
    ends_on = Column(Date, nullable=True)
    # Frozen at start and at close; NULL until then.
    committed_points = Column(Integer, nullable=True)
    completed_points = Column(Integer, nullable=True)
    started_at = Column(DateTime, nullable=True)
    closed_at = Column(DateTime, nullable=True)
    created_by = Column(CHAR(36), ForeignKey("users.id"), nullable=False)
    created_at = Column(DateTime, default=_now, nullable=False)
    updated_at = Column(DateTime, default=_now, onupdate=_now, nullable=False)
    deleted = Column(Integer, nullable=False, default=0)
    deleted_at = Column(DateTime, nullable=True)


class SprintIssue(Base):
    """One stay of an issue in a sprint: when it came in, with how many points, and when (if
    ever) it left. An issue removed and added back has two rows, which is exactly what the
    sprint report needs to say "removed, then re-added"."""

    __tablename__ = "sprint_issues"
    __table_args__ = (
        Index("ix_sprint_issues_sprint_issue", "sprint_id", "issue_id"),
        Index("ix_sprint_issues_issue", "issue_id"),
    )

    id = Column(CHAR(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    workspace_id = Column(CHAR(36), ForeignKey("workspaces.id"), nullable=False)
    sprint_id = Column(CHAR(36), ForeignKey("sprints.id"), nullable=False)
    issue_id = Column(CHAR(36), ForeignKey("issues.id"), nullable=False)
    points_at_add = Column(Integer, nullable=True)
    added_at = Column(DateTime, default=_now, nullable=False)
    removed_at = Column(DateTime, nullable=True)
