"""Releases and fix versions — the transaction boundary for both.

| Act | guest | member | admin / owner |
|---|---|---|---|
| See releases, their issues, progress and notes; an issue's fix versions | yes | yes | yes |
| Set an issue's fix versions | — | yes | yes |
| Create, change, release or delete a release | — | — | yes |

The line is where 09-restructuring.md §10 draws it. A release is a promise a delivery unit
makes to the people it ships to, and releasing one can move other people's issues, so
managing releases is an admin's act and is audited (`activity_logs`), the way starting and
closing a sprint is. Choosing which release an issue ships in is everyday planning, like
putting it in a sprint, and its events are its record.

Writes go through the write guard (services/write_guard.py): a release write in an archived or
frozen space is a 423, and so is a change to the fix versions of an issue whose list, folder
or space is. Reads are never refused.

Anything of another workspace — a space, a release, an issue — is a 404, the tenancy rule of
every lookup here (CLAUDE.md §8). A release *named in a body* that is not one this issue may
take is a 422 whichever the reason, so the answer says nothing about other tenants either.
"""
from __future__ import annotations

from datetime import date

from fastapi import Request
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from controllers.workspace_controller import _audit
from dependencies.workspace import WorkspaceAccess
from models.release import Release
from schemas.releases import FixVersionsSet, ReleaseCreate, ReleaseShip, ReleaseUpdate
from schemas.response import SuccessResponse, error_json
from services import issue as issue_svc
from services import releases as svc
from services import workspace as workspace_svc
from services import write_guard
from services.reports import settings as report_settings
from utils import report_math as rm
from utils import time_tracking as tt

_NO_RELEASE = "Release not found"
_NO_ISSUE = "Issue not found"


def _today(db: Session, access: WorkspaceAccess) -> date:
    """Today in the workspace's zone — what "released today" means to the people in it."""
    zone_name = report_settings.load_settings(db, access.workspace_id).timezone
    return rm.local_today(tt.utc_now(), rm.zone(zone_name))


def _summary(db: Session, release: Release) -> dict:
    return svc.payloads(db, [release])[0]


def _taken(name: str) -> str:
    return (f"This space already has a release called {name}. Release names are unique in a "
            "space, ignoring case.")


def list_releases(access: WorkspaceAccess, space_id: str, state: str | None, db: Session):
    space = workspace_svc.get_space(db, access.workspace_id, space_id)
    if space is None:
        return error_json(404, "Space not found")
    states = [s for s in (state or "").split(",") if s] or None
    releases = svc.space_releases(db, access.workspace_id, str(space.id), states)
    return SuccessResponse(status_code=200, message="Releases fetched",
                           data=svc.payloads(db, releases))


def create_release(access: WorkspaceAccess, space_id: str, body: ReleaseCreate, db: Session,
                   request: Request):
    space = workspace_svc.get_space(db, access.workspace_id, space_id)
    if space is None:
        return error_json(404, "Space not found")
    block = write_guard.space_block(space)
    if block is not None:
        return block.response()
    svc.lock_space(db, access.workspace_id, str(space.id))
    if svc.name_taken(db, access.workspace_id, str(space.id), body.name):
        db.rollback()
        return error_json(409, _taken(body.name))
    release = Release(workspace_id=access.workspace_id, space_id=space.id, name=body.name,
                      description=body.description, start_date=body.start_date,
                      release_date=body.release_date, state="unreleased",
                      created_by=access.user_id)
    db.add(release)
    db.flush()
    _audit(db, request, access.user_id, "CREATE", "release", release.id,
           f"Created release {release.name} in space {space.key}")
    db.commit()
    return SuccessResponse(status_code=201, message="Release created",
                           data=_summary(db, release))


def get_release(access: WorkspaceAccess, release_id: str, db: Session):
    release = svc.get_release(db, access.workspace_id, release_id)
    if release is None:
        return error_json(404, _NO_RELEASE)
    return SuccessResponse(status_code=200, message="Release fetched",
                           data=svc.detail(db, release))


