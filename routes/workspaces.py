"""Tenancy and hierarchy routes. Thin: path, contract, the tenancy gate, a controller call.

Every route under `/workspaces/{workspace_id}` declares `workspace_access(<min role>)`, and
that dependency — not the handler — is what turns a non-member into a 404.

Since 2026-10-05 (docs/design/09-restructuring.md §3): the space charter and governance
settings, renaming and reparenting containers, and the write states — archive and unarchive
for spaces, folders and lists, freeze and unfreeze for spaces. Every structural change is an
admin's; reading a space is a guest's.

Since 2026-10-06: renaming a workspace (an admin's) and deleting it (an owner's), changing a
member's role (an admin's, with owners managing owners) and removing a member (an admin's —
or anyone's own, to leave). Deleting and removing gate on `as_guest` and refuse in the
controller, because the rule is not a minimum role: an admin may not delete, and anyone may
leave.
"""
from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from controllers import workspace_controller as ctl
from db.session import get_db
from dependencies.workspace import WorkspaceAccess, as_admin, as_guest, caller_id
from schemas.errors import COMMON_ERRORS, VALIDATION_ERROR
from schemas.governance import GovernanceSettings, GovernanceSettingsUpdate
from schemas.rates import WorkspaceRatesResponse, WorkspaceRatesUpdate
from schemas.response import ErrorResponse, SuccessResponse
from schemas.responses.workspaces import (
    FolderNode,
    ListNode,
    MemberSummary,
    RepositorySummary,
    SpaceDetail,
    SpaceNode,
    SpaceUnfreezeResult,
    WorkspaceSummary,
    WorkspaceTree,
)
from schemas.workspaces import (
    FolderCreate,
    FolderUpdate,
    ListCreate,
    ListUpdate,
    MemberAdd,
    MemberUpdate,
    RepositoryLink,
    SpaceCreate,
    SpaceFreeze,
    SpaceUpdate,
    WorkspaceCreate,
    WorkspaceUpdate,
)

router = APIRouter(prefix="/workspaces", tags=["Workspaces"])

#: Spread into every route that names a workspace. The wording is the point: it is the same
#: 404 whether the workspace does not exist or the caller is not in it (CLAUDE.md §8).
NOT_A_MEMBER = {404: {"model": ErrorResponse,
                      "description": "No such workspace, or you are not a member of it — "
                                     "deliberately indistinguishable."}}
CONFLICT = {409: {"model": ErrorResponse, "description": "It already exists."}}
#: The write guard's answer (services/write_guard.py, 09-restructuring.md §3.4). Declared
#: here for the hierarchy routes; any route that asks the guard can spread it.
LOCKED = {423: {"model": ErrorResponse,
                "description": "Locked: the container, or the folder or space it is in, is "
                               "archived (read-only until an admin unarchives it) or frozen "
                               "(read-only while it is being reorganised). The message names "
                               "which, and what lifts it."}}

NO_SPACE = {404: {"model": ErrorResponse,
                  "description": "No such workspace or space, or one in another workspace — "
                                 "deliberately indistinguishable."}}
NO_FOLDER = {404: {"model": ErrorResponse,
                   "description": "No such workspace or folder, or one in another workspace "
                                  "— deliberately indistinguishable."}}
NO_LIST = {404: {"model": ErrorResponse,
                 "description": "No such workspace, list or target folder, or one in another "
                                "workspace — deliberately indistinguishable."}}
SPACE_CONFLICT = {409: {"model": ErrorResponse,
                        "description": "The key is taken: by another space, or as the old "
                                       "key of one, which stays reserved so old issue keys "
                                       "keep resolving."}}
ARCHIVE_REFUSED = {409: {"model": ErrorResponse,
                         "description": "Not now, and the message says what to do first: it "
                                        "is already archived (or, to unarchive, is not); a "
                                        "space is frozen or has an active sprint; an issue "
                                        "of a folder or list is in a sprint that is not "
                                        "closed; a timer is running on one of its issues. "
                                        "`data` names the sprint or the issue keys."}}
FREEZE_REFUSED = {409: {"model": ErrorResponse,
                        "description": "Not now: freezing a space that is archived or already "
                                       "frozen; unfreezing one that is not frozen, or that a "
                                       "migration plan froze and is still running or has "
                                       "failed (`data` names the plan)."}}
