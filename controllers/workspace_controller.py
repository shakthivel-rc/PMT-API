"""Tenancy and hierarchy: workspaces, members, spaces, folders, lists, repositories.

The transaction boundary, as everywhere in `controllers/` (CLAUDE.md §2). Membership has
already been resolved by `dependencies/workspace.workspace_access` for every route that
names a workspace; what arrives here is a `WorkspaceAccess`, and the workspace id is read
from it and from nowhere else.

Administrative changes go to the shared audit trail (`activity_logs`) with an `entity_type`
of their own, beside the identity events the base already writes there.

Since 2026-10-05 (docs/design/09-restructuring.md §3) containers also have a **write state**.
Two different questions are asked of it, and they get different answers:

* *May this container be written to?* — creating a list in it, renaming it, moving a list
  into it. `services/write_guard.py` answers, and a refusal is **423 Locked**.
* *May this container change state?* — archive, unarchive, freeze, unfreeze. The state
  change itself is refused with **409** when the container is not ready (close its sprint
  first, stop its timers, wait for the migration plan), and with 423 when what *holds* it
  is read-only. `services/workspace.py` holds those rules, so the migration runner applies
  the same ones.

Since 2026-10-06 a workspace can be renamed and **soft-deleted**, and a member's role changed
or their membership removed — by an admin, or by the member themselves, who may always leave.
Who may do which is `utils/membership_rules.py`; what would break, and so must be changed
first, is `services/workspace.workspace_delete_refusal` and `member_removal_refusal`. Every
one of them takes `services/workspace.lock_workspace` first, so changes to one workspace's
membership happen one at a time.
"""
import logging

from fastapi import Request
from pydantic import ValidationError
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from dependencies.workspace import WorkspaceAccess
from models.user import User
from models.vcs import VcsRepository
from models.workspace import Workspace, WorkspaceMember, WorkspaceMemberRate
from schemas.governance import GovernanceSettings, GovernanceSettingsUpdate
from schemas.rates import MemberRateItem, WorkspaceRatesResponse, WorkspaceRatesUpdate
from schemas.response import SuccessResponse, error_json
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
from services import governance as governance_svc
from services import key_resolution, workflow_templates, write_guard
from services import workspace as svc
from services.activity_log import log_activity
from utils import membership_rules
from utils import time_tracking as tt

logger = logging.getLogger("base_project.workspaces")

_OWNER_NOT_MEMBER = "The owner must be a member of this workspace."
_NO_SPACE = "Space not found"
_NO_FOLDER = "Folder not found"
_NO_LIST = "List not found"
#: The tenancy gate's own wording (dependencies/workspace.py): what a request that passed it
#: is told when the workspace was deleted before its lock was taken.
_NO_WORKSPACE = "Workspace not found"
_NO_MEMBER = "That person is not a member of this workspace."
#: Create and rename alike. Says why an address no live workspace shows can still be taken —
#: without saying whether this one is, which would tell a stranger that a deleted workspace
#: once had it.
_SLUG_TAKEN = ("That workspace address is taken. Choose another slug. A deleted workspace "
               "keeps its address, so that it can be restored.")


def _audit(db: Session, request: Request, actor_id: str, action: str, entity_type: str,
           entity_id: str, details: str) -> None:
    """One `activity_logs` row, attributed the way the base's own controllers attribute
    theirs: the actor's display name at the time, and the client address the proxy-aware
    middleware left on the request."""
    actor = db.query(User).filter(User.id == actor_id).first()
    actor_name = f"{actor.first_name} {actor.last_name}" if actor else ""
    ip_address = request.client.host if request.client else None
    log_activity(db, actor_id, actor_name, action, entity_type=entity_type,
                 entity_id=entity_id, details=details, ip_address=ip_address)


def _blank_is_none(value: str | None) -> str | None:
    return value if value is not None and value.strip() else None


def _listed(labels: list[str]) -> str:
    return labels[0] if len(labels) == 1 else f"{', '.join(labels[:-1])} and {labels[-1]}"


def _charter_refusal(missing: list[str], lead: str):
    """The governance 422 (09-restructuring.md §3.2): every missing item at once.

    `data.errors` has the shape of a validation 422 — `loc` naming the field — so a form that
    already highlights validation errors highlights these too; `data.missing` is the same list
    as dotted names.
    """
    labels = [svc.CHARTER_ITEM_LABELS[item] for item in missing]
    errors = [{"loc": ["body", *item.split(".")], "type": "missing",
               "msg": f"Required by this workspace's governance settings: "
                      f"{svc.CHARTER_ITEM_LABELS[item]}."}
              for item in missing]
    return error_json(422, f"{lead} Missing: {_listed(labels)}.",
                      {"missing": missing, "errors": errors})


def _key_conflict(db: Session, workspace_id: str, key: str):
    if svc.space_key_taken(db, workspace_id, key):
        return error_json(409, f"The key {key} is already used by a space here.")
    return error_json(409, f"The key {key} used to belong to a space here. It stays reserved "
                           "so the issue keys that quote it keep resolving; choose another.")


