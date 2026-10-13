"""The tenant: a workspace, and who belongs to it.

**Every project-management row carries `workspace_id`, and every query filters on it.**
That column is the tenancy boundary. In Phase 1 it is enforced by the application — the
`workspace_access` dependency resolves membership once per request and every service takes
the resolved workspace id as its first argument. On PostgreSQL the same column becomes the
key of a row-level-security policy, so a query that forgets the filter returns nothing
rather than someone else's data (docs/design/02-data-model.md, "Tenant isolation").

Workspace roles are a second authorisation layer beside the global RBAC catalogue in
`core/permissions.py`, not a replacement for it: the catalogue answers "may this account
administer the platform", a membership answers "what may this account do *inside this
tenant*". Keeping them apart is what lets one person be an owner of their own workspace
and a guest in a customer's.
"""
import uuid
from datetime import UTC, datetime

from sqlalchemy import (
    CHAR,
    JSON,
    Column,
    DateTime,
    ForeignKey,
    Integer,
    String,
    UniqueConstraint,
)

from db.session import Base

#: Ordered least to most privileged. `dependencies/workspace.workspace_access` compares
#: positions, so a new role is inserted where it belongs rather than appended.
WORKSPACE_ROLES: tuple[str, ...] = ("guest", "member", "admin", "owner")


def _now() -> datetime:
    return datetime.now(UTC)


class Workspace(Base):
    __tablename__ = "workspaces"

    id = Column(CHAR(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    name = Column(String(100), nullable=False)
    # URL-safe and globally unique: it appears in links people paste into chat.
    slug = Column(String(50), nullable=False, unique=True)
    created_by = Column(CHAR(36), ForeignKey("users.id"), nullable=False)
    # Per-tenant feature settings, one key per feature (`{"time_tracking": {...}}`), the
    # document docs/design/schema.sql calls `workspaces.settings`. Nullable with the default
    # applied in code because MySQL cannot give a JSON column a literal DEFAULT. The column
    # does not constrain its contents, so every write goes through a pydantic model first
    # (schemas/time_tracking.TimeTrackingSettings) and every read through the same model.
    settings = Column(JSON, nullable=True)
    created_at = Column(DateTime, default=_now, nullable=False)
    updated_at = Column(DateTime, default=_now, onupdate=_now, nullable=False)
    deleted = Column(Integer, nullable=False, default=0)
    deleted_at = Column(DateTime, nullable=True)


class WorkspaceMember(Base):
    __tablename__ = "workspace_members"
    __table_args__ = (
        UniqueConstraint("workspace_id", "user_id", name="uq_workspace_members_workspace_user"),
    )

    id = Column(CHAR(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    workspace_id = Column(CHAR(36), ForeignKey("workspaces.id"), nullable=False)
    # Indexed on its own as well as through the unique pair: "which workspaces am I in" is
    # the first query of every session and it leads with user_id.
    user_id = Column(CHAR(36), ForeignKey("users.id"), nullable=False, index=True)
    role = Column(String(20), nullable=False, default="member")
    created_at = Column(DateTime, default=_now, nullable=False)
