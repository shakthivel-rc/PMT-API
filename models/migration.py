"""Migration plans: the register, the runner's record, and the baseline it reconciles against.

docs/design/09-restructuring.md §8 is the design. The restructuring plan asks for "dry-run
output, before/after mappings, resumable/idempotent batches, explicit failures, and rollback
support", and for a migration register of "old container IDs -> destination IDs; old issue
ID/key -> destination identity/key; parent mapping; status mapping; sprint treatment; owner;
exception; approval". These three tables are that, literally:

* `migration_plans` — one wave: its operations as submitted, its state, who approved it.
* `migration_items` — one row of the register per thing the wave changes (or decides to leave
  where it is), with the `before` and `after` snapshot the rollback reverses. A subtree that
  moves between spaces is one `group_key`, never split across batches: a parent and child in
  different spaces, even between two batches, is an illegal relationship.
* `migration_baselines` — counts and a per-issue fingerprint taken before the wave, which the
  reconciliation gates compare against afterwards.

No foreign key from an item to its subject: a subject is an issue, a list, a folder or a
space, and the register must outlive what it describes in either direction.
"""
import uuid
from datetime import UTC, datetime

from sqlalchemy import (
    CHAR,
    JSON,
    Boolean,
    Column,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects import mysql

from db.session import Base

#: MySQL's TEXT stops at 64 KiB; a fingerprint of a few thousand issues does not fit. The same
#: variant models/reporting.py uses for the report cache.
_LONG_TEXT = Text().with_variant(mysql.LONGTEXT(), "mysql")

MIGRATION_PLAN_STATES: tuple[str, ...] = (
    "draft", "validated", "approved", "running", "completed", "failed", "rolled_back",
    "canceled",
)
MIGRATION_ITEM_KINDS: tuple[str, ...] = (
    "issue_move", "list_reparent", "issue_retype", "issue_reparent", "issue_set_status",
    "container_archive", "retain",
)
MIGRATION_ITEM_STATES: tuple[str, ...] = ("pending", "applied", "failed", "skipped",
                                          "rolled_back")


def _now() -> datetime:
    return datetime.now(UTC)


class MigrationBaseline(Base):
    __tablename__ = "migration_baselines"
    __table_args__ = (Index("ix_migration_baselines_workspace", "workspace_id", "captured_at"),)

    id = Column(CHAR(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    workspace_id = Column(CHAR(36), ForeignKey("workspaces.id"), nullable=False)
    label = Column(String(120), nullable=False)
    # The spaces it covers; every space of the workspace when captured without a choice.
    space_ids = Column(JSON, nullable=False)
    # Per-space totals: issues, comments, seconds logged, PR links, sprint rows, events.
    totals = Column(JSON, nullable=False)
    # Per issue, as JSON text: key, space, list, stage, parent, and the same counts. Text
    # rather than JSON because it is written once and read whole, and MySQL's JSON type
    # would re-parse and re-serialise megabytes of it for nothing.
    fingerprints = Column(_LONG_TEXT, nullable=False)
    issue_count = Column(Integer, nullable=False, default=0)
    captured_by = Column(CHAR(36), ForeignKey("users.id"), nullable=False)
    captured_at = Column(DateTime, default=_now, nullable=False)


class MigrationPlan(Base):
    __tablename__ = "migration_plans"
    __table_args__ = (Index("ix_migration_plans_workspace", "workspace_id", "created_at"),)

    id = Column(CHAR(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    workspace_id = Column(CHAR(36), ForeignKey("workspaces.id"), nullable=False)
    name = Column(String(120), nullable=False)
    description = Column(Text, nullable=True)
    status = Column(String(20), nullable=False, default="draft", server_default="draft")
    # The operations as submitted (schemas/migrations.py); items are expanded from them.
    operations = Column(JSON, nullable=False)
    # Every space an operation reads from or writes to: what `freeze` freezes.
    affected_space_ids = Column(JSON, nullable=True)
    # The last dry-run's summary: problem counts and item counts by kind.
    validation = Column(JSON, nullable=True)
    baseline_id = Column(CHAR(36), ForeignKey("migration_baselines.id"), nullable=True)
    created_by = Column(CHAR(36), ForeignKey("users.id"), nullable=False)
    approved_by = Column(CHAR(36), ForeignKey("users.id"), nullable=True)
    approved_at = Column(DateTime, nullable=True)
    validated_at = Column(DateTime, nullable=True)
    frozen_at = Column(DateTime, nullable=True)
    started_at = Column(DateTime, nullable=True)
    finished_at = Column(DateTime, nullable=True)
    rolled_back_at = Column(DateTime, nullable=True)
    canceled_at = Column(DateTime, nullable=True)
    # Optimistic concurrency: two admins approving or executing at once.
    version = Column(Integer, nullable=False, default=1, server_default="1")
    created_at = Column(DateTime, default=_now, nullable=False)
    updated_at = Column(DateTime, default=_now, onupdate=_now, nullable=False)


class MigrationItem(Base):
    __tablename__ = "migration_items"
    __table_args__ = (
        UniqueConstraint("plan_id", "kind", "subject_id", name="uq_migration_items_subject"),
        Index("ix_migration_items_plan_seq", "plan_id", "seq"),
        Index("ix_migration_items_plan_state", "plan_id", "state"),
    )

    id = Column(CHAR(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    workspace_id = Column(CHAR(36), ForeignKey("workspaces.id"), nullable=False)
    plan_id = Column(CHAR(36), ForeignKey("migration_plans.id"), nullable=False)
    # Execution order: containers first, then issues parent-first.
    seq = Column(Integer, nullable=False)
    # The subtree root's id for an `issue_move`, else the item's own subject id.
    group_key = Column(String(64), nullable=False)
    kind = Column(String(30), nullable=False)
    subject_type = Column(String(20), nullable=False)
    subject_id = Column(CHAR(36), nullable=False)
    # The issue key (or container name) when the item was planned, for the register.
    subject_key = Column(String(120), nullable=True)
    source = Column(JSON, nullable=True)
    target = Column(JSON, nullable=True)
    options = Column(JSON, nullable=True)
    owner_id = Column(CHAR(36), ForeignKey("users.id"), nullable=True)
    exception_note = Column(Text, nullable=True)
    approved = Column(Boolean, nullable=False, default=False, server_default=text("0"))
    state = Column(String(20), nullable=False, default="pending", server_default="pending")
    batch = Column(Integer, nullable=True)
    error = Column(Text, nullable=True)
    # The snapshot the rollback reverses, and what the item produced (new key, version).
    before = Column(JSON, nullable=True)
    after = Column(JSON, nullable=True)
    applied_at = Column(DateTime, nullable=True)
    rolled_back_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=_now, nullable=False)
    updated_at = Column(DateTime, default=_now, onupdate=_now, nullable=False)