def create_workspace(body: WorkspaceCreate, user_id: str, db: Session, request: Request):
    if svc.slug_taken(db, body.slug):
        return error_json(409, _SLUG_TAKEN)
    try:
        workspace = svc.create_workspace(db, user_id, body.name, body.slug)
        _audit(db, request, user_id, "CREATE", "workspace", workspace.id,
               f"Created workspace {body.slug}")
        db.commit()
    except IntegrityError:
        # Lost the race with a concurrent create of the same slug.
        db.rollback()
        return error_json(409, _SLUG_TAKEN)
    return SuccessResponse(
        status_code=201,
        message="Workspace created",
        data={"id": workspace.id, "name": workspace.name, "slug": workspace.slug,
              "role": "owner"},
    )


def list_workspaces(user_id: str, db: Session):
    rows = svc.workspaces_for_user(db, user_id)
    return SuccessResponse(
        status_code=200,
        message="Workspaces fetched",
        data=[{"id": w.id, "name": w.name, "slug": w.slug, "role": role} for w, role in rows],
    )


def get_workspace_tree(access: WorkspaceAccess, db: Session):
    workspace = svc.get_workspace(db, access.workspace_id)
    if workspace is None:
        return error_json(404, "Workspace not found")
    return SuccessResponse(
        status_code=200,
        message="Workspace fetched",
        data={"id": workspace.id, "name": workspace.name, "slug": workspace.slug,
              "role": access.role,
              "spaces": svc.workspace_tree(db, access.workspace_id, access=access)},
    )


def update_workspace(access: WorkspaceAccess, body: WorkspaceUpdate, db: Session,
                     request: Request):
    """Rename the workspace or give it another address. The body has at least one field and
    no nulls (schemas/workspaces.WorkspaceUpdate); a field equal to what is stored is not a
    change and is not audited."""
    workspace = svc.lock_workspace(db, access.workspace_id)
    if workspace is None:
        db.rollback()
        return error_json(404, _NO_WORKSPACE)
    fields = body.model_fields_set
    # Its own slug is not a change, so it is never checked against itself.
    new_slug = body.slug if "slug" in fields and body.slug != workspace.slug else None
    if new_slug is not None and svc.slug_taken(db, new_slug):
        db.rollback()
        return error_json(409, _SLUG_TAKEN)
    changes: list[str] = []
    try:
        if "name" in fields and body.name != workspace.name:
            changes.append(f"name {workspace.name} -> {body.name}")
            workspace.name = body.name
        if new_slug is not None:
            changes.append(f"slug {workspace.slug} -> {new_slug}")
            workspace.slug = new_slug
        if changes:
            _audit(db, request, access.user_id, "UPDATE", "workspace", str(workspace.id),
                   f"Updated workspace {workspace.slug}: {'; '.join(changes)}")
        db.commit()
    except IntegrityError:
        # Lost the race for the slug with a concurrent create or rename.
        db.rollback()
        return error_json(409, _SLUG_TAKEN)
    return SuccessResponse(
        status_code=200,
        message="Workspace updated",
        data={"id": workspace.id, "name": workspace.name, "slug": workspace.slug,
              "role": access.role},
    )


def delete_workspace(access: WorkspaceAccess, db: Session, request: Request):
    """Soft-delete the workspace (`services/workspace.mark_deleted`). Owners only: the route
    admits every member so that an admin is told exactly that, rather than the tenancy
    gate's "needs the admin role", which an admin already has."""
    if not access.at_least("owner"):
        return error_json(403, "Only an owner can delete this workspace.")
    workspace = svc.lock_workspace(db, access.workspace_id)
    if workspace is None:
        db.rollback()
        return error_json(404, _NO_WORKSPACE)
    refusal = svc.workspace_delete_refusal(db, workspace)
    if refusal is not None:
        db.rollback()
        return refusal.response()
    svc.mark_deleted(workspace, tt.utc_now())
    _audit(db, request, access.user_id, "DELETE", "workspace", str(workspace.id),
           f"Deleted workspace {workspace.slug} ({workspace.name})")
    db.commit()
    logger.info("workspace deleted", extra={"extra_fields": {
        "workspace_id": access.workspace_id}})
    return SuccessResponse(status_code=200, message="Workspace deleted",
                           data={"id": access.workspace_id})


def list_members(access: WorkspaceAccess, db: Session):
    return SuccessResponse(
        status_code=200,
        message="Members fetched",
        data=[svc.member_payload(m, u) for m, u in svc.members(db, access.workspace_id)],
    )