def _state_problem(release: Release, target: str) -> str | None:
    """Why `release` cannot be moved to `target` by an edit. Only one move is refused:
    into `released` for a release that never shipped — releasing has its own route,
    because it sets the date and can move the open issues on."""
    if target == "released" and release.released_at is None:
        return ("Release it with POST …/releases/{id}/release, which sets its date and can "
                "move its open issues to another release.")
    return None


def _apply_state(release: Release, target: str) -> None:
    if target == "unreleased":
        # Undoing a release (or un-archiving one that never shipped): it has not shipped.
        release.released_at = None
    release.state = target


def update_release(access: WorkspaceAccess, release_id: str, body: ReleaseUpdate, db: Session,
                   request: Request):
    release = svc.get_release(db, access.workspace_id, release_id)
    if release is None:
        return error_json(404, _NO_RELEASE)
    block = write_guard.space_id_block(db, access.workspace_id, str(release.space_id))
    if block is not None:
        return block.response()
    changes = body.model_fields_set
    for field in ("name", "state"):
        if field in changes and getattr(body, field) is None:
            return error_json(422, f"{field} cannot be empty.")
    start = body.start_date if "start_date" in changes else release.start_date
    end = body.release_date if "release_date" in changes else release.release_date
    if start and end and end < start:
        return error_json(422, "release_date is before start_date")
    if "state" in changes and body.state is not None:
        problem = _state_problem(release, body.state)
        if problem:
            return error_json(409, problem)
    if "name" in changes and body.name is not None and body.name != release.name:
        svc.lock_space(db, access.workspace_id, str(release.space_id))
        if svc.name_taken(db, access.workspace_id, str(release.space_id), body.name,
                          except_id=str(release.id)):
            db.rollback()
            return error_json(409, _taken(body.name))

    changed = []
    for field in ("name", "description", "start_date", "release_date"):
        if field in changes and getattr(body, field) != getattr(release, field):
            setattr(release, field, getattr(body, field))
            changed.append(field)
    if "state" in changes and body.state is not None and body.state != release.state:
        changed.append(f"state {release.state} -> {body.state}")
        _apply_state(release, body.state)
    if changed:
        _audit(db, request, access.user_id, "UPDATE", "release", release.id,
               f"Changed release {release.name}: {', '.join(changed)}")
    db.commit()
    return SuccessResponse(status_code=200, message="Release updated",
                           data=_summary(db, release))


def delete_release(access: WorkspaceAccess, release_id: str, db: Session, request: Request):
    release = svc.get_release(db, access.workspace_id, release_id)
    if release is None:
        return error_json(404, _NO_RELEASE)
    block = write_guard.space_id_block(db, access.workspace_id, str(release.space_id))
    if block is not None:
        return block.response()
    if release.state == "released" or release.released_at is not None:
        # Including one archived after it shipped: what a space shipped is history, and its
        # notes and fix versions must keep resolving.
        return error_json(409, "This release has shipped, so it is kept as history. Archive "
                               "it instead.")
    release.deleted = 1
    release.deleted_at = tt.utc_now()
    _audit(db, request, access.user_id, "DELETE", "release", release.id,
           f"Deleted release {release.name}")
    db.commit()
    return SuccessResponse(status_code=200, message="Release deleted", data={"id": release_id})


