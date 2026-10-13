"""Typed dependencies — the transaction boundary for linking and unlinking issues.

| Act | guest | member | admin / owner |
|---|---|---|---|
| See an issue's dependencies | yes | yes | yes |
| Add or remove one | — | yes | yes |

Linking is everyday work, like commenting, so it is a member's act and is not audited: the
`dependency_added` / `dependency_removed` events on both issues already say who did what.

The order of refusals, and why:

1. **404** for the issue in the path, then for the target. An id or key that is not in the
   caller's workspace is the same answer as one that does not exist (CLAUDE.md §8): the
   target is looked up — or its key resolved — inside the caller's workspace only.
2. **423** when either issue's list, folder or space is archived or frozen
   (services/write_guard.py). A link is a write to both issues — both histories change — so
   both are asked, the issue in the path first, because that is the one the person is
   looking at.
3. **422** for an issue linked to itself; **409** for a dependency that already exists,
   which for `relates` includes the same pair the other way round.
"""
from __future__ import annotations

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from dependencies.workspace import WorkspaceAccess
from models.issue import Issue
from schemas.dependencies import DependencyCreate
from schemas.response import SuccessResponse, error_json
from services import dependencies as svc
from services import issue as issue_svc
from services import key_resolution, write_guard

_NO_ISSUE = "Issue not found"
_NO_TARGET = "The issue to link to was not found in this workspace."


def _target(db: Session, access: WorkspaceAccess, body: DependencyCreate) -> Issue | None:
    """The issue the body names, by id or by key — inside the caller's workspace only."""
    parts = body.key_parts()
    if parts is None:
        return issue_svc.get_issue(db, access.workspace_id, body.target_issue_id or "")
    found = key_resolution.resolve_key(db, access.workspace_id, *parts)
    return found[0] if found is not None else None


def list_dependencies(access: WorkspaceAccess, issue_id: str, db: Session):
    issue = issue_svc.get_issue(db, access.workspace_id, issue_id)
    if issue is None:
        return error_json(404, _NO_ISSUE)
    links = svc.links_of(db, access.workspace_id, str(issue.id))
    return SuccessResponse(status_code=200, message="Dependencies fetched",
                           data=svc.payloads(db, access.workspace_id, str(issue.id), links))


def create_dependency(access: WorkspaceAccess, issue_id: str, body: DependencyCreate,
                      db: Session):
    source = issue_svc.get_issue(db, access.workspace_id, issue_id)
    if source is None:
        return error_json(404, _NO_ISSUE)
    block = write_guard.issue_block(db, source)
    if block is not None:
        return block.response()
    target = _target(db, access, body)
    if target is None:
        return error_json(404, _NO_TARGET)
    if target.id == source.id:
        return error_json(422, "An issue cannot depend on itself.")
    block = write_guard.issue_block(db, target)
    if block is not None:
        return block.response()

    svc.lock_pair(db, access.workspace_id, str(source.id), str(target.id))
    existing = svc.existing_link(db, access.workspace_id, str(source.id), str(target.id),
                                 body.kind)
    if existing is not None:
        # Read before the rollback releases the lock: a rollback expires every loaded row.
        message = (f"{key_resolution.current_key(db, source)} already "
                   f"{svc.label(body.kind, svc.OUTWARD)} "
                   f"{key_resolution.current_key(db, target)}.")
        existing_id = existing.id
        db.rollback()
        return error_json(409, message, {"dependency_id": existing_id})
    try:
        link = svc.create_link(db, source, target, body.kind, access.user_id)
        db.commit()
    except IntegrityError:
        # `uq_issue_links_pair_kind`: the same link, added by someone else in between.
        db.rollback()
        return error_json(409, "That dependency was added by someone else at the same "
                               "moment. Reload the issue to see it.")
    return SuccessResponse(status_code=201, message="Dependency added",
                           data=svc.payloads(db, access.workspace_id, str(source.id),
                                             [link])[0])


def delete_dependency(access: WorkspaceAccess, dependency_id: str, db: Session):
    link = svc.get_link(db, access.workspace_id, dependency_id)
    if link is None:
        return error_json(404, "Dependency not found")
    source, target = svc.link_ends(db, link)
    for issue in (source, target):
        block = write_guard.issue_block(db, issue)
        if block is not None:
            return block.response()
    svc.remove_link(db, link, source, target, access.user_id)
    db.commit()
    return SuccessResponse(status_code=200, message="Dependency removed",
                           data={"id": dependency_id})