def add_member(access: WorkspaceAccess, body: MemberAdd, db: Session, request: Request):
    if body.role == "owner" and access.role != "owner":
        return error_json(403, "Only an owner can make someone else an owner.")
    user = svc.active_user_by_email(db, body.email)
    if user is None:
        # Deliberately the same answer as "exists but is disabled": this route is not a way
        # to find out which addresses have accounts.
        return error_json(404, "No active account uses that address. Invite them first.")
    if svc.is_member(db, access.workspace_id, user.id):
        return error_json(409, "That person is already a member of this workspace.")
    member = WorkspaceMember(workspace_id=access.workspace_id, user_id=user.id, role=body.role)
    db.add(member)
    _audit(db, request, access.user_id, "CREATE", "workspace_member", user.id,
           f"Added to workspace {access.workspace_id} as {body.role}")
    db.commit()
    return SuccessResponse(status_code=201, message="Member added",
                           data=svc.member_payload(member, user))


def update_member(access: WorkspaceAccess, user_id: str, body: MemberUpdate, db: Session,
                  request: Request):
    """Give a member another role. The role they already have is a 200 that writes nothing,
    the audit row included."""
    workspace_id = access.workspace_id
    if svc.lock_workspace(db, workspace_id) is None:
        db.rollback()
        return error_json(404, _NO_WORKSPACE)
    found = svc.lock_member(db, workspace_id, user_id)
    if found is None:
        db.rollback()
        return error_json(404, _NO_MEMBER)
    member, user = found
    current = str(member.role)
    problem = membership_rules.role_change_problem(
        caller_role=access.role, current_role=current, new_role=body.role,
        live_owners=svc.live_owner_count(db, workspace_id))
    if problem is not None:
        db.rollback()
        return error_json(*problem)
    if body.role != current:
        member.role = body.role
        _audit(db, request, access.user_id, "UPDATE", "workspace_member", str(user.id),
               f"Role in workspace {workspace_id}: {current} -> {body.role}")
    db.commit()
    return SuccessResponse(status_code=200, message="Member updated",
                           data=svc.member_payload(member, user))


def remove_member(access: WorkspaceAccess, user_id: str, db: Session, request: Request):
    """Remove a member, or — when `user_id` is the caller — leave. The role rules come first
    (utils/membership_rules.removal_problem), then what the removal would break
    (services/workspace.member_removal_refusal); with neither in the way the membership row
    goes, and their workflow-edit grants with it. Everything they did stays."""
    workspace_id = access.workspace_id
    if svc.lock_workspace(db, workspace_id) is None:
        db.rollback()
        return error_json(404, _NO_WORKSPACE)
    found = svc.lock_member(db, workspace_id, user_id)
    if found is None:
        db.rollback()
        return error_json(404, _NO_MEMBER)
    member, user = found
    leaving = user_id == access.user_id
    role = str(member.role)
    problem = membership_rules.removal_problem(
        caller_role=access.role, leaving=leaving, target_role=role,
        live_owners=svc.live_owner_count(db, workspace_id))
    if problem is not None:
        db.rollback()
        return error_json(*problem)
    refusal = svc.member_removal_refusal(db, workspace_id, user, access.user_id)
    if refusal is not None:
        db.rollback()
        return refusal.response()
    grants = svc.revoke_workflow_grants(db, workspace_id, user_id)
    db.delete(member)
    details = (f"Left workspace {workspace_id} (was {role})" if leaving
               else f"Removed from workspace {workspace_id} (was {role})")
    if grants:
        details += f"; workflow-edit grants revoked on {grants} space(s)"
    _audit(db, request, access.user_id, "DELETE", "workspace_member", user_id, details)
    db.commit()
    logger.info("workspace member removed", extra={"extra_fields": {
        "workspace_id": workspace_id, "user_id": user_id, "left": leaving}})
    return SuccessResponse(status_code=200,
                           message="You left the workspace" if leaving else "Member removed",
                           data={"user_id": user_id})


# ---- spaces -----------------------------------------------------------------------------


def _new_space_definition(db: Session, workspace_id: str, requested: str | None,
                          default_ref: str):
    """The workflow definition a new space starts with, or the 422 that refuses it.

    Named: it must exist here (422 otherwise). Not named: the governance default — and when
    that names a template that has since been deleted, the built-in `default`, because a
    deleted template must not stop anyone creating a space. Either way the definition must be
    one a publish would accept (`services/workspace.prepare_definition`).
    """
    if requested is not None:
        stored = svc.template_definition(db, workspace_id, requested)
        if stored is None:
            return None, error_json(422, f"There is no workflow template {requested} here. "
                                         "Use default, delivery or support, or the id of one "
                                         "of this workspace's templates.")
        source = "The workflow template"
    else:
        stored = svc.template_definition(db, workspace_id, default_ref)
        source = ("The workspace's default workflow template (governance settings)"
                  if stored is not None else "The default workflow template")
        if stored is None:
            stored = workflow_templates.builtin_definition(
                workflow_templates.DEFAULT_TEMPLATE_KEY)
    definition, problem = svc.prepare_definition(stored)
    if problem is not None:
        return None, error_json(422, f"{source} cannot be applied: {problem}.")
    return definition, None


