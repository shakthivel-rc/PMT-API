"""Workflows — the transaction boundary for reading, previewing and publishing a space's
workflow, for workflow templates, and for who may edit (docs/design/09-restructuring.md §4).

| Act | guest | member | member granted on the space | admin / owner |
|---|---|---|---|---|
| Read a workflow, its editors, the templates | yes | yes | yes | yes |
| Preview and publish the space's workflow | — | — | yes | yes |
| Save the space's workflow as a template | — | — | yes | yes |
| Save a template from a written definition | — | — | — | yes |
| Delete a template | — | its creator | its creator | yes |
| Grant workflow-edit permission | — | — | — | yes |

Everyone who may move issues keeps executing the transitions a workflow allows; the grant is
only about changing the workflow itself (services/workflow_access.py). A space, template or
user of another workspace is a 404 like any unknown id (CLAUDE.md §8).
"""
from __future__ import annotations

import logging

from fastapi import Request
from fastapi.encoders import jsonable_encoder
from pydantic import ValidationError
from sqlalchemy.orm import Session

from controllers.workspace_controller import _audit
from dependencies.workspace import WorkspaceAccess
from models.hierarchy import WorkflowTemplate
from schemas.response import SuccessResponse, error_json
from schemas.workflow import (
    TemplateDefinition,
    WorkflowDraft,
    WorkflowEditorsUpdate,
    WorkflowPublish,
    WorkflowTemplateCreate,
)
from services import governance, workflow_access, write_guard
from services import issue as issue_svc
from services import workflow as svc
from services import workspace as workspace_svc

logger = logging.getLogger("base_project.workflow")

_NO_SPACE = "Space not found"
_NO_TEMPLATE = "Workflow template not found"
_NO_EDIT = ("You cannot change this space's workflow. An admin can grant you workflow-edit "
            "permission on it.")


def _plural(count: int, noun: str) -> str:
    return f"{count} {noun}" if count == 1 else f"{count} {noun}s"


def get_workflow(access: WorkspaceAccess, space_id: str, db: Session):
    space = workspace_svc.get_space(db, access.workspace_id, space_id)
    if space is None:
        return error_json(404, _NO_SPACE)
    return SuccessResponse(status_code=200, message="Workflow fetched",
                           data=svc.definition_payload(db, access, space))


def preview_workflow(access: WorkspaceAccess, space_id: str, body: WorkflowDraft,
                     db: Session):
    """Writes nothing, so it is answered for a frozen or archived space too: reads are never
    refused (09-restructuring.md §3.4), and the publish says 423."""
    space = workspace_svc.get_space(db, access.workspace_id, space_id)
    if space is None:
        return error_json(404, _NO_SPACE)
    if not workflow_access.can_edit_workflow(db, access, str(space.id)):
        return error_json(403, _NO_EDIT)
    evaluation = svc.evaluate(svc.snapshot(db, access.workspace_id, space), body)
    return SuccessResponse(status_code=200, message="Workflow previewed",
                           data=evaluation.preview)


