"""Release and fix-version routes (docs/design/09-restructuring.md §10).

Thin, like every route module (CLAUDE.md §2). Releases belong to a space, the way sprints do:
listed and created under `/spaces/{space_id}`, read and changed under `/releases/{id}`. An
issue's fix versions hang off the issue.
"""
from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy.orm import Session

from controllers import release_controller as ctl
from db.session import get_db
from dependencies.workspace import WorkspaceAccess, as_admin, as_guest, as_member

# Registers the tables on the metadata until the services behind these routes import them.
from models import release as _release  # noqa: F401
from routes.dependencies import LOCKED, NO_ISSUE
from schemas.errors import COMMON_ERRORS, VALIDATION_ERROR
from schemas.releases import FixVersionsSet, ReleaseCreate, ReleaseShip, ReleaseUpdate
from schemas.response import ErrorResponse, SuccessResponse
from schemas.responses.releases import (
    FixVersion,
    ReleaseDetail,
    ReleaseNotes,
    ReleaseOutcome,
    ReleaseSummary,
)

router = APIRouter(prefix="/workspaces/{workspace_id}", tags=["Releases"])

NO_SPACE = {404: {"model": ErrorResponse,
                  "description": "No such workspace or space, or a space of another "
                                 "workspace."}}
NO_RELEASE = {404: {"model": ErrorResponse,
                    "description": "No such workspace or release, a deleted release, or one "
                                   "of another workspace."}}
NAME_TAKEN = {409: {"model": ErrorResponse,
                    "description": "The space already has a release by that name, ignoring "
                                   "case."}}
BAD_DATES = {422: {"model": ErrorResponse,
                   "description": "A value did not validate, or `release_date` is before "
                                  "`start_date`."}}
#: `state` filter: a comma-separated list of release states.
_STATES = r"^(unreleased|released|archived)(,(unreleased|released|archived))*$"


@router.get(
    "/spaces/{space_id}/releases",
    response_model=SuccessResponse[list[ReleaseSummary]],
    responses={**COMMON_ERRORS, **VALIDATION_ERROR, **NO_SPACE},
    summary="List a space's releases with their progress",
    operation_id="list_releases",
)
def list_releases(space_id: str,
                  state: str | None = Query(None, max_length=40, pattern=_STATES,
                                            description="Comma-separated: unreleased, "
                                                        "released, archived. Default: all."),
                  access: WorkspaceAccess = Depends(as_guest), db: Session = Depends(get_db)):
    """Unreleased first, soonest planned first; then released and archived, newest first.
    Each with its issues counted by outcome, in one grouped query for the whole list."""
    return ctl.list_releases(access, space_id, state, db)


@router.post(
    "/spaces/{space_id}/releases",
    response_model=SuccessResponse[ReleaseSummary],
    status_code=201,
    responses={**COMMON_ERRORS, **NO_SPACE, **NAME_TAKEN, **BAD_DATES, **LOCKED},
    summary="Create a release",
    operation_id="create_release",
)
def create_release(space_id: str, body: ReleaseCreate, request: Request,
                   access: WorkspaceAccess = Depends(as_admin), db: Session = Depends(get_db)):
    """Unreleased, with no issues yet. Admins only; audited."""
    return ctl.create_release(access, space_id, body, db, request)


@router.get(
    "/releases/{release_id}",
    response_model=SuccessResponse[ReleaseDetail],
    responses={**COMMON_ERRORS, **NO_RELEASE},
    summary="Get a release with its issues",
    operation_id="get_release",
)
def get_release(release_id: str, access: WorkspaceAccess = Depends(as_guest),
                db: Session = Depends(get_db)):
    """Its issues as board cards, in key order, and its progress."""
    return ctl.get_release(access, release_id, db)


@router.patch(
    "/releases/{release_id}",
    response_model=SuccessResponse[ReleaseSummary],
    responses={**COMMON_ERRORS, **NO_RELEASE, **BAD_DATES, **LOCKED,
               409: {"model": ErrorResponse,
                     "description": "The name is taken in the space, or `state: released` "
                                    "for a release that never shipped (use "
                                    "`POST …/release`)."}},
    summary="Rename, re-date, archive or un-release a release",
    operation_id="update_release",
)
def update_release(release_id: str, body: ReleaseUpdate, request: Request,
                   access: WorkspaceAccess = Depends(as_admin), db: Session = Depends(get_db)):
    """Only the fields present change. Admins only; audited."""
    return ctl.update_release(access, release_id, body, db, request)