def create_space(access: WorkspaceAccess, body: SpaceCreate, db: Session, request: Request):
    workspace_id = access.workspace_id
    governance = governance_svc.load_settings(db, workspace_id)
    if governance.require_space_charter:
        missing = svc.charter_gaps(body.owner_id, body.delivery_method, body.charter)
        if missing:
            return _charter_refusal(missing, "This workspace asks every new space for its "
                                             "creation checklist.")
    if body.owner_id is not None and not svc.is_member(db, workspace_id, body.owner_id):
        return error_json(422, _OWNER_NOT_MEMBER)
    definition, refusal = _new_space_definition(db, workspace_id, body.workflow_template,
                                                governance.default_workflow_template)
    if refusal is not None:
        return refusal
    if key_resolution.space_key_reserved(db, workspace_id, body.key):
        return _key_conflict(db, workspace_id, body.key)
    try:
        space = svc.create_space(
            db, workspace_id, body.name, body.key, definition,
            description=_blank_is_none(body.description), owner_id=body.owner_id,
            delivery_method=body.delivery_method, charter=svc.charter_document(body.charter),
            budget_amount=body.budget_amount,
            budget_alert_threshold=body.budget_alert_threshold,
            ai_triage_enabled=body.ai_triage_enabled if body.ai_triage_enabled is not None else True,
            ai_standup_enabled=body.ai_standup_enabled if body.ai_standup_enabled is not None else True,
            ai_retrospective_enabled=body.ai_retrospective_enabled if body.ai_retrospective_enabled is not None else True)
        space_id = str(space.id)
        for name in body.initial_lists:
            svc.create_list(db, workspace_id, space_id, None, name)
        _audit(db, request, access.user_id, "CREATE", "space", space_id,
               f"Created space {body.key}")
        db.commit()
    except ValueError as exc:
        # `materialize`'s own bounds checks; `prepare_definition` should have caught them
        # first, and this keeps a template that slips past both a 422 with nothing built.
        db.rollback()
        return error_json(422, f"The workflow template cannot be applied: {exc}.")
    except IntegrityError:
        # Lost the race with a concurrent create of the same key.
        db.rollback()
        return _key_conflict(db, workspace_id, body.key)
    return SuccessResponse(status_code=201, message="Space created",
                           data=svc.space_node(db, access, space_id))


def get_space(access: WorkspaceAccess, space_id: str, db: Session):
    space = svc.get_space(db, access.workspace_id, space_id)
    if space is None:
        return error_json(404, _NO_SPACE)
    return SuccessResponse(status_code=200, message="Space fetched",
                           data=svc.space_detail(db, access, space))


def update_space(access: WorkspaceAccess, space_id: str, body: SpaceUpdate, db: Session,
                 request: Request):
    workspace_id = access.workspace_id
    space = svc.get_space(db, workspace_id, space_id)
    if space is None:
        return error_json(404, _NO_SPACE)
    block = write_guard.space_block(space)
    if block is not None:
        return block.response()
    fields = body.model_fields_set
    for field in ("name", "key"):
        if field in fields and getattr(body, field) is None:
            return error_json(422, f"{field} cannot be empty.")
    if "owner_id" in fields and body.owner_id is not None and not svc.is_member(
            db, workspace_id, body.owner_id):
        return error_json(422, _OWNER_NOT_MEMBER)

    stored_charter = svc.charter_of(space)
    owner_id = body.owner_id if "owner_id" in fields else space.owner_id
    delivery = body.delivery_method if "delivery_method" in fields else space.delivery_method
    charter = body.charter if "charter" in fields else stored_charter
    if governance_svc.load_settings(db, workspace_id).require_space_charter:
        # The epic rule of 09-restructuring.md §2.3, applied to the space checklist: what is
        # filled in may not be removed while the workspace requires it, but a space created
        # before the setting stays editable without completing it first.
        before = set(svc.charter_gaps(space.owner_id, space.delivery_method, stored_charter))
        removed = [item for item in svc.charter_gaps(owner_id, delivery, charter)
                   if item not in before]
        if removed:
            return _charter_refusal(removed, "This workspace requires every space to keep "
                                             "its creation checklist.")

    new_key = body.key if "key" in fields and body.key != space.key else None
    if new_key is not None and key_resolution.space_key_reserved(
            db, workspace_id, new_key, except_space_id=str(space.id)):
        return _key_conflict(db, workspace_id, new_key)

    changes: list[str] = []
    try:
        if "name" in fields and body.name != space.name:
            changes.append(f"name {space.name} -> {body.name}")
            space.name = body.name
        if new_key is not None:
            old_key = svc.change_space_key(db, space, new_key)
            changes.append(f"key {old_key} -> {new_key} (the old key stays an alias)")
        if "description" in fields:
            description = _blank_is_none(body.description)
            if description != space.description:
                space.description = description
                changes.append("description")
        if "owner_id" in fields and body.owner_id != space.owner_id:
            space.owner_id = body.owner_id
            changes.append(f"owner {body.owner_id or 'none'}")
        if "delivery_method" in fields and body.delivery_method != space.delivery_method:
            space.delivery_method = body.delivery_method
            changes.append(f"delivery method {body.delivery_method or 'none'}")
        if "charter" in fields:
            document = svc.charter_document(body.charter)
            if document != space.charter:
                space.charter = document
                changes.append("charter")
        if "budget_amount" in fields and body.budget_amount != space.budget_amount:
            space.budget_amount = body.budget_amount
            changes.append(f"budget_amount {body.budget_amount}")
        if "budget_alert_threshold" in fields and body.budget_alert_threshold != space.budget_alert_threshold:
            space.budget_alert_threshold = body.budget_alert_threshold or 80
            changes.append(f"budget_alert_threshold {body.budget_alert_threshold}")
        if "ai_triage_enabled" in fields and body.ai_triage_enabled is not None:
            space.ai_triage_enabled = body.ai_triage_enabled
            changes.append(f"ai_triage_enabled {body.ai_triage_enabled}")
        if "ai_standup_enabled" in fields and body.ai_standup_enabled is not None:
            space.ai_standup_enabled = body.ai_standup_enabled
            changes.append(f"ai_standup_enabled {body.ai_standup_enabled}")
        if "ai_retrospective_enabled" in fields and body.ai_retrospective_enabled is not None:
            space.ai_retrospective_enabled = body.ai_retrospective_enabled
            changes.append(f"ai_retrospective_enabled {body.ai_retrospective_enabled}")
        if changes:
            _audit(db, request, access.user_id, "UPDATE", "space", str(space.id),
                   f"Updated space {space.key}: {'; '.join(changes)}")
        db.commit()
    except IntegrityError:
        # Lost the race for the key with a concurrent rename or create.
        db.rollback()
        return _key_conflict(db, workspace_id, new_key or str(body.key))
    return SuccessResponse(status_code=200, message="Space updated",
                           data=svc.space_detail(db, access, space))


