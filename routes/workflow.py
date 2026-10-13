"""Workflow routes: read, preview and publish a space's workflow; templates; editors.

docs/design/09-restructuring.md §4. Thin, like every route module (CLAUDE.md §2): path,
contract, the tenancy gate, a controller call.

Preview and publish declare `as_member` because a guest never edits a workflow, granted or
not (services/workflow_access.py); the per-space grant is checked in the controller, after
the space itself has been found — a space of another workspace is a 404 before it is a 403.
"""
from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from controllers import workflow_controller as ctl
from db.session import get_db
from dependencies.workspace import WorkspaceAccess, as_admin, as_guest, as_member
from routes.workspaces import NOT_A_MEMBER
from schemas.errors import COMMON_ERRORS, VALIDATION_ERROR
from schemas.response import ErrorResponse, SuccessResponse
from schemas.responses.workflow import (
    WorkflowDefinition,
    WorkflowEditor,
    WorkflowPreview,
    WorkflowPublishResult,
    WorkflowTemplateDeleted,
    WorkflowTemplateSummary,
)
from schemas.workflow import (
    WorkflowDraft,
    WorkflowEditorsUpdate,
    WorkflowPublish,
    WorkflowTemplateCreate,
)

router = APIRouter(prefix="/workspaces/{workspace_id}", tags=["Workflow"])

NO_SPACE = {404: {"model": ErrorResponse,
                  "description": "No such space in this workspace — or one in another."}}
NO_EDIT = {403: {"model": ErrorResponse,
                 "description": "Your role is too low, or you have no workflow-edit "
                                "permission on this space. Admins and owners always have "
                                "it; an admin can grant it to a member; guests never edit."}}
PUBLISH_CONFLICT = {409: {"model": ErrorResponse,
                          "description": "The draft is stale: someone published since "
                                         "(`data.current_version`), or the number of issues "
                                         "that would move is not `confirmed_affected_issues` "
                                         "(`data.affected_issue_count`). Preview again."}}
PUBLISH_INVALID = {422: {"model": ErrorResponse,
                         "description": "The body did not validate (`data.errors`, as on any "
                                        "route), or the workflow has errors, or a stage that "
                                        "holds issues changes category without "
                                        "`acknowledge_reporting_changes` — in both of those "
                                        "`data` is the `WorkflowPreview`."}}
LOCKED = {423: {"model": ErrorResponse,
                "description": "The space is archived or frozen for a migration: read-only."}}
NO_TEMPLATE = {404: {"model": ErrorResponse,
                     "description": "No such template in this workspace, or a built-in "
                                    "(built-ins cannot be deleted)."}}
TEMPLATE_CONFLICT = {409: {"model": ErrorResponse,
                           "description": "A template of that name exists (ignoring case), "
                                          "or — on delete — it is the workspace's default for "
                                          "new spaces."}}


@router.get(
    "/spaces/{space_id}/workflow",
    response_model=SuccessResponse[WorkflowDefinition],
    responses={**COMMON_ERRORS, **NOT_A_MEMBER, **NO_SPACE},
    summary="Get a space's workflow, with issue counts per stage",
    operation_id="get_workflow",
)
def get_workflow(space_id: str, access: WorkspaceAccess = Depends(as_guest),
                 db: Session = Depends(get_db)):
    """The live stages in board order with their issue and WIP counts, the transitions, the
    initial stage, the merge action, whether you may edit it and who was granted to. Its
    `version` is the `base_version` of a draft made from it."""
    return ctl.get_workflow(access, space_id, db)


@router.post(
    "/spaces/{space_id}/workflow/preview",
    response_model=SuccessResponse[WorkflowPreview],
    responses={**COMMON_ERRORS, **VALIDATION_ERROR, **NOT_A_MEMBER, **NO_SPACE, **NO_EDIT},
    summary="Preview what publishing a workflow draft would do",
    operation_id="preview_workflow",
)
def preview_workflow(space_id: str, body: WorkflowDraft,
                     access: WorkspaceAccess = Depends(as_member),
                     db: Session = Depends(get_db)):
    """Stores nothing. Answers 200 whether the draft is valid or not: `valid`, every error
    and warning with its code and the refs it is about, the resolved stages, the stages being
    removed and where their issues go, category changes, the moves, the transitions with
    names, the merge action, and `affected_issue_count` — the number to confirm when
    publishing."""
    return ctl.preview_workflow(access, space_id, body, db)