#: The 422s, per route group. Each replaces VALIDATION_ERROR's description for its routes
#: (the same status), so each also says that a value which did not validate lands here.
SPACE_RULES = {422: {"model": ErrorResponse,
                     "description": "A value did not validate (`data.errors` is the pydantic "
                                    "list, as on every 422), or it is well-formed but "
                                    "against a rule: the owner is not a member; the workflow "
                                    "template does not exist here or cannot be applied; the "
                                    "workspace's governance settings require the creation "
                                    "checklist (`data.missing`, and `data.errors` in the "
                                    "same shape, name every missing item); a name or key "
                                    "cannot be cleared."}}
CONTAINER_RULES = {422: {"model": ErrorResponse,
                         "description": "A value did not validate (`data.errors`), a name or "
                                        "position was cleared, or a list was sent to a "
                                        "folder of another space — that is a transfer of "
                                        "its issues, not a list change."}}
GOVERNANCE_RULES = {422: {"model": ErrorResponse,
                          "description": "A value did not validate (`data.errors`) or was "
                                         "cleared, or `default_workflow_template` names no "
                                         "template here, or one that cannot be applied."}}

#: The workspace and member changes of 2026-10-06. The 403s replace COMMON_ERRORS' wording,
#: which is about platform permissions, with the workspace rule that actually refuses.
SLUG_TAKEN = {409: {"model": ErrorResponse,
                    "description": "The slug is taken — by another workspace, or by a "
                                   "deleted one: a deleted workspace keeps its address so "
                                   "that it can be restored."}}
OWNER_ONLY = {403: {"model": ErrorResponse,
                    "description": "Only an owner can delete a workspace; an admin is "
                                   "refused too."}}
DELETE_REFUSED = {409: {"model": ErrorResponse,
                        "description": "Not now, and the message says what to do first: a "
                                       "timer is running on one of its issues "
                                       "(`data.issue_keys`), or a migration plan is running "
                                       "in it (`data.plan_id`)."}}
NO_MEMBER = {404: {"model": ErrorResponse,
                   "description": "No such workspace, or you are not a member of it; or the "
                                  "person is not a member of it, which includes an account "
                                  "that does not exist or has been deleted."}}
ROLE_RULES = {403: {"model": ErrorResponse,
                    "description": "Your role here is below admin; or the change makes "
                                   "someone an owner or changes an owner's role, which only "
                                   "an owner may do."}}
LAST_OWNER = {409: {"model": ErrorResponse,
                    "description": "They are the workspace's last owner. Make someone else "
                                   "an owner first."}}
REMOVE_RULES = {403: {"model": ErrorResponse,
                      "description": "Removing someone else needs admin or owner — anyone "
                                     "may remove themselves — and removing an owner needs an "
                                     "owner."}}
REMOVAL_REFUSED = {409: {"model": ErrorResponse,
                         "description": "Not yet, and the message says what to change first, "
                                        "every reason at once: they are the last owner; they "
                                        "own spaces that are not archived (`data.spaces`); "
                                        "their timer is running here (`data.issue_keys`); "
                                        "report schedules would stop without them "
                                        "(`data.schedules`)."}}


@router.post(
    "",
    response_model=SuccessResponse[WorkspaceSummary],
    status_code=201,
    responses={**COMMON_ERRORS, **VALIDATION_ERROR, **CONFLICT},
    summary="Create a workspace; you become its owner",
    operation_id="create_workspace",
)
def create_workspace(body: WorkspaceCreate, request: Request, db: Session = Depends(get_db)):
    return ctl.create_workspace(body, caller_id(request), db, request)


@router.get(
    "",
    response_model=SuccessResponse[list[WorkspaceSummary]],
    responses={**COMMON_ERRORS},
    summary="List the workspaces you belong to",
    operation_id="list_workspaces",
)
def list_workspaces(request: Request, db: Session = Depends(get_db)):
    return ctl.list_workspaces(caller_id(request), db)


@router.get(
    "/{workspace_id}",
    response_model=SuccessResponse[WorkspaceTree],
    responses={**COMMON_ERRORS, **NOT_A_MEMBER},
    summary="Get a workspace with its whole hierarchy",
    operation_id="get_workspace",
)
def get_workspace(access: WorkspaceAccess = Depends(as_guest),
                  db: Session = Depends(get_db)):
    """Spaces, each with its workflow (stages and rules), folders and lists — one read for
    the sidebar. Archived containers are included and flagged; frozen spaces say why."""
    return ctl.get_workspace_tree(access, db)