def archive_space(access: WorkspaceAccess, space_id: str, db: Session, request: Request):
    space = svc.lock_space(db, access.workspace_id, space_id)
    if space is None:
        return error_json(404, _NO_SPACE)
    refusal = svc.space_archive_refusal(db, access.workspace_id, space)
    if refusal is not None:
        db.rollback()
        return refusal.response()
    svc.set_archived(space, access.user_id, tt.utc_now())
    _audit(db, request, access.user_id, "UPDATE", "space", space_id,
           f"Archived space {space.key} ({space.name})")
    db.commit()
    logger.info("space archived", extra={"extra_fields": {
        "workspace_id": access.workspace_id, "space_id": space_id}})
    return SuccessResponse(status_code=200, message="Space archived",
                           data=svc.space_node(db, access, space_id))


def unarchive_space(access: WorkspaceAccess, space_id: str, db: Session, request: Request):
    space = svc.lock_space(db, access.workspace_id, space_id)
    if space is None:
        return error_json(404, _NO_SPACE)
    if space.archived_at is None:
        db.rollback()
        return error_json(409, f"{space.name} is not archived.")
    svc.clear_archived(space)
    _audit(db, request, access.user_id, "UPDATE", "space", space_id,
           f"Unarchived space {space.key} ({space.name})")
    db.commit()
    logger.info("space unarchived", extra={"extra_fields": {
        "workspace_id": access.workspace_id, "space_id": space_id}})
    return SuccessResponse(status_code=200, message="Space unarchived",
                           data=svc.space_node(db, access, space_id))


def freeze_space(access: WorkspaceAccess, space_id: str, body: SpaceFreeze, db: Session,
                 request: Request):
    space = svc.lock_space(db, access.workspace_id, space_id)
    if space is None:
        return error_json(404, _NO_SPACE)
    refusal = svc.freeze_refusal(space)
    if refusal is not None:
        db.rollback()
        return refusal.response()
    svc.freeze(space, access.user_id, body.reason, tt.utc_now())
    _audit(db, request, access.user_id, "UPDATE", "space", space_id,
           f"Froze space {space.key}: {body.reason}")
    db.commit()
    logger.info("space frozen", extra={"extra_fields": {
        "workspace_id": access.workspace_id, "space_id": space_id}})
    return SuccessResponse(status_code=200, message="Space frozen",
                           data=svc.space_node(db, access, space_id))


