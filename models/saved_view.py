"""Saved board filters: a named scope, view and filter set (09-restructuring.md §5.4).

The board, the list view and the backlog share one filter vocabulary (`schemas/
saved_views.ViewFilters`), so a saved view is a scope plus that document — not a query
language, which is Phase 2's PQL (docs/design/04-api-and-views.md §4.4); when PQL lands, a
saved view gains a `pql` column and these filters become its simple form.
"""
import uuid
from datetime import UTC, datetime

from sqlalchemy import CHAR, JSON, Column, DateTime, ForeignKey, Index, Integer, String

from db.session import Base

SAVED_VIEW_TYPES: tuple[str, ...] = ("board", "list", "backlog")
SAVED_VIEW_SCOPES: tuple[str, ...] = ("list", "space", "sprint")
SAVED_VIEW_VISIBILITY: tuple[str, ...] = ("private", "workspace")


def _now() -> datetime:
    return datetime.now(UTC)


class SavedView(Base):
    __tablename__ = "saved_views"
    __table_args__ = (Index("ix_saved_views_workspace_owner", "workspace_id", "owner_id"),)

    id = Column(CHAR(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    workspace_id = Column(CHAR(36), ForeignKey("workspaces.id"), nullable=False)
    owner_id = Column(CHAR(36), ForeignKey("users.id"), nullable=False)
    name = Column(String(120), nullable=False)
    view_type = Column(String(20), nullable=False, default="board")
    scope_kind = Column(String(20), nullable=False)
    scope_id = Column(CHAR(36), nullable=False)
    filters = Column(JSON, nullable=True)
    visibility = Column(String(20), nullable=False, default="private")
    created_at = Column(DateTime, default=_now, nullable=False)
    updated_at = Column(DateTime, default=_now, onupdate=_now, nullable=False)
    deleted = Column(Integer, nullable=False, default=0)
    deleted_at = Column(DateTime, nullable=True)
