"""Where work lives: Workspace > Space > Folder > List, and each space's workflow.

The ClickUp shape, with one Jira idea folded in: a **space owns the issue key** (`PAY` in
`PAY-142`) and the workflow its issues move through. Folders are optional — a list may
hang directly off a space — because a small team with one backlog should not have to
invent a folder to hold it.

Issues belong to exactly one list. Moving an issue between lists of the same space keeps
its key; moving it to another space re-keys it, and the old key keeps resolving through
`issue_key_aliases` (models/issue.py) for every link and commit message that already quotes
it — docs/design/09-restructuring.md §7.

**Since 2026-10-05 the workflow is the space's to design** (09-restructuring.md §4): stages
with a colour, a reporting category, a WIP flag and limit; an initial stage; free or
restricted transitions; and the GitHub merge action's target. A stage's id never changes —
names and positions do — and a removed stage is soft-deleted, because `issue_events` and the
daily snapshots name stage ids and must keep resolving them to a category.
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
    Numeric,
    String,
    Text,
    UniqueConstraint,
    text,
)

from db.session import Base

# `spaces.frozen_by_plan_id` references `migration_plans`; importing the module registers that
# table on the metadata wherever spaces are, as models/issue.py does for sprints.
from models import migration as _migration  # noqa: F401
from utils import workflow_rules

#: The buckets every status falls into, whatever a team names its columns. Reports
#: (burndown, cumulative flow) and automations ("move to Done") reason about categories,
#: never about names — "Shipped", "Closed" and "Done" are all `done`. `canceled` was added on
#: 2026-10-05 (utils/workflow_rules.py says why).
STATUS_CATEGORIES: tuple[str, ...] = workflow_rules.STATUS_CATEGORIES

#: What a new space starts with when no template is named: the `default` template of
#: services/workflow_templates.py, which is where colours, the WIP flags, the initial stage
#: and the merge target are decided. Four columns because "In Review" is the state a
#: code-hosted team spends the most time in and the one a PR webhook moves issues out of.
DEFAULT_STATUSES: tuple[tuple[str, str], ...] = (
    ("To Do", "todo"),
    ("In Progress", "in_progress"),
    ("In Review", "in_progress"),
    ("Done", "done"),
)


def _now() -> datetime:
    return datetime.now(UTC)


class Space(Base):
    __tablename__ = "spaces"
    __table_args__ = (UniqueConstraint("workspace_id", "key", name="uq_spaces_workspace_key"),)

    id = Column(CHAR(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    workspace_id = Column(CHAR(36), ForeignKey("workspaces.id"), nullable=False, index=True)
    name = Column(String(100), nullable=False)
    # 2-10 upper-case letters/digits, starting with a letter. Validated in the request
    # schema; `utils/issue_keys.KEY_PATTERN` is the same shape, so anything a space can be
    # called is something a branch name or commit message can reference. A key that is
    # changed stays reserved in `space_key_aliases` (09-restructuring.md §3.5).
    key = Column(String(10), nullable=False)
    # The last issue number handed out in this space. Incremented with a single UPDATE in
    # the same transaction that inserts the issue, so the row lock serialises concurrent
    # creates; `uq_issues_space_number` is the backstop if anything ever bypasses it. It
    # only ever increments, which is what lets an old key alias never be shadowed.
    issue_counter = Column(Integer, nullable=False, default=0)

    # ---- workflow (09-restructuring.md §4) -------------------------------------------------
    # No foreign key on either stage reference: `workflow_statuses.space_id` already points
    # here, and the reverse FK would make the two tables a cycle that neither
    # `metadata.sorted_tables` (the test teardown) nor MySQL's drop order can resolve. Both
    # are validated by every writer (services/transitions.py, services/workflow.py).
    initial_status_id = Column(CHAR(36), nullable=True)
    transition_mode = Column(String(20), nullable=False, default="any", server_default="any")
    # Optimistic concurrency for publishing: a publish names the version it previewed.
    workflow_version = Column(Integer, nullable=False, default=1, server_default="1")
    wip_policy = Column(String(10), nullable=False, default="warn", server_default="warn")
    merge_action = Column(String(20), nullable=False, default="status",
                          server_default="status")
    merge_target_status_id = Column(CHAR(36), nullable=True)

    # ---- the charter: the creation checklist (09-restructuring.md §3.2) --------------------
    description = Column(Text, nullable=True)
    owner_id = Column(CHAR(36), ForeignKey("users.id"), nullable=True)
    delivery_method = Column(String(10), nullable=True)
    # A document, validated by `schemas/workspaces.SpaceCharter` on every write and read —
    # the column does not constrain it, exactly like `workspaces.settings`.
    charter = Column(JSON, nullable=True)

    # ---- financial budget (Phase 1) --------------------------------------------------------
    budget_amount = Column(Numeric(12, 2), nullable=True)
    budget_alert_threshold = Column(Integer, nullable=False, default=80, server_default="80")

    # ---- AI Autonomous Agents (Phase 3) ----------------------------------------------------
    ai_triage_enabled = Column(Boolean, nullable=False, default=True, server_default=text("1"))
    ai_standup_enabled = Column(Boolean, nullable=False, default=True, server_default=text("1"))
    ai_retrospective_enabled = Column(Boolean, nullable=False, default=True, server_default=text("1"))

    # ---- write state (09-restructuring.md §3.4) -------------------------------------------
    archived_at = Column(DateTime, nullable=True)
    archived_by = Column(CHAR(36), ForeignKey("users.id"), nullable=True)
    frozen_at = Column(DateTime, nullable=True)
    frozen_by = Column(CHAR(36), ForeignKey("users.id"), nullable=True)
    freeze_reason = Column(String(500), nullable=True)
    # Set when a migration plan froze the space; only that plan's runner may write while it
    # is frozen, and only that plan's unfreeze (or an admin's, once the plan is not running)
    # lifts it.
    frozen_by_plan_id = Column(CHAR(36), ForeignKey("migration_plans.id"), nullable=True)

    created_at = Column(DateTime, default=_now, nullable=False)
    updated_at = Column(DateTime, default=_now, onupdate=_now, nullable=False)
    deleted = Column(Integer, nullable=False, default=0)


class Folder(Base):
    __tablename__ = "folders"

    id = Column(CHAR(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    workspace_id = Column(CHAR(36), ForeignKey("workspaces.id"), nullable=False)
    space_id = Column(CHAR(36), ForeignKey("spaces.id"), nullable=False, index=True)
    name = Column(String(100), nullable=False)
    position = Column(Integer, nullable=False, default=0)
    archived_at = Column(DateTime, nullable=True)
    archived_by = Column(CHAR(36), ForeignKey("users.id"), nullable=True)
    created_at = Column(DateTime, default=_now, nullable=False)
    updated_at = Column(DateTime, default=_now, onupdate=_now, nullable=False)
    deleted = Column(Integer, nullable=False, default=0)


class ProjectList(Base):
    """A list of issues. Named `ProjectList` in Python only because `list` is a builtin."""

    __tablename__ = "lists"

    id = Column(CHAR(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    workspace_id = Column(CHAR(36), ForeignKey("workspaces.id"), nullable=False)
    space_id = Column(CHAR(36), ForeignKey("spaces.id"), nullable=False, index=True)
    folder_id = Column(CHAR(36), ForeignKey("folders.id"), nullable=True)
    name = Column(String(100), nullable=False)
    position = Column(Integer, nullable=False, default=0)
    archived_at = Column(DateTime, nullable=True)
    archived_by = Column(CHAR(36), ForeignKey("users.id"), nullable=True)
    created_at = Column(DateTime, default=_now, nullable=False)
    updated_at = Column(DateTime, default=_now, onupdate=_now, nullable=False)
    deleted = Column(Integer, nullable=False, default=0)


class WorkflowStatus(Base):
    """One stage of a space's workflow. The board view renders the live ones left to right."""

    __tablename__ = "workflow_statuses"
    __table_args__ = (Index("ix_workflow_statuses_space_position", "space_id", "position"),)

    id = Column(CHAR(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    workspace_id = Column(CHAR(36), ForeignKey("workspaces.id"), nullable=False)
    space_id = Column(CHAR(36), ForeignKey("spaces.id"), nullable=False)
    name = Column(String(50), nullable=False)
    category = Column(String(20), nullable=False)
    position = Column(Integer, nullable=False, default=0)
    # A tone name from utils/workflow_rules.STAGE_COLORS, never a hex value.
    color = Column(String(20), nullable=False, default="neutral", server_default="neutral")
    counts_toward_wip = Column(Boolean, nullable=False, default=False,
                               server_default=text("0"))
    wip_limit = Column(Integer, nullable=True)
    # Soft delete: a removed stage keeps resolving for history (module docstring).
    deleted = Column(Integer, nullable=False, default=0, server_default="0")
    deleted_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=_now, nullable=False)
    updated_at = Column(DateTime, default=_now, onupdate=_now, nullable=False)


class WorkflowTransition(Base):
    """One allowed move in a `restricted` workflow. `from_status_id` NULL is a global
    transition: from any stage. Ignored — but kept — while the space's mode is `any`."""

    __tablename__ = "workflow_transitions"
    __table_args__ = (
        UniqueConstraint("space_id", "from_status_id", "to_status_id",
                         name="uq_workflow_transitions_pair"),
        Index("ix_workflow_transitions_space", "workspace_id", "space_id"),
    )

    id = Column(CHAR(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    workspace_id = Column(CHAR(36), ForeignKey("workspaces.id"), nullable=False)
    space_id = Column(CHAR(36), ForeignKey("spaces.id"), nullable=False)
    from_status_id = Column(CHAR(36), ForeignKey("workflow_statuses.id"), nullable=True)
    to_status_id = Column(CHAR(36), ForeignKey("workflow_statuses.id"), nullable=False)
    created_at = Column(DateTime, default=_now, nullable=False)


class WorkflowTemplate(Base):
    """A workspace's own reusable workflow. The built-in ones live in code
    (services/workflow_templates.py) and are never rows."""

    __tablename__ = "workflow_templates"
    __table_args__ = (Index("ix_workflow_templates_workspace", "workspace_id"),)

    id = Column(CHAR(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    workspace_id = Column(CHAR(36), ForeignKey("workspaces.id"), nullable=False)
    name = Column(String(100), nullable=False)
    description = Column(Text, nullable=True)
    # `schemas/workflow.TemplateDefinition`: stages by position, the initial stage, the
    # transitions and the merge target as stage indexes.
    definition = Column(JSON, nullable=False)
    created_by = Column(CHAR(36), ForeignKey("users.id"), nullable=False)
    created_at = Column(DateTime, default=_now, nullable=False)
    updated_at = Column(DateTime, default=_now, onupdate=_now, nullable=False)
    deleted = Column(Integer, nullable=False, default=0)


class WorkflowRevision(Base):
    """Every published workflow of a space, and what publishing it did — the structural
    change recorded rather than history rewritten (plan Phase 5 step 5)."""

    __tablename__ = "workflow_revisions"
    __table_args__ = (Index("ix_workflow_revisions_space_version", "space_id", "version"),)

    id = Column(CHAR(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    workspace_id = Column(CHAR(36), ForeignKey("workspaces.id"), nullable=False)
    space_id = Column(CHAR(36), ForeignKey("spaces.id"), nullable=False)
    version = Column(Integer, nullable=False)
    definition = Column(JSON, nullable=False)
    impact = Column(JSON, nullable=True)
    published_by = Column(CHAR(36), ForeignKey("users.id"), nullable=False)
    published_at = Column(DateTime, default=_now, nullable=False)


class SpaceWorkflowEditor(Base):
    """A member granted workflow-edit permission on one space (09-restructuring.md §4.6).
    Admins and owners have it everywhere and are never rows here."""

    __tablename__ = "space_workflow_editors"
    __table_args__ = (
        UniqueConstraint("space_id", "user_id", name="uq_space_workflow_editors_space_user"),
    )

    id = Column(CHAR(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    workspace_id = Column(CHAR(36), ForeignKey("workspaces.id"), nullable=False)
    space_id = Column(CHAR(36), ForeignKey("spaces.id"), nullable=False)
    user_id = Column(CHAR(36), ForeignKey("users.id"), nullable=False, index=True)
    granted_by = Column(CHAR(36), ForeignKey("users.id"), nullable=False)
    created_at = Column(DateTime, default=_now, nullable=False)


class SpaceKeyAlias(Base):
    """A key a space used to have. It keeps resolving (`PAY-7` still finds the issue) and it
    stays reserved, so no other space can take it and make an old reference ambiguous."""

    __tablename__ = "space_key_aliases"
    __table_args__ = (
        UniqueConstraint("workspace_id", "key", name="uq_space_key_aliases_workspace_key"),
    )

    id = Column(CHAR(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    workspace_id = Column(CHAR(36), ForeignKey("workspaces.id"), nullable=False)
    space_id = Column(CHAR(36), ForeignKey("spaces.id"), nullable=False, index=True)
    key = Column(String(10), nullable=False)
    created_at = Column(DateTime, default=_now, nullable=False)