def ship_release(access: WorkspaceAccess, release_id: str, body: ReleaseShip, db: Session,
                 request: Request):
    release = svc.get_release(db, access.workspace_id, release_id, for_update=True)
    if release is None:
        return error_json(404, _NO_RELEASE)
    block = write_guard.space_id_block(db, access.workspace_id, str(release.space_id))
    if block is not None:
        db.rollback()
        return block.response()
    if release.state != "unreleased":
        db.rollback()
        return error_json(409, f"{release.name} is {release.state}; only an unreleased "
                               "release can be released.")
    release_date = body.release_date or _today(db, access)
    if release.start_date and release_date < release.start_date:
        db.rollback()
        return error_json(422, "The release date is before the release's start date.")

    target = None
    if body.move_open_to_release_id is not None:
        if body.move_open_to_release_id == release.id:
            db.rollback()
            return error_json(422, "Open issues cannot move to the release being released.")
        target = svc.get_release(db, access.workspace_id, body.move_open_to_release_id)
        if target is None or target.space_id != release.space_id:
            db.rollback()
            return error_json(404, "Target release not found in this space")
        if target.state != "unreleased":
            db.rollback()
            return error_json(409, "Open issues can only move to an unreleased release.")
    move = svc.open_issues(db, release) if target is not None else []
    # Moving an issue's fix version is a write to that issue: each list holding one is asked,
    # once, and one read-only list refuses the whole release rather than shipping half a move.
    for list_id in sorted({str(issue.list_id) for issue in move}):
        block = write_guard.list_id_block(db, access.workspace_id, list_id)
        if block is not None:
            db.rollback()
            return block.response()

    moved = svc.ship(db, release, release_date=release_date, now=tt.utc_now(), move=move,
                     to=target, actor_id=access.user_id)
    moved_to = f"; {len(moved)} open issue(s) moved to {target.name}" if target else ""
    _audit(db, request, access.user_id, "UPDATE", "release", release.id,
           f"Released {release.name} on {release_date.isoformat()}{moved_to}")
    db.commit()
    return SuccessResponse(status_code=200, message="Release released",
                           data={"release": _summary(db, release), "moved_issue_ids": moved,
                                 "moved_to": _summary(db, target) if target else None})


def release_notes(access: WorkspaceAccess, release_id: str, db: Session):
    release = svc.get_release(db, access.workspace_id, release_id)
    if release is None:
        return error_json(404, _NO_RELEASE)
    return SuccessResponse(status_code=200, message="Release notes generated",
                           data=svc.notes(db, release))


def list_fix_versions(access: WorkspaceAccess, issue_id: str, db: Session):
    issue = issue_svc.get_issue(db, access.workspace_id, issue_id)
    if issue is None:
        return error_json(404, _NO_ISSUE)
    return SuccessResponse(status_code=200, message="Fix versions fetched",
                           data=[svc.ref_payload(r) for r in
                                 svc.fix_versions_of(db, access.workspace_id, str(issue.id))])


def set_fix_versions(access: WorkspaceAccess, issue_id: str, body: FixVersionsSet,
                     db: Session):
    issue = issue_svc.get_issue(db, access.workspace_id, issue_id)
    if issue is None:
        return error_json(404, _NO_ISSUE)
    block = write_guard.issue_block(db, issue)
    if block is not None:
        return block.response()
    wanted = list(dict.fromkeys(body.release_ids))
    current = svc.fix_versions_of(db, access.workspace_id, str(issue.id))
    have = {release.id for release in current}
    adding = svc.releases_by_id(db, access.workspace_id,
                                [release_id for release_id in wanted if release_id not in have])
    for release_id in wanted:
        if release_id in have:
            continue
        release = adding.get(release_id)
        if release is None or release.space_id != issue.space_id:
            return error_json(422, "Fix versions must be releases of the issue's own space.",
                              {"release_id": release_id})
        if release.state == "archived":
            return error_json(422, f"{release.name} is archived, so it takes no new issues.",
                              {"release_id": release_id})
    try:
        svc.replace_fix_versions(db, issue, wanted, current, adding, access.user_id)
        db.commit()
    except IntegrityError:
        # `uq_issue_fix_versions_pair`: someone else set the same fix version in between.
        db.rollback()
        return error_json(409, "Someone else changed this issue's fix versions at the same "
                               "moment. Reload it and try again.")
    return SuccessResponse(status_code=200, message="Fix versions updated",
                           data=[svc.ref_payload(r) for r in
                                 svc.fix_versions_of(db, access.workspace_id, str(issue.id))])