@router.patch(
    "/{workspace_id}",
    response_model=SuccessResponse[WorkspaceSummary],
    responses={**COMMON_ERRORS, **VALIDATION_ERROR, **NOT_A_MEMBER, **SLUG_TAKEN},
    summary="Rename a workspace or change its address",
    operation_id="update_workspace",
)
def update_workspace(body: WorkspaceUpdate, request: Request,
                     access: WorkspaceAccess = Depends(as_admin),
                     db: Session = Depends(get_db)):
    """Admins and owners. Only the fields present change, and at least one must be; neither
    can be cleared. Answers with the workspace as the switcher lists it, with your role.
    Audited."""
    return ctl.update_workspace(access, body, db, request)


@router.delete(
    "/{workspace_id}",
    response_model=SuccessResponse[dict],
    responses={**COMMON_ERRORS, **NOT_A_MEMBER, **OWNER_ONLY, **DELETE_REFUSED},
    summary="Delete a workspace: gone from every list and link for everyone, history kept",
    operation_id="delete_workspace",
)
def delete_workspace(request: Request, access: WorkspaceAccess = Depends(as_guest),
                     db: Session = Depends(get_db)):
    """Owners only. A soft delete: its spaces, issues, time and history stay in the database
    and nothing in it changes, but every route under it answers 404 and it is in nobody's
    list; its slug stays taken. `data` is `{id}`. Audited."""
    return ctl.delete_workspace(access, db, request)


@router.get(
    "/{workspace_id}/members",
    response_model=SuccessResponse[list[MemberSummary]],
    responses={**COMMON_ERRORS, **NOT_A_MEMBER},
    summary="List a workspace's members",
    operation_id="list_workspace_members",
)
def list_members(access: WorkspaceAccess = Depends(as_guest),
                 db: Session = Depends(get_db)):
    return ctl.list_members(access, db)


@router.post(
    "/{workspace_id}/members",
    response_model=SuccessResponse[MemberSummary],
    status_code=201,
    responses={**COMMON_ERRORS, **VALIDATION_ERROR, **NOT_A_MEMBER, **CONFLICT},
    summary="Add an existing account to a workspace",
    operation_id="add_workspace_member",
)
def add_member(body: MemberAdd, request: Request,
               access: WorkspaceAccess = Depends(as_admin),
               db: Session = Depends(get_db)):
    """Admins and owners only. Only an owner can grant `owner`."""
    return ctl.add_member(access, body, db, request)


@router.patch(
    "/{workspace_id}/members/{user_id}",
    response_model=SuccessResponse[MemberSummary],
    responses={**COMMON_ERRORS, **VALIDATION_ERROR, **NO_MEMBER, **ROLE_RULES, **LAST_OWNER},
    summary="Give a member another role",
    operation_id="update_member",
)
def update_member(user_id: str, body: MemberUpdate, request: Request,
                  access: WorkspaceAccess = Depends(as_admin),
                  db: Session = Depends(get_db)):
    """Admins and owners. Only an owner makes someone an owner or changes an owner's role,
    and the last owner keeps the role. The role they already have changes nothing and is not
    audited; any other change is."""
    return ctl.update_member(access, user_id, body, db, request)


@router.delete(
    "/{workspace_id}/members/{user_id}",
    response_model=SuccessResponse[dict],
    responses={**COMMON_ERRORS, **NO_MEMBER, **REMOVE_RULES, **REMOVAL_REFUSED},
    summary="Remove a member from a workspace, or leave it yourself",
    operation_id="remove_member",
)
def remove_member(user_id: str, request: Request,
                  access: WorkspaceAccess = Depends(as_guest),
                  db: Session = Depends(get_db)):
    """Your own id leaves, whatever your role. Anyone else's needs admin or owner, and an
    owner's needs an owner. Refused while it would leave the workspace without an owner, or
    while they own spaces, have a timer running here or have report schedules that need
    them. What they did stays — assigned issues stay assigned. `data` is `{user_id}`.
    Audited."""
    return ctl.remove_member(access, user_id, db, request)


@router.get(
    "/{workspace_id}/settings/governance",
    response_model=SuccessResponse[GovernanceSettings],
    responses={**COMMON_ERRORS, **NOT_A_MEMBER},
    summary="Get the workspace's governance settings",
    operation_id="get_governance_settings",
)
def get_governance(access: WorkspaceAccess = Depends(as_guest),
                   db: Session = Depends(get_db)):
    """Whether new spaces need the creation checklist, whether new epics need an owner and
    completion criteria, the template new spaces start with, and when the inventory calls a
    container inactive."""
    return ctl.get_governance(access, db)