def _replay_deferred(db: Session, workspace_id: str) -> dict | None:
    """Replay the workspace's deferred GitHub deliveries (09-restructuring.md §6.3), or None
    when the replay itself raised.

    Imported here rather than at the top: `controllers/github_controller.py` is another
    lane's, and if it ever imports this module (for `_audit`, say), a top-level import in
    both directions would fail at start-up.

    The unfreeze is already committed when this runs, so a replay that raises must not turn
    the answer into a 500 — the caller would retry, be told the space is not frozen, and
    never learn that the deliveries are still waiting. They stay deferred, an admin can list
    them (`GET …/integrations/deferred`), and the next unfreeze in the workspace replays them.
    """
    from controllers import github_controller

    try:
        result = github_controller.replay_deferred(db, workspace_id)
    except Exception:  # the reason is in the docstring: the unfreeze already happened
        db.rollback()
        logger.exception("replaying deferred deliveries failed", extra={"extra_fields": {
            "workspace_id": workspace_id}})
        return None
    return {"replayed": int(result.get("replayed", 0)),
            "still_deferred": int(result.get("still_deferred", 0)),
            "failed": int(result.get("failed", 0))}


def unfreeze_space(access: WorkspaceAccess, space_id: str, db: Session, request: Request):
    space = svc.lock_space(db, access.workspace_id, space_id)
    if space is None:
        return error_json(404, _NO_SPACE)
    refusal = svc.unfreeze_refusal(db, access.workspace_id, space)
    if refusal is not None:
        db.rollback()
        return refusal.response()
    plan_id = space.frozen_by_plan_id
    svc.unfreeze(space)
    _audit(db, request, access.user_id, "UPDATE", "space", space_id,
           f"Unfroze space {space.key}"
           + (f" (frozen by migration plan {plan_id})" if plan_id else ""))
    db.commit()
    replay = _replay_deferred(db, access.workspace_id)
    logger.info("space unfrozen", extra={"extra_fields": {
        "workspace_id": access.workspace_id, "space_id": space_id, "plan_id": plan_id,
        "replay": replay}})
    message = ("Space unfrozen" if replay is not None
               else "Space unfrozen. Its deferred GitHub deliveries could not be replayed "
                    "now and are still waiting.")
    return SuccessResponse(status_code=200, message=message,
                           data={"space": svc.space_node(db, access, space_id),
                                 "replay": replay})


# ---- folders ----------------------------------------------------------------------------


def _folder_node(db: Session, workspace_id: str, folder_id: str) -> dict | None:
    folder = svc.get_folder(db, workspace_id, folder_id)
    if folder is None:
        return None
    return svc.folder_payload(folder, svc.folder_lists(db, workspace_id, folder_id))


def create_folder(access: WorkspaceAccess, space_id: str, body: FolderCreate, db: Session):
    space = svc.get_space(db, access.workspace_id, space_id)
    if space is None:
        return error_json(404, _NO_SPACE)
    block = write_guard.space_block(space)
    if block is not None:
        return block.response()
    folder = svc.create_folder(db, access.workspace_id, space_id, body.name)
    db.commit()
    return SuccessResponse(status_code=201, message="Folder created",
                           data=svc.folder_payload(folder, []))


def update_folder(access: WorkspaceAccess, folder_id: str, body: FolderUpdate, db: Session,
                  request: Request):
    folder = svc.get_folder(db, access.workspace_id, folder_id)
    if folder is None:
        return error_json(404, _NO_FOLDER)
    block = write_guard.folder_block(db, access.workspace_id, folder)
    if block is not None:
        return block.response()
    fields = body.model_fields_set
    for field in ("name", "position"):
        if field in fields and getattr(body, field) is None:
            return error_json(422, f"{field} cannot be empty.")
    changes: list[str] = []
    if "name" in fields and body.name != folder.name:
        changes.append(f"name {folder.name} -> {body.name}")
        folder.name = body.name
    if "position" in fields and body.position != folder.position:
        changes.append(f"position {body.position}")
        folder.position = body.position
    if changes:
        _audit(db, request, access.user_id, "UPDATE", "folder", folder_id,
               f"Updated folder {folder.name}: {'; '.join(changes)}")
    db.commit()
    return SuccessResponse(status_code=200, message="Folder updated",
                           data=_folder_node(db, access.workspace_id, folder_id))


def archive_folder(access: WorkspaceAccess, folder_id: str, db: Session, request: Request):
    folder = svc.get_folder(db, access.workspace_id, folder_id)
    if folder is None:
        return error_json(404, _NO_FOLDER)
    refusal = svc.container_archive_refusal(db, access.workspace_id, folder)
    if refusal is not None:
        return refusal.response()
    svc.set_archived(folder, access.user_id, tt.utc_now())
    _audit(db, request, access.user_id, "UPDATE", "folder", folder_id,
           f"Archived folder {folder.name}")
    db.commit()
    return SuccessResponse(status_code=200, message="Folder archived",
                           data=_folder_node(db, access.workspace_id, folder_id))


def unarchive_folder(access: WorkspaceAccess, folder_id: str, db: Session, request: Request):
    folder = svc.get_folder(db, access.workspace_id, folder_id)
    if folder is None:
        return error_json(404, _NO_FOLDER)
    refusal = svc.container_unarchive_refusal(db, access.workspace_id, folder)
    if refusal is not None:
        return refusal.response()
    svc.clear_archived(folder)
    _audit(db, request, access.user_id, "UPDATE", "folder", folder_id,
           f"Unarchived folder {folder.name}")
    db.commit()
    return SuccessResponse(status_code=200, message="Folder unarchived",
                           data=_folder_node(db, access.workspace_id, folder_id))