@router.put(
    "/spaces/{space_id}/workflow",
    response_model=SuccessResponse[WorkflowPublishResult],
    responses={**COMMON_ERRORS, **NOT_A_MEMBER, **NO_SPACE, **NO_EDIT, **PUBLISH_CONFLICT,
               **PUBLISH_INVALID, **LOCKED},
    summary="Publish a workflow draft to the whole space",
    operation_id="publish_workflow",
)
def publish_workflow(space_id: str, body: WorkflowPublish, request: Request,
                     access: WorkspaceAccess = Depends(as_member),
                     db: Session = Depends(get_db)):
    """Affects every board and list of the space. One transaction under a lock on the space:
    the version and the draft are checked again against the data as it is now, kept stages
    keep their ids (so issues and history stay attached), new ones are created, removed ones
    are soft-deleted after their issues move to the replacement — each move a
    `status_changed` event by you. Nothing is completed or deleted silently. Audited."""
    return ctl.publish_workflow(access, space_id, body, db, request)


@router.get(
    "/workflow-templates",
    response_model=SuccessResponse[list[WorkflowTemplateSummary]],
    responses={**COMMON_ERRORS, **NOT_A_MEMBER},
    summary="List the workflow templates: the built-ins, then this workspace's own",
    operation_id="list_workflow_templates",
)
def list_workflow_templates(access: WorkspaceAccess = Depends(as_guest),
                            db: Session = Depends(get_db)):
    """Each with its definition. Applying one to an existing space is the editor loading it
    into a draft and publishing it, with the same preview and replacement rules as any other
    change — resetting to the default is exactly that, with `builtin:default`."""
    return ctl.list_templates(access, db)


@router.post(
    "/workflow-templates",
    response_model=SuccessResponse[WorkflowTemplateSummary],
    status_code=201,
    responses={**COMMON_ERRORS, **VALIDATION_ERROR, **NOT_A_MEMBER, **NO_SPACE,
               **TEMPLATE_CONFLICT},
    summary="Save a workflow template",
    operation_id="create_workflow_template",
)
def create_workflow_template(body: WorkflowTemplateCreate, request: Request,
                             access: WorkspaceAccess = Depends(as_member),
                             db: Session = Depends(get_db)):
    """From a space's current workflow (`from_space_id`; needs workflow-edit permission on
    it) or from a written definition (`definition`; admins only). Audited."""
    return ctl.create_template(access, body, db, request)


@router.delete(
    "/workflow-templates/{template_id}",
    response_model=SuccessResponse[WorkflowTemplateDeleted],
    responses={**COMMON_ERRORS, **NOT_A_MEMBER, **NO_TEMPLATE, **TEMPLATE_CONFLICT},
    summary="Delete one of this workspace's workflow templates",
    operation_id="delete_workflow_template",
)
def delete_workflow_template(template_id: str, request: Request,
                             access: WorkspaceAccess = Depends(as_member),
                             db: Session = Depends(get_db)):
    """Its creator or an admin. Spaces made from it are unaffected: a template is copied into
    a space, never linked to it. Audited."""
    return ctl.delete_template(access, template_id, db, request)


@router.get(
    "/spaces/{space_id}/workflow/editors",
    response_model=SuccessResponse[list[WorkflowEditor]],
    responses={**COMMON_ERRORS, **NOT_A_MEMBER, **NO_SPACE},
    summary="List who was granted workflow-edit permission on a space",
    operation_id="list_workflow_editors",
)
def list_workflow_editors(space_id: str, access: WorkspaceAccess = Depends(as_guest),
                          db: Session = Depends(get_db)):
    """Admins and owners can always edit and are not listed."""
    return ctl.list_editors(access, space_id, db)


@router.put(
    "/spaces/{space_id}/workflow/editors",
    response_model=SuccessResponse[list[WorkflowEditor]],
    responses={**COMMON_ERRORS, **VALIDATION_ERROR, **NOT_A_MEMBER, **NO_SPACE},
    summary="Replace who may edit a space's workflow",
    operation_id="set_workflow_editors",
)
def set_workflow_editors(space_id: str, body: WorkflowEditorsUpdate, request: Request,
                         access: WorkspaceAccess = Depends(as_admin),
                         db: Session = Depends(get_db)):
    """Admins only. The list replaces the grants; every id must be a member whose role is
    member or higher (a 422 names the rest). Granted once, the member edits the space's
    workflow without an admin each time. Audited."""
    return ctl.set_editors(access, space_id, body, db, request)