def publish_workflow(access: WorkspaceAccess, space_id: str, body: WorkflowPublish,
                     db: Session, request: Request):
    """§4.4 steps 1-7, in one transaction under the space's row lock."""
    space = workspace_svc.get_space(db, access.workspace_id, space_id)
    if space is None:
        return error_json(404, _NO_SPACE)
    if not workflow_access.can_edit_workflow(db, access, str(space.id)):
        return error_json(403, _NO_EDIT)

    # End the transaction the reads above ran in, before taking the lock. Under MySQL's
    # REPEATABLE READ a transaction keeps reading the snapshot of its first read — here the
    # membership check — so a publish that waited for the lock behind another one would go
    # on counting the stages and issues as they were before that one committed. After the
    # rollback the lock is the new transaction's first statement, and every read after it
    # sees what was committed before the lock was granted. Nothing has been written, so
    # nothing is lost (CLAUDE.md §17 rule 5).
    db.rollback()
    space = svc.lock_space(db, access.workspace_id, space_id)
    if space is None:
        db.rollback()
        return error_json(404, _NO_SPACE)
    block = write_guard.space_block(space)
    if block is not None:
        db.rollback()
        return block.response()

    # 1. The draft was made from the workflow as it is now.
    current_version = int(space.workflow_version or 1)
    if body.base_version != current_version:
        db.rollback()
        return error_json(409, "Someone published a change to this workflow since you opened "
                               "it. Reload it and apply your changes again.",
                          data={"current_version": current_version})

    # 2. Valid against the database as it is now, not as it was at the preview.
    evaluation = svc.evaluate(svc.snapshot(db, access.workspace_id, space), body)
    if not evaluation.valid:
        db.rollback()
        return error_json(422, "The workflow has problems to fix before it can be published. "
                               "See data.errors.", data=evaluation.preview)

    # 3. The issues that will move are the ones the person saw. Counted from the locked
    # rows, the ones that will actually move — and a removed stage that gained issues since
    # the preview without one being given a replacement is the same refusal: the person
    # reviewed a different set of moves.
    moving = svc.issues_to_move(db, access.workspace_id, str(space.id), evaluation)
    affected = sum(len(issues) for issues in moving.values())
    unplaced = any(moving.get(removal.stage.id) and removal.replacement is None
                   for removal in evaluation.removals)
    if affected != body.confirmed_affected_issues or unplaced:
        db.rollback()
        if affected != body.confirmed_affected_issues:
            verb = "is" if affected == 1 else "are"
            message = (f"{_plural(affected, 'issue')} {verb} in the stages being removed "
                       f"now, not {body.confirmed_affected_issues}. Preview again before "
                       "publishing.")
        else:
            message = ("Issues have moved into a stage being removed since the preview, and "
                       "it has no replacement. Preview again before publishing.")
        return error_json(409, message, data={"affected_issue_count": affected})

    # A populated stage changing category re-labels its issues and their history (§4.2).
    if evaluation.preview["requires_reporting_ack"] and not body.acknowledge_reporting_changes:
        db.rollback()
        names = ", ".join(change["name"] for change in evaluation.preview["category_changes"]
                          if change["issue_count"])
        return error_json(422, f"Publishing re-labels the issues in {names}, and their "
                               "history, in every report. Confirm it with "
                               "acknowledge_reporting_changes: true.", data=evaluation.preview)

    # 4-7.
    impact = svc.publish(db, space, body, evaluation, moving, access.user_id)
    version = int(space.workflow_version)
    _audit(db, request, access.user_id, "UPDATE", "workflow", str(space.id),
           f"Published workflow version {version} of space {space.key}: "
           f"{len(impact['added'])} stage(s) added, {len(impact['removed'])} removed, "
           f"{len(impact['renamed'])} renamed; {_plural(impact['moved_issues'], 'issue')} "
           "moved")
    db.commit()
    logger.info("workflow published", extra={"extra_fields": {
        "workspace_id": access.workspace_id, "space_id": space_id, "version": version,
        "moved_issues": impact["moved_issues"], "removed_stages": len(impact["removed"])}})
    return SuccessResponse(status_code=200, message="Workflow published", data={
        "workflow": svc.definition_payload(db, access, space),
        "moved_issues": impact["moved_issues"],
        "version": version,
    })


# ---- templates --------------------------------------------------------------------------------


def list_templates(access: WorkspaceAccess, db: Session):
    return SuccessResponse(status_code=200, message="Workflow templates fetched",
                           data=svc.template_payloads(db, access.workspace_id))


def create_template(access: WorkspaceAccess, body: WorkflowTemplateCreate, db: Session,
                    request: Request):
    if body.from_space_id is not None:
        space = workspace_svc.get_space(db, access.workspace_id, body.from_space_id)
        if space is None:
            return error_json(404, _NO_SPACE)
        if not workflow_access.can_edit_workflow(db, access, str(space.id)):
            return error_json(403, "Saving a space's workflow as a template needs "
                                   "workflow-edit permission on that space.")
        source = svc.space_definition(db, access.workspace_id, space)
    else:
        if not access.at_least("admin"):
            return error_json(403, "Only an admin can save a template from a written "
                                   "definition. Save one from a space whose workflow you can "
                                   "edit instead.")
        # Validated on the way in; the dump is what is stored.
        source = body.definition.model_dump(by_alias=True) if body.definition else {}
    if svc.template_name_taken(db, access.workspace_id, body.name):
        return error_json(409, f"This workspace already has a template called {body.name}.")
    try:
        definition = TemplateDefinition.model_validate(source).model_dump(by_alias=True)
    except ValidationError as exc:
        # Only reachable from a space: a written definition was refused before this ran. A
        # space migrated with an old shape (no done stage, say) cannot become a template
        # until a publish fixes it — a template is applied without review, so it must be one.
        return error_json(422, "This space's workflow cannot be saved as a template until it "
                               "is fixed. See data.errors.",
                          data={"errors": jsonable_encoder(exc.errors(include_url=False))})
    row = WorkflowTemplate(workspace_id=access.workspace_id, name=body.name,
                           description=body.description, definition=definition,
                           created_by=access.user_id)
    db.add(row)
    db.flush()
    _audit(db, request, access.user_id, "CREATE", "workflow_template", str(row.id),
           f"Saved workflow template {row.name}")
    db.commit()
    creator = issue_svc.users_by_id(db, [access.user_id]).get(access.user_id)
    return SuccessResponse(status_code=201, message="Workflow template saved",
                           data=svc.template_payload(row, definition, creator))