# ---- lists ------------------------------------------------------------------------------


def create_list(access: WorkspaceAccess, body: ListCreate, db: Session):
    space = svc.get_space(db, access.workspace_id, body.space_id)
    if space is None:
        return error_json(404, _NO_SPACE)
    folder = None
    if body.folder_id is not None:
        folder = svc.get_folder(db, access.workspace_id, body.folder_id)
        if folder is None or folder.space_id != body.space_id:
            return error_json(404, "Folder not found in that space")
    block = (write_guard.folder_block(db, access.workspace_id, folder) if folder is not None
             else write_guard.space_block(space))
    if block is not None:
        return block.response()
    item = svc.create_list(db, access.workspace_id, body.space_id, body.folder_id, body.name)
    db.commit()
    return SuccessResponse(status_code=201, message="List created", data=svc.list_payload(item))


def update_list(access: WorkspaceAccess, list_id: str, body: ListUpdate, db: Session,
                request: Request):
    workspace_id = access.workspace_id
    task_list = svc.get_list(db, workspace_id, list_id)
    if task_list is None:
        return error_json(404, _NO_LIST)
    block = write_guard.list_block(db, workspace_id, task_list)
    if block is not None:
        return block.response()
    fields = body.model_fields_set
    for field in ("name", "position"):
        if field in fields and getattr(body, field) is None:
            return error_json(422, f"{field} cannot be empty.")
    changes: list[str] = []
    position = body.position if "position" in fields else None
    if "folder_id" in fields and body.folder_id != task_list.folder_id:
        folder, refusal = svc.list_move_target(db, workspace_id, task_list, body.folder_id)
        if refusal is not None:
            return refusal.response()
        svc.move_list(db, workspace_id, task_list, body.folder_id, position)
        changes.append(f"moved to {f'folder {folder.name}' if folder else 'the space root'}")
    elif position is not None and position != task_list.position:
        task_list.position = position
        changes.append(f"position {position}")
    if "name" in fields and body.name != task_list.name:
        changes.append(f"name {task_list.name} -> {body.name}")
        task_list.name = body.name
    if changes:
        _audit(db, request, access.user_id, "UPDATE", "list", list_id,
               f"Updated list {task_list.name}: {'; '.join(changes)}")
    db.commit()
    return SuccessResponse(status_code=200, message="List updated",
                           data=svc.list_payload(task_list))


def archive_list(access: WorkspaceAccess, list_id: str, db: Session, request: Request):
    task_list = svc.get_list(db, access.workspace_id, list_id)
    if task_list is None:
        return error_json(404, _NO_LIST)
    refusal = svc.container_archive_refusal(db, access.workspace_id, task_list)
    if refusal is not None:
        return refusal.response()
    svc.set_archived(task_list, access.user_id, tt.utc_now())
    _audit(db, request, access.user_id, "UPDATE", "list", list_id,
           f"Archived list {task_list.name}")
    db.commit()
    return SuccessResponse(status_code=200, message="List archived",
                           data=svc.list_payload(task_list))


def unarchive_list(access: WorkspaceAccess, list_id: str, db: Session, request: Request):
    task_list = svc.get_list(db, access.workspace_id, list_id)
    if task_list is None:
        return error_json(404, _NO_LIST)
    refusal = svc.container_unarchive_refusal(db, access.workspace_id, task_list)
    if refusal is not None:
        return refusal.response()
    svc.clear_archived(task_list)
    _audit(db, request, access.user_id, "UPDATE", "list", list_id,
           f"Unarchived list {task_list.name}")
    db.commit()
    return SuccessResponse(status_code=200, message="List unarchived",
                           data=svc.list_payload(task_list))


# ---- governance (09-restructuring.md §3.3) ------------------------------------------------


def get_governance(access: WorkspaceAccess, db: Session):
    return SuccessResponse(status_code=200, message="Governance settings fetched",
                           data=governance_svc.load_settings(db, access.workspace_id)
                           .model_dump())