@router.delete(
    "/releases/{release_id}",
    response_model=SuccessResponse[dict],
    responses={**COMMON_ERRORS, **NO_RELEASE, **LOCKED,
               409: {"model": ErrorResponse,
                     "description": "It has shipped. A shipped release is history: archive "
                                    "it instead."}},
    summary="Delete a release that has not shipped",
    operation_id="delete_release",
)
def delete_release(release_id: str, request: Request,
                   access: WorkspaceAccess = Depends(as_admin), db: Session = Depends(get_db)):
    """A soft delete: it disappears from the space and from its issues' fix versions.
    Admins only; audited."""
    return ctl.delete_release(access, release_id, db, request)


@router.post(
    "/releases/{release_id}/release",
    response_model=SuccessResponse[ReleaseOutcome],
    responses={**COMMON_ERRORS, **LOCKED,
               404: {"model": ErrorResponse,
                     "description": "No such workspace or release, or a target release that "
                                    "is not of the same space."},
               409: {"model": ErrorResponse,
                     "description": "It is not unreleased, or the target release is not "
                                    "unreleased."},
               422: {"model": ErrorResponse,
                     "description": "A value did not validate, the release date is before the "
                                    "start date, or the target is the release itself."}},
    summary="Release a release, optionally moving its open issues to another",
    operation_id="ship_release",
)
def ship_release(release_id: str, body: ReleaseShip, request: Request,
                 access: WorkspaceAccess = Depends(as_admin), db: Session = Depends(get_db)):
    """Marks it released (`released_at` now). With `move_open_to_release_id`, every issue of
    it in an open stage moves to that release instead — done and canceled ones stay, so the
    release keeps what it delivered. Admins only; audited."""
    return ctl.ship_release(access, release_id, body, db, request)


@router.get(
    "/releases/{release_id}/notes",
    response_model=SuccessResponse[ReleaseNotes],
    responses={**COMMON_ERRORS, **NO_RELEASE},
    summary="Generate a release's notes",
    operation_id="get_release_notes",
)
def release_notes(release_id: str, access: WorkspaceAccess = Depends(as_guest),
                  db: Session = Depends(get_db)):
    """Delivered work by issue type, then canceled and still-open work in their own sections —
    as plain text and as structure. Generated from the issues on every call, never stored."""
    return ctl.release_notes(access, release_id, db)


@router.get(
    "/issues/{issue_id}/fix-versions",
    response_model=SuccessResponse[list[FixVersion]],
    responses={**COMMON_ERRORS, **NO_ISSUE},
    summary="List an issue's fix versions",
    operation_id="list_issue_fix_versions",
)
def list_fix_versions(issue_id: str, access: WorkspaceAccess = Depends(as_guest),
                      db: Session = Depends(get_db)):
    """The releases it ships in, by release date. Archived ones are included."""
    return ctl.list_fix_versions(access, issue_id, db)


@router.put(
    "/issues/{issue_id}/fix-versions",
    response_model=SuccessResponse[list[FixVersion]],
    responses={**COMMON_ERRORS, **NO_ISSUE, **LOCKED,
               409: {"model": ErrorResponse,
                     "description": "Someone else changed its fix versions at the same "
                                    "moment."},
               422: {"model": ErrorResponse,
                     "description": "More than 20 releases, or one being added that is not a "
                                    "live, unarchived release of the issue's space. "
                                    "`data.release_id` names it."}},
    summary="Replace an issue's fix versions",
    operation_id="set_issue_fix_versions",
)
def set_fix_versions(issue_id: str, body: FixVersionsSet,
                     access: WorkspaceAccess = Depends(as_member),
                     db: Session = Depends(get_db)):
    """The set becomes exactly `release_ids`, recording `fix_version_added` and
    `fix_version_removed` for each difference. A release it already has may stay even if
    archived — leave it out and it is removed."""
    return ctl.set_fix_versions(access, issue_id, body, db)