@router.patch(
    "/{workspace_id}/settings/governance",
    response_model=SuccessResponse[GovernanceSettings],
    responses={**COMMON_ERRORS, **VALIDATION_ERROR, **NOT_A_MEMBER, **GOVERNANCE_RULES},
    summary="Change the workspace's governance settings",
    operation_id="update_governance_settings",
)
def update_governance(body: GovernanceSettingsUpdate, request: Request,
                      access: WorkspaceAccess = Depends(as_admin),
                      db: Session = Depends(get_db)):
    """Admins. Only the fields present change. `default_workflow_template` must name a
    template that exists here and can be applied. Audited."""
    return ctl.update_governance(access, body, db, request)


@router.get(
    "/{workspace_id}/settings/rates",
    response_model=SuccessResponse[WorkspaceRatesResponse],
    responses={**COMMON_ERRORS, **NOT_A_MEMBER},
    summary="Get workspace financial currency and member hourly rates",
    operation_id="get_workspace_rates",
)
def get_rates(access: WorkspaceAccess = Depends(as_guest),
              db: Session = Depends(get_db)):
    """Currency, default rate, and member rate overrides for financial accounting."""
    return ctl.get_rates(access, db)


@router.put(
    "/{workspace_id}/settings/rates",
    response_model=SuccessResponse[WorkspaceRatesResponse],
    responses={**COMMON_ERRORS, **VALIDATION_ERROR, **NOT_A_MEMBER},
    summary="Update workspace currency, default rate, or member rate overrides",
    operation_id="update_workspace_rates",
)
def update_rates(body: WorkspaceRatesUpdate, request: Request,
                 access: WorkspaceAccess = Depends(as_admin),
                 db: Session = Depends(get_db)):
    """Admins. Update currency code, default billing rate, or member overrides. Audited."""
    return ctl.update_rates(access, body, db, request)


@router.post(
    "/{workspace_id}/spaces",
    response_model=SuccessResponse[SpaceNode],
    status_code=201,
    responses={**COMMON_ERRORS, **VALIDATION_ERROR, **NOT_A_MEMBER, **SPACE_CONFLICT,
               **SPACE_RULES},
    summary="Create a space from a workflow template, with its charter and first lists",
    operation_id="create_space",
)
def create_space(body: SpaceCreate, request: Request,
                 access: WorkspaceAccess = Depends(as_admin),
                 db: Session = Depends(get_db)):
    """Only `name` and `key` are required: with nothing else the space starts with the
    governance default template — To Do, In Progress, In Review and Done unless the
    workspace chose another. `initial_lists` are created at the space root, in order.
    Audited."""
    return ctl.create_space(access, body, db, request)


@router.get(
    "/{workspace_id}/spaces/{space_id}",
    response_model=SuccessResponse[SpaceDetail],
    responses={**COMMON_ERRORS, **NOT_A_MEMBER, **NO_SPACE},
    summary="Get a space with its charter and earlier keys",
    operation_id="get_space",
)
def get_space(space_id: str, access: WorkspaceAccess = Depends(as_guest),
              db: Session = Depends(get_db)):
    """The space as the tree draws it, plus its creation checklist, the keys it used to have
    and its freeze details."""
    return ctl.get_space(access, space_id, db)


@router.patch(
    "/{workspace_id}/spaces/{space_id}",
    response_model=SuccessResponse[SpaceDetail],
    responses={**COMMON_ERRORS, **VALIDATION_ERROR, **NOT_A_MEMBER, **NO_SPACE,
               **SPACE_CONFLICT, **LOCKED, **SPACE_RULES},
    summary="Rename a space, change its key, owner, delivery method or charter",
    operation_id="update_space",
)
def update_space(space_id: str, body: SpaceUpdate, request: Request,
                 access: WorkspaceAccess = Depends(as_admin),
                 db: Session = Depends(get_db)):
    """Admins. Only the fields present change; null clears the nullable ones. A new key keeps
    the old one resolving and reserved. An archived or frozen space refuses (423). Audited."""
    return ctl.update_space(access, space_id, body, db, request)


@router.post(
    "/{workspace_id}/spaces/{space_id}/archive",
    response_model=SuccessResponse[SpaceNode],
    responses={**COMMON_ERRORS, **NOT_A_MEMBER, **NO_SPACE, **ARCHIVE_REFUSED},
    summary="Archive a space: read-only for good, history and reports kept",
    operation_id="archive_space",
)
def archive_space(space_id: str, request: Request,
                  access: WorkspaceAccess = Depends(as_admin),
                  db: Session = Depends(get_db)):
    """Admins. Refused while it is frozen, has an active sprint, or someone has a timer
    running on one of its issues. It stays in the tree, flagged. Audited."""
    return ctl.archive_space(access, space_id, db, request)