def update_governance(access: WorkspaceAccess, body: GovernanceSettingsUpdate, db: Session,
                      request: Request):
    workspace = db.query(Workspace).filter(Workspace.id == access.workspace_id).one()
    current = governance_svc.settings_for(workspace)
    changes = {field: getattr(body, field) for field in body.model_fields_set}
    for field, value in changes.items():
        if value is None:
            return error_json(422, f"{field} cannot be empty.")
    if "default_workflow_template" in changes:
        # A default that cannot be applied would refuse every space created without naming
        # a template, so it is checked here, once, rather than discovered there.
        ref = changes["default_workflow_template"]
        stored = svc.template_definition(db, access.workspace_id, ref)
        if stored is None:
            return error_json(422, f"There is no workflow template {ref} here. Use default, "
                                   "delivery or support, or the id of one of this "
                                   "workspace's templates.")
        _definition, problem = svc.prepare_definition(stored)
        if problem is not None:
            return error_json(422, f"That workflow template cannot be applied: {problem}.")
    try:
        updated = GovernanceSettings.model_validate({**current.model_dump(), **changes})
    except ValidationError as exc:
        return error_json(422, "Invalid governance settings.",
                          {"errors": exc.errors(include_url=False, include_context=False)})
    governance_svc.save_settings(workspace, updated)
    if changes:
        summary = ", ".join(f"{k}={v}" for k, v in sorted(changes.items()))
        _audit(db, request, access.user_id, "UPDATE", "workspace_settings", workspace.id,
               f"Governance settings: {summary}")
    db.commit()
    return SuccessResponse(status_code=200, message="Governance settings saved",
                           data=updated.model_dump())


# ---- financial rates (Phase 1) -----------------------------------------------------------


def get_rates(access: WorkspaceAccess, db: Session):
    workspace = svc.get_workspace(db, access.workspace_id)
    if workspace is None:
        return error_json(404, "Workspace not found")

    rate_rows = (
        db.query(WorkspaceMemberRate)
        .filter(WorkspaceMemberRate.workspace_id == access.workspace_id)
        .all()
    )
    rates_by_user = {str(r.user_id): float(r.hourly_rate) for r in rate_rows}

    member_pairs = svc.members(db, access.workspace_id)
    items = []
    for m, u in member_pairs:
        uid = str(u.id)
        display = f"{u.first_name or ''} {u.last_name or ''}".strip() or u.email
        items.append({
            "user_id": uid,
            "email": u.email,
            "display_name": display,
            "role": m.role,
            "hourly_rate": rates_by_user.get(uid),
        })

    return SuccessResponse(
        status_code=200,
        message="Workspace rates fetched",
        data={
            "currency": workspace.currency or "USD",
            "default_billing_rate": float(workspace.default_billing_rate) if workspace.default_billing_rate is not None else None,
            "member_rates": items,
        },
    )


def update_rates(access: WorkspaceAccess, body: WorkspaceRatesUpdate, db: Session, request: Request):
    workspace = svc.get_workspace(db, access.workspace_id)
    if workspace is None:
        return error_json(404, "Workspace not found")

    changes: list[str] = []
    if body.currency is not None:
        new_curr = body.currency.upper().strip()
        if new_curr != workspace.currency:
            changes.append(f"currency {workspace.currency} -> {new_curr}")
            workspace.currency = new_curr

    if "default_billing_rate" in body.model_fields_set:
        changes.append(f"default_billing_rate {workspace.default_billing_rate} -> {body.default_billing_rate}")
        workspace.default_billing_rate = body.default_billing_rate

    if body.member_rates is not None:
        for item in body.member_rates:
            user_id = item.user_id
            existing = (
                db.query(WorkspaceMemberRate)
                .filter(WorkspaceMemberRate.workspace_id == access.workspace_id,
                        WorkspaceMemberRate.user_id == user_id)
                .first()
            )
            if item.hourly_rate is None:
                if existing is not None:
                    db.delete(existing)
                    changes.append(f"cleared rate for user {user_id}")
            else:
                if existing is not None:
                    existing.hourly_rate = item.hourly_rate
                else:
                    new_rate = WorkspaceMemberRate(
                        workspace_id=access.workspace_id,
                        user_id=user_id,
                        hourly_rate=item.hourly_rate,
                    )
                    db.add(new_rate)
                changes.append(f"set rate for user {user_id} to {item.hourly_rate}")

    if changes:
        _audit(db, request, access.user_id, "UPDATE", "workspace_rates", str(workspace.id),
               f"Updated rates: {'; '.join(changes)}")
    db.commit()

    return get_rates(access, db)


# ---- repositories -----------------------------------------------------------------------


def list_repositories(access: WorkspaceAccess, db: Session):
    return SuccessResponse(
        status_code=200,
        message="Repositories fetched",
        data=[svc.repository_payload(r) for r in svc.repositories(db, access.workspace_id)],
    )


def link_repository(access: WorkspaceAccess, body: RepositoryLink, db: Session,
                    request: Request):
    existing = svc.repository_by_name(db, "github", body.full_name)
    if existing is not None:
        # Same answer whichever workspace holds it: telling a caller that *another* tenant
        # has linked a repository would disclose that tenant's toolchain.
        return error_json(409, "That repository is already linked to a workspace.")
    repo = VcsRepository(workspace_id=access.workspace_id, provider="github",
                         full_name=body.full_name, created_by=access.user_id)
    db.add(repo)
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        return error_json(409, "That repository is already linked to a workspace.")
    _audit(db, request, access.user_id, "CREATE", "repository", repo.id,
           f"Linked repository {body.full_name}")
    db.commit()
    return SuccessResponse(status_code=201, message="Repository linked",
                           data=svc.repository_payload(repo))
