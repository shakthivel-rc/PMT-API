"""Saved board filters — the transaction boundary.

| Act | guest | member | admin / owner |
|---|---|---|---|
| See their own views, and every view shared with the workspace | yes | yes | yes |
| Save a view | — | yes | yes |
| Change a view | — | their own | their own |
| Delete a view | — | their own | their own, and any shared one |

Guests read and do not save: a guest is read-only everywhere else in a workspace, and a shared
view lands in every member's picker.

Someone else's private view is a 404 for every act, never a 403 (services/saved_views.py says
why). A shared view the caller can see but may not change is a 403: they can see it, so there
is nothing left to disclose, and "you can see this but not change it" is the useful answer.

An admin deleting someone else's shared view goes to the audit trail (`activity_logs`); a
person deleting their own does not. That is the line the time-tracking controller draws for
time entries: an administrative act on somebody else's thing is audited, a person's act on
their own is not.
"""
from __future__ import annotations

from fastapi import Request
from sqlalchemy.orm import Session

from controllers.workspace_controller import _audit
from dependencies.workspace import WorkspaceAccess
from models.saved_view import SavedView
from schemas.response import SuccessResponse, error_json
from schemas.saved_views import SavedViewCreate, SavedViewUpdate, scope_problem
from services import saved_views as svc
from utils import time_tracking as tt

_NOT_FOUND = "Saved view not found"
_SCOPE_NOT_FOUND = {"list": "List not found", "space": "Space not found",
                    "sprint": "Sprint not found"}


def _payload(db: Session, access: WorkspaceAccess, view: SavedView) -> dict:
    return svc.payloads(db, access, [view])[0]


def list_views(access: WorkspaceAccess, db: Session, *, scope_kind: str | None,
               scope_id: str | None, view_type: str | None):
    views = svc.list_visible(db, access, scope_kind=scope_kind, scope_id=scope_id,
                             view_type=view_type)
    return SuccessResponse(status_code=200, message="Saved views fetched",
                           data=svc.payloads(db, access, views))


def get_view(access: WorkspaceAccess, view_id: str, db: Session):
    view = svc.get_visible(db, access, view_id)
    if view is None:
        return error_json(404, _NOT_FOUND)
    return SuccessResponse(status_code=200, message="Saved view fetched",
                           data=_payload(db, access, view))


def create_view(access: WorkspaceAccess, body: SavedViewCreate, db: Session):
    if not svc.scope_exists(db, access.workspace_id, body.scope_kind, body.scope_id):
        return error_json(404, _SCOPE_NOT_FOUND[body.scope_kind])
    view = SavedView(workspace_id=access.workspace_id, owner_id=access.user_id,
                     name=body.name, view_type=body.view_type, scope_kind=body.scope_kind,
                     scope_id=body.scope_id, filters=svc.stored_filters(body.filters),
                     visibility=body.visibility)
    db.add(view)
    db.commit()
    return SuccessResponse(status_code=201, message="View saved",
                           data=_payload(db, access, view))


def update_view(access: WorkspaceAccess, view_id: str, body: SavedViewUpdate, db: Session):
    view = svc.get_visible(db, access, view_id)
    if view is None:
        return error_json(404, _NOT_FOUND)
    if not svc.can_edit(access, view):
        return error_json(403, "Only the person who saved this view can change it.")
    changes = body.model_fields_set
    for field in changes:
        if getattr(body, field) is None:
            return error_json(422, f"{field} cannot be empty.")
    view_type = body.view_type if body.view_type is not None else str(view.view_type)
    scope_kind = body.scope_kind if body.scope_kind is not None else str(view.scope_kind)
    problem = scope_problem(view_type, scope_kind)
    if problem:
        return error_json(422, problem)
    if body.scope_kind is not None and body.scope_id is not None and not svc.scope_exists(
            db, access.workspace_id, body.scope_kind, body.scope_id):
        return error_json(404, _SCOPE_NOT_FOUND[body.scope_kind])

    if body.name is not None:
        view.name = body.name
    if body.view_type is not None:
        view.view_type = body.view_type
    if body.scope_kind is not None and body.scope_id is not None:
        view.scope_kind, view.scope_id = body.scope_kind, body.scope_id
    if body.filters is not None:
        view.filters = svc.stored_filters(body.filters)
    if body.visibility is not None:
        view.visibility = body.visibility
    db.commit()
    return SuccessResponse(status_code=200, message="Saved view updated",
                           data=_payload(db, access, view))


def delete_view(access: WorkspaceAccess, view_id: str, db: Session, request: Request):
    view = svc.get_visible(db, access, view_id)
    if view is None:
        return error_json(404, _NOT_FOUND)
    if not svc.can_delete(access, view):
        return error_json(403, "Only the person who saved this view, or an admin when it is "
                               "shared with the workspace, can delete it.")
    svc.soft_delete(view, tt.utc_now())
    if view.owner_id != access.user_id:
        _audit(db, request, access.user_id, "DELETE", "saved_view", view.id,
               f"Deleted the shared view {view.name} saved by {view.owner_id}")
    db.commit()
    return SuccessResponse(status_code=200, message="Saved view deleted", data={"id": view_id})