@router.post(
    "/{workspace_id}/spaces/{space_id}/unarchive",
    response_model=SuccessResponse[SpaceNode],
    responses={**COMMON_ERRORS, **NOT_A_MEMBER, **NO_SPACE, **ARCHIVE_REFUSED},
    summary="Unarchive a space",
    operation_id="unarchive_space",
)
def unarchive_space(space_id: str, request: Request,
                    access: WorkspaceAccess = Depends(as_admin),
                    db: Session = Depends(get_db)):
    """Admins. Folders and lists archived on their own stay archived. Audited."""
    return ctl.unarchive_space(access, space_id, db, request)


@router.post(
    "/{workspace_id}/spaces/{space_id}/freeze",
    response_model=SuccessResponse[SpaceNode],
    responses={**COMMON_ERRORS, **VALIDATION_ERROR, **NOT_A_MEMBER, **NO_SPACE, **FREEZE_REFUSED},
    summary="Freeze a space: read-only while it is being reorganised",
    operation_id="freeze_space",
)
def freeze_space(space_id: str, body: SpaceFreeze, request: Request,
                 access: WorkspaceAccess = Depends(as_admin),
                 db: Session = Depends(get_db)):
    """Admins. Every write to the space and its issues answers 423 with the reason until it
    is unfrozen; GitHub deliveries naming its issues are deferred. Audited."""
    return ctl.freeze_space(access, space_id, body, db, request)


@router.post(
    "/{workspace_id}/spaces/{space_id}/unfreeze",
    response_model=SuccessResponse[SpaceUnfreezeResult],
    responses={**COMMON_ERRORS, **NOT_A_MEMBER, **NO_SPACE, **FREEZE_REFUSED},
    summary="Unfreeze a space and replay its deferred GitHub deliveries",
    operation_id="unfreeze_space",
)
def unfreeze_space(space_id: str, request: Request,
                   access: WorkspaceAccess = Depends(as_admin),
                   db: Session = Depends(get_db)):
    """Admins. Refused while the migration plan that froze it is running or has failed.
    Then replays the workspace's deferred deliveries, oldest first, and says how many.
    Audited."""
    return ctl.unfreeze_space(access, space_id, db, request)


@router.post(
    "/{workspace_id}/spaces/{space_id}/folders",
    response_model=SuccessResponse[FolderNode],
    status_code=201,
    responses={**COMMON_ERRORS, **VALIDATION_ERROR, **NOT_A_MEMBER, **NO_SPACE, **LOCKED},
    summary="Create a folder in a space",
    operation_id="create_folder",
)
def create_folder(space_id: str, body: FolderCreate,
                  access: WorkspaceAccess = Depends(as_admin),
                  db: Session = Depends(get_db)):
    """Refused (423) in an archived or frozen space."""
    return ctl.create_folder(access, space_id, body, db)


@router.patch(
    "/{workspace_id}/folders/{folder_id}",
    response_model=SuccessResponse[FolderNode],
    responses={**COMMON_ERRORS, **VALIDATION_ERROR, **NOT_A_MEMBER, **NO_FOLDER, **LOCKED,
               **CONTAINER_RULES},
    summary="Rename or reorder a folder",
    operation_id="update_folder",
)
def update_folder(folder_id: str, body: FolderUpdate, request: Request,
                  access: WorkspaceAccess = Depends(as_admin),
                  db: Session = Depends(get_db)):
    """Admins. Positions are set as given. An archived folder, or one in an archived or
    frozen space, refuses (423). Audited."""
    return ctl.update_folder(access, folder_id, body, db, request)


@router.post(
    "/{workspace_id}/folders/{folder_id}/archive",
    response_model=SuccessResponse[FolderNode],
    responses={**COMMON_ERRORS, **NOT_A_MEMBER, **NO_FOLDER, **ARCHIVE_REFUSED, **LOCKED},
    summary="Archive a folder and, with it, its lists",
    operation_id="archive_folder",
)
def archive_folder(folder_id: str, request: Request,
                   access: WorkspaceAccess = Depends(as_admin),
                   db: Session = Depends(get_db)):
    """Admins. Its lists become read-only with it. Refused while one of its issues is in a
    sprint that is not closed or has a timer running. Audited."""
    return ctl.archive_folder(access, folder_id, db, request)


