"""Saved board filters: storage, visibility, and the payloads the API returns.

docs/design/09-restructuring.md §5.4. A saved view is a name, a scope (a list, a space or a
sprint), a view type and a `ViewFilters` document (schemas/saved_views.py). Who sees and
changes one, and why:

* **A `private` view is its owner's alone.** Anyone else gets 404 for it — an admin and an
  owner of the workspace included — never 403: a 403 would confirm that a view with that id
  exists and belongs to someone (CLAUDE.md §8), and a personal filter is nobody else's
  business.
* **A `workspace` view is seen by every member, guests included.** It is how a team shares
  the board as it looks at it in stand-up.
* **Only its owner changes a view.** An edit to a shared view changes what everyone sees
  under the owner's name, so it is the owner's to make.
* **Its owner deletes it — or an admin, when it is shared.** A stale shared view is clutter in
  everyone's picker and its owner may have left the team. An admin cannot see a private view,
  so cannot delete one either.

The shape is saved reports' (services/reports/saved.py), with a narrower edit rule — there an
admin may change anyone's report; §5.4 gives a view's edits to its owner alone.
"""
from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime

from sqlalchemy import func, or_
from sqlalchemy.orm import Query, Session

from dependencies.workspace import WorkspaceAccess
from models.hierarchy import ProjectList, Space
from models.saved_view import SavedView
from models.sprint import Sprint
from schemas.saved_views import ViewFilters
from services import issue as issue_svc

#: What each scope kind names, and so where its existence and its name are looked up.
_SCOPE_MODELS = {"list": ProjectList, "space": Space, "sprint": Sprint}


def visible_query(db: Session, access: WorkspaceAccess) -> Query:
    """The views `access` may see: its own, and every one shared with the workspace."""
    return (db.query(SavedView)
            .filter(SavedView.workspace_id == access.workspace_id, SavedView.deleted == 0,
                    or_(SavedView.owner_id == access.user_id,
                        SavedView.visibility == "workspace")))


def get_visible(db: Session, access: WorkspaceAccess, view_id: str) -> SavedView | None:
    """The view, if the caller may see it. None — answered 404 — for one that does not exist,
    is deleted, is another workspace's, or is someone else's private view: all four are the
    same answer on purpose."""
    return visible_query(db, access).filter(SavedView.id == view_id).first()


def can_edit(access: WorkspaceAccess, view: SavedView) -> bool:
    return view.owner_id == access.user_id


def can_delete(access: WorkspaceAccess, view: SavedView) -> bool:
    return can_edit(access, view) or (view.visibility == "workspace"
                                      and access.at_least("admin"))


def filters_of(view: SavedView) -> ViewFilters:
    """The stored document, read through the model. One that no longer validates — written
    before a filter's rules tightened — reads as no filters rather than failing every
    listing of the workspace's views; the next save writes a valid one."""
    try:
        return ViewFilters.model_validate(view.filters or {})
    except ValueError:
        return ViewFilters()


def stored_filters(filters: ViewFilters) -> dict:
    """What the column holds: the non-default fields only, so a filter added later needs no
    backfill — reading fills in its default."""
    return filters.model_dump(mode="json", exclude_defaults=True)


def scope_exists(db: Session, workspace_id: str, scope_kind: str, scope_id: str) -> bool:
    model = _SCOPE_MODELS[scope_kind]
    return (db.query(model.id)
            .filter(model.workspace_id == workspace_id, model.id == scope_id,
                    model.deleted == 0)
            .first()) is not None


def _scope_names(db: Session, workspace_id: str,
                 views: Iterable[SavedView]) -> dict[tuple[str, str], str]:
    """The current name of every scope the views point at: at most one query per kind."""
    wanted: dict[str, set[str]] = {}
    for view in views:
        wanted.setdefault(str(view.scope_kind), set()).add(str(view.scope_id))
    names: dict[tuple[str, str], str] = {}
    for kind, ids in wanted.items():
        model = _SCOPE_MODELS.get(kind)
        if model is None:
            continue
        rows = (db.query(model.id, model.name)
                .filter(model.workspace_id == workspace_id, model.id.in_(ids),
                        model.deleted == 0)
                .all())
        names.update({(kind, scope_id): name for scope_id, name in rows})
    return names


def payloads(db: Session, access: WorkspaceAccess, views: list[SavedView]) -> list[dict]:
    """`SavedViewSummary` payloads: the owners and the scope names in a fixed number of
    queries (one for the owners, one per scope kind present), whatever the number of views."""
    owners = issue_svc.users_by_id(db, (view.owner_id for view in views))
    names = _scope_names(db, access.workspace_id, views)
    return [
        {
            "id": view.id, "name": view.name, "view_type": view.view_type,
            "scope_kind": view.scope_kind, "scope_id": view.scope_id,
            "scope_name": names.get((str(view.scope_kind), str(view.scope_id))),
            "filters": filters_of(view).model_dump(mode="json"),
            "visibility": view.visibility,
            "owner": (issue_svc.user_ref(owners.get(view.owner_id))
                      or {"id": view.owner_id, "display_name": ""}),
            "can_edit": can_edit(access, view), "can_delete": can_delete(access, view),
            "created_at": view.created_at, "updated_at": view.updated_at,
        }
        for view in views
    ]


def list_visible(db: Session, access: WorkspaceAccess, *, scope_kind: str | None = None,
                 scope_id: str | None = None, view_type: str | None = None) -> list[SavedView]:
    """The views the caller may see, alphabetically, optionally of one scope or type — what
    a board's view picker asks for."""
    query = visible_query(db, access)
    if scope_kind is not None:
        query = query.filter(SavedView.scope_kind == scope_kind)
    if scope_id is not None:
        query = query.filter(SavedView.scope_id == scope_id)
    if view_type is not None:
        query = query.filter(SavedView.view_type == view_type)
    return query.order_by(func.lower(SavedView.name), SavedView.id).all()


def soft_delete(view: SavedView, now: datetime) -> None:
    view.deleted = 1
    view.deleted_at = now