def delete_template(access: WorkspaceAccess, template_id: str, db: Session,
                    request: Request):
    if template_id.startswith(svc.BUILTIN_PREFIX):
        # 404 rather than 403: there is no row to refuse access to, and no role that could
        # delete one — built-ins are code, shared by every workspace.
        return error_json(404, "Built-in templates are part of PMT and cannot be deleted.")
    row = svc.get_template(db, access.workspace_id, template_id)
    if row is None:
        return error_json(404, _NO_TEMPLATE)
    if not access.at_least("admin") and row.created_by != access.user_id:
        return error_json(403, "Only the person who saved a template, or an admin, can "
                               "delete it.")
    if governance.load_settings(db, access.workspace_id).default_workflow_template == row.id:
        # Deleting it would leave the governance default naming nothing, and the next space
        # created would fail or silently fall back — neither is what the admin chose.
        return error_json(409, "This template is the workspace's default for new spaces. "
                               "Choose another default in the governance settings first.")
    row.deleted = 1
    _audit(db, request, access.user_id, "DELETE", "workflow_template", str(row.id),
           f"Deleted workflow template {row.name}")
    db.commit()
    return SuccessResponse(status_code=200, message="Workflow template deleted",
                           data={"id": row.id})


# ---- editors ----------------------------------------------------------------------------------


def list_editors(access: WorkspaceAccess, space_id: str, db: Session):
    space = workspace_svc.get_space(db, access.workspace_id, space_id)
    if space is None:
        return error_json(404, _NO_SPACE)
    return SuccessResponse(status_code=200, message="Workflow editors fetched",
                           data=svc.editor_payloads(db, access.workspace_id, str(space.id)))


def set_editors(access: WorkspaceAccess, space_id: str, body: WorkflowEditorsUpdate,
                db: Session, request: Request):
    """Not behind the write guard: a grant changes who may edit, not the space's work, and an
    archived or frozen space still refuses the publish itself."""
    space = workspace_svc.get_space(db, access.workspace_id, space_id)
    if space is None:
        return error_json(404, _NO_SPACE)
    wanted = list(dict.fromkeys(body.user_ids))
    members = svc.members_by_id(db, access.workspace_id, wanted)
    outsiders = [user_id for user_id in wanted if user_id not in members]
    guests = [user_id for user_id in wanted
              if user_id in members and members[user_id][1] == "guest"]
    if outsiders or guests:
        # Outsiders are named by the id that was sent: naming the account would tell an
        # admin of this workspace who has an account elsewhere.
        reasons = []
        if outsiders:
            reasons.append(f"not members of this workspace: {', '.join(outsiders)}")
        if guests:
            reasons.append("guests, who never edit a workflow: "
                           + ", ".join(issue_svc.display_name(members[user_id][0])
                                       for user_id in guests))
        return error_json(422, "These people cannot be workflow editors: "
                               + "; ".join(reasons) + ".",
                          data={"not_members": outsiders, "guests": guests})
    added, removed = svc.replace_editors(db, access.workspace_id, str(space.id), wanted,
                                         access.user_id)
    people = issue_svc.users_by_id(db, [*added, *removed])

    def named(user_ids: list[str]) -> str:
        return ", ".join(issue_svc.display_name(people.get(user_id)) or user_id
                         for user_id in user_ids) or "nobody"

    _audit(db, request, access.user_id, "UPDATE", "workflow_editors", str(space.id),
           f"Workflow editors of space {space.key}: added {named(added)}; removed "
           f"{named(removed)}")
    db.commit()
    return SuccessResponse(status_code=200, message="Workflow editors updated",
                           data=svc.editor_payloads(db, access.workspace_id, str(space.id)))