@router.post(
    "/{workspace_id}/folders/{folder_id}/unarchive",
    response_model=SuccessResponse[FolderNode],
    responses={**COMMON_ERRORS, **NOT_A_MEMBER, **NO_FOLDER, **ARCHIVE_REFUSED, **LOCKED},
    summary="Unarchive a folder",
    operation_id="unarchive_folder",
)
def unarchive_folder(folder_id: str, request: Request,
                     access: WorkspaceAccess = Depends(as_admin),
                     db: Session = Depends(get_db)):
    """Admins. In an archived or frozen space it is refused (423): unarchive from the top
    down. Audited."""
    return ctl.unarchive_folder(access, folder_id, db, request)


@router.post(
    "/{workspace_id}/lists",
    response_model=SuccessResponse[ListNode],
    status_code=201,
    responses={**COMMON_ERRORS, **VALIDATION_ERROR, **NOT_A_MEMBER, **NO_LIST, **LOCKED},
    summary="Create a list in a space or one of its folders",
    operation_id="create_list",
)
def create_list(body: ListCreate,
                access: WorkspaceAccess = Depends(as_admin),
                db: Session = Depends(get_db)):
    """Refused (423) in an archived or frozen space, or an archived folder."""
    return ctl.create_list(access, body, db)


@router.patch(
    "/{workspace_id}/lists/{list_id}",
    response_model=SuccessResponse[ListNode],
    responses={**COMMON_ERRORS, **VALIDATION_ERROR, **NOT_A_MEMBER, **NO_LIST, **LOCKED,
               **CONTAINER_RULES},
    summary="Rename, reorder or move a list within its space",
    operation_id="update_list",
)
def update_list(list_id: str, body: ListUpdate, request: Request,
                access: WorkspaceAccess = Depends(as_admin),
                db: Session = Depends(get_db)):
    """Admins. `folder_id` is a folder of the same space, or null for the space root; to
    another space is a transfer, not a list change. A read-only list or target refuses
    (423). Audited."""
    return ctl.update_list(access, list_id, body, db, request)


@router.post(
    "/{workspace_id}/lists/{list_id}/archive",
    response_model=SuccessResponse[ListNode],
    responses={**COMMON_ERRORS, **NOT_A_MEMBER, **NO_LIST, **ARCHIVE_REFUSED, **LOCKED},
    summary="Archive a list: read-only for good, history and reports kept",
    operation_id="archive_list",
)
def archive_list(list_id: str, request: Request,
                 access: WorkspaceAccess = Depends(as_admin),
                 db: Session = Depends(get_db)):
    """Admins. Refused while one of its issues is in a sprint that is not closed or has a
    timer running. Audited."""
    return ctl.archive_list(access, list_id, db, request)


@router.post(
    "/{workspace_id}/lists/{list_id}/unarchive",
    response_model=SuccessResponse[ListNode],
    responses={**COMMON_ERRORS, **NOT_A_MEMBER, **NO_LIST, **ARCHIVE_REFUSED, **LOCKED},
    summary="Unarchive a list",
    operation_id="unarchive_list",
)
def unarchive_list(list_id: str, request: Request,
                   access: WorkspaceAccess = Depends(as_admin),
                   db: Session = Depends(get_db)):
    """Admins. In an archived folder or an archived or frozen space it is refused (423):
    unarchive from the top down. Audited."""
    return ctl.unarchive_list(access, list_id, db, request)


@router.get(
    "/{workspace_id}/repositories",
    response_model=SuccessResponse[list[RepositorySummary]],
    responses={**COMMON_ERRORS, **NOT_A_MEMBER},
    summary="List the repositories linked to a workspace",
    operation_id="list_repositories",
)
def list_repositories(access: WorkspaceAccess = Depends(as_guest),
                      db: Session = Depends(get_db)):
    return ctl.list_repositories(access, db)


@router.post(
    "/{workspace_id}/repositories",
    response_model=SuccessResponse[RepositorySummary],
    status_code=201,
    responses={**COMMON_ERRORS, **VALIDATION_ERROR, **NOT_A_MEMBER, **CONFLICT},
    summary="Link a GitHub repository to a workspace",
    operation_id="link_repository",
)
def link_repository(body: RepositoryLink, request: Request,
                    access: WorkspaceAccess = Depends(as_admin),
                    db: Session = Depends(get_db)):
    """After this, pull requests from the repository that name an issue key in their branch,
    title or body are linked to that issue, and a merge applies the space's merge action."""
    return ctl.link_repository(access, body, db, request)
