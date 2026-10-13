"""Sprints — the transaction boundary for planning, starting and closing sprints.

| Act | guest | member | admin / owner |
|---|---|---|---|
| See sprints and what is in them | yes | yes | yes |
| Create, rename, re-date a sprint; move issues in and out | — | yes | yes |
| Start, close or delete a sprint | per `sprint_manage_role` (default: admin) |||

Starting and closing are the two acts that freeze numbers the velocity chart reads later
(`committed_points`, `completed_points`), which is why they sit behind a setting and are
audited; planning is everyday work and is not. Jira draws the line in the same place
("Manage sprints" permission).

A sprint in another workspace, or an issue in another project, is a 404 — the tenancy rule of
every lookup here (CLAUDE.md §8).

**Archived and frozen projects** (docs/design/09-restructuring.md §3.4) refuse every sprint
write with 423, through `services/write_guard` — the one place that decides it. Creating,
changing, deleting, starting and closing a sprint ask about its project; moving issues in or
out also asks about each issue's own list and folder, because an issue's sprint membership
is a write to the issue. Closing with the unfinished issues carried into another sprint is
that same move, and asks the same question of each of them. Reads never ask. The order is
404, then 403, then 423, then the state checks (409): who may act on what comes before
whether it can be acted on now, and a locked project says so before a closed sprint does.
"""
from __future__ import annotations

from fastapi import Request
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from controllers.workspace_controller import _audit
from dependencies.workspace import WorkspaceAccess
from models.hierarchy import Space
from models.issue import Issue
from models.sprint import Sprint
from schemas.response import SuccessResponse, error_json
from schemas.sprints import SprintClose, SprintCreate, SprintIssues, SprintStart, SprintUpdate
from services import issue as issue_svc
from services import sprint as svc
from services import workspace as workspace_svc
from services import write_guard
from services.reports import settings as report_settings
from utils import report_math as rm
from utils import time_tracking as tt
from utils.issue_keys import format_issue_key

_NOT_FOUND = "Sprint not found"
_NO_MANAGE = "Your role cannot start, close or delete sprints in this workspace."


def _payload(db: Session, sprint: Sprint) -> dict:
    return svc.payloads(db, [sprint])[0]


def _locked(db: Session, access: WorkspaceAccess, sprint: Sprint) -> JSONResponse | None:
    """The 423 for a sprint whose project is archived or frozen, or None when it is writable."""
    block = write_guard.space_id_block(db, access.workspace_id, str(sprint.space_id))
    return block.response() if block is not None else None


def _first_locked_issue(db: Session,
                        issues: list[Issue]) -> tuple[Issue, write_guard.WriteBlock] | None:
    """The first of `issues` the write guard refuses, and why; None when all are writable.

    Asked once per list rather than once per issue: the answer is the list's, its folder's
    and its space's (`write_guard.issue_block`), so two issues of one list always get the
    same one, and a 200-issue planning drag costs a few lookups instead of six hundred.
    """
    asked: set[str] = set()
    for issue in issues:
        if str(issue.list_id) in asked:
            continue
        asked.add(str(issue.list_id))
        block = write_guard.issue_block(db, issue)
        if block is not None:
            return issue, block
    return None


def _key(db: Session, access: WorkspaceAccess, issue: Issue) -> str:
    space_key = (db.query(Space.key)
                 .filter(Space.workspace_id == access.workspace_id, Space.id == issue.space_id)
                 .scalar())
    return format_issue_key(str(space_key or "?"), int(issue.number))


def _may_manage(db: Session, access: WorkspaceAccess) -> bool:
    return access.at_least(report_settings.load_settings(db, access.workspace_id)
                           .sprint_manage_role)


def _today(db: Session, access: WorkspaceAccess):
    zone_name = report_settings.load_settings(db, access.workspace_id).timezone
    return rm.local_today(tt.utc_now(), rm.zone(zone_name))


def list_sprints(access: WorkspaceAccess, space_id: str, state: str | None, db: Session):
    if workspace_svc.get_space(db, access.workspace_id, space_id) is None:
        return error_json(404, "Project not found")
    states = [s for s in (state or "").split(",") if s] or None
    sprints = svc.space_sprints(db, access.workspace_id, space_id, states)
    return SuccessResponse(status_code=200, message="Sprints fetched",
                           data=svc.payloads(db, sprints))


def create_sprint(access: WorkspaceAccess, space_id: str, body: SprintCreate, db: Session):
    space = workspace_svc.get_space(db, access.workspace_id, space_id)
    if space is None:
        return error_json(404, "Project not found")
    block = write_guard.space_block(space)
    if block is not None:
        return block.response()
    sprint = svc.create_sprint(db, access.workspace_id, space_id, access.user_id,
                               name=body.name, goal=body.goal, starts_on=body.starts_on,
                               ends_on=body.ends_on)
    db.commit()
    return SuccessResponse(status_code=201, message="Sprint created",
                           data=_payload(db, sprint))


def get_sprint(access: WorkspaceAccess, sprint_id: str, db: Session):
    sprint = svc.get_sprint(db, access.workspace_id, sprint_id)
    if sprint is None:
        return error_json(404, _NOT_FOUND)
    return SuccessResponse(status_code=200, message="Sprint fetched", data=_payload(db, sprint))


def update_sprint(access: WorkspaceAccess, sprint_id: str, body: SprintUpdate, db: Session):
    sprint = svc.get_sprint(db, access.workspace_id, sprint_id)
    if sprint is None:
        return error_json(404, _NOT_FOUND)
    locked = _locked(db, access, sprint)
    if locked is not None:
        return locked
    if sprint.state == "closed":
        return error_json(409, "A closed sprint cannot be changed.")
    changes = body.model_fields_set
    if "name" in changes and body.name is None:
        return error_json(422, "name cannot be empty.")
    starts_on = body.starts_on if "starts_on" in changes else sprint.starts_on
    ends_on = body.ends_on if "ends_on" in changes else sprint.ends_on
    if sprint.state == "active" and (starts_on is None or ends_on is None):
        return error_json(422, "An active sprint needs both dates.")
    if starts_on and ends_on and ends_on < starts_on:
        return error_json(422, "ends_on is before starts_on")
    if "name" in changes and body.name is not None:
        sprint.name = body.name.strip()
    if "goal" in changes:
        sprint.goal = body.goal
    sprint.starts_on, sprint.ends_on = starts_on, ends_on
    db.commit()
    return SuccessResponse(status_code=200, message="Sprint updated", data=_payload(db, sprint))


def delete_sprint(access: WorkspaceAccess, sprint_id: str, db: Session, request: Request):
    sprint = svc.get_sprint(db, access.workspace_id, sprint_id)
    if sprint is None:
        return error_json(404, _NOT_FOUND)
    if not _may_manage(db, access):
        return error_json(403, _NO_MANAGE)
    locked = _locked(db, access, sprint)
    if locked is not None:
        return locked
    if sprint.state != "planned":
        return error_json(409, "Only a sprint that has not started can be deleted.")
    moved = svc.delete_sprint(db, sprint, access.user_id, tt.utc_now())
    _audit(db, request, access.user_id, "DELETE", "sprint", sprint.id,
           f"Deleted sprint {sprint.name}; {moved} issue(s) back to the backlog")
    db.commit()
    return SuccessResponse(status_code=200, message="Sprint deleted",
                           data={"id": sprint.id, "issues_moved": moved})


def start_sprint(access: WorkspaceAccess, sprint_id: str, body: SprintStart, db: Session,
                 request: Request):
    sprint = svc.get_sprint(db, access.workspace_id, sprint_id)
    if sprint is None:
        return error_json(404, _NOT_FOUND)
    if not _may_manage(db, access):
        return error_json(403, _NO_MANAGE)
    locked = _locked(db, access, sprint)
    if locked is not None:
        return locked
    if sprint.state != "planned":
        return error_json(409, "Only a planned sprint can be started.")
    if body.starts_on and body.ends_on and body.ends_on < body.starts_on:
        return error_json(422, "ends_on is before starts_on")
    svc.lock_space(db, sprint)
    active = svc.active_sprint(db, access.workspace_id, sprint.space_id)
    if active is not None:
        db.rollback()
        return error_json(409, f"{active.name} is already active in this project. Close it "
                               "first.", {"active_sprint_id": active.id})
    svc.start_sprint(db, sprint, today=_today(db, access), now=tt.utc_now(),
                     starts_on=body.starts_on, ends_on=body.ends_on)
    _audit(db, request, access.user_id, "UPDATE", "sprint", sprint.id,
           f"Started sprint {sprint.name} ({sprint.starts_on} to {sprint.ends_on}, "
           f"{sprint.committed_points} points committed)")
    db.commit()
    return SuccessResponse(status_code=200, message="Sprint started", data=_payload(db, sprint))


def close_sprint(access: WorkspaceAccess, sprint_id: str, body: SprintClose, db: Session,
                 request: Request):
    sprint = svc.get_sprint(db, access.workspace_id, sprint_id)
    if sprint is None:
        return error_json(404, _NOT_FOUND)
    if not _may_manage(db, access):
        return error_json(403, _NO_MANAGE)
    locked = _locked(db, access, sprint)
    if locked is not None:
        return locked
    if sprint.state != "active":
        return error_json(409, "Only the active sprint can be closed.")
    target = None
    if body.carry_over == "sprint":
        target = svc.get_sprint(db, access.workspace_id, body.target_sprint_id or "")
        if target is None or target.space_id != sprint.space_id:
            return error_json(404, "Target sprint not found in this project")
        if target.state != "planned":
            return error_json(409, "Unfinished issues can only move to a planned sprint.")
        # Carrying an issue into another sprint is adding it there, so it asks what adding
        # asks. Refused explicitly rather than quietly sending that one issue to the backlog:
        # the person chose the target, and a close that half-follows it would be a surprise
        # found later in the next sprint's plan. Ending its membership of THIS sprint is the
        # close's own bookkeeping and is not asked about, so closing to the backlog works.
        _done, _canceled, unfinished = svc.split_by_outcome(db, sprint)
        refused = _first_locked_issue(db, unfinished)
        if refused is not None:
            issue, block = refused
            return error_json(block.status_code,
                              f"{_key(db, access, issue)} cannot be carried into "
                              f"{target.name}. {block.message} Close the sprint with its "
                              "unfinished issues going to the backlog instead.",
                              {"issue_id": issue.id})
    outcome = svc.close_sprint(db, sprint, access.user_id, tt.utc_now(), target)
    _audit(db, request, access.user_id, "UPDATE", "sprint", sprint.id,
           f"Closed sprint {sprint.name}: {outcome['completed_issues']} done, "
           f"{outcome['canceled_issues']} canceled, {outcome['carried_over']} carried to "
           f"{target.name if target is not None else 'the backlog'}")
    db.commit()
    return SuccessResponse(status_code=200, message="Sprint closed",
                           data={"sprint": _payload(db, sprint), **outcome})


def sprint_issues(access: WorkspaceAccess, sprint_id: str, db: Session):
    sprint = svc.get_sprint(db, access.workspace_id, sprint_id)
    if sprint is None:
        return error_json(404, _NOT_FOUND)
    return SuccessResponse(status_code=200, message="Sprint issues fetched",
                           data=issue_svc.cards(db, svc.sprint_issues(db, sprint)))


def _issues_of(db: Session, sprint: Sprint, ids: list[str]) -> tuple[list[Issue], str | None]:
    """The named issues, all live and in the sprint's project — or the id that is not."""
    found = {issue.id: issue for issue in db.query(Issue).filter(
        Issue.workspace_id == sprint.workspace_id, Issue.id.in_(set(ids)),
        Issue.deleted == 0, Issue.space_id == sprint.space_id)}
    for issue_id in ids:
        if issue_id not in found:
            return [], issue_id
    seen: dict[str, Issue] = {}
    for issue_id in ids:
        seen.setdefault(issue_id, found[issue_id])
    return list(seen.values()), None


def change_issues(access: WorkspaceAccess, sprint_id: str, body: SprintIssues, add: bool,
                  db: Session):
    sprint = svc.get_sprint(db, access.workspace_id, sprint_id)
    if sprint is None:
        return error_json(404, _NOT_FOUND)
    locked = _locked(db, access, sprint)
    if locked is not None:
        return locked
    if sprint.state == "closed":
        return error_json(409, "A closed sprint cannot change.")
    issues, missing = _issues_of(db, sprint, body.issue_ids)
    if missing is not None:
        return error_json(404, "Issue not found in this sprint's project", {"issue_id": missing})
    # All or nothing, like the 404 above: one issue in an archived list refuses the batch,
    # so a drag of ten never half-lands.
    refused = _first_locked_issue(db, issues)
    if refused is not None:
        issue, block = refused
        return error_json(block.status_code,
                          f"{_key(db, access, issue)} cannot be "
                          f"{'added to' if add else 'removed from'} the sprint. "
                          f"{block.message}", {"issue_id": issue.id})
    now = tt.utc_now()
    changed, unchanged = [], []
    for issue in issues:
        did = (svc.add_issue(db, sprint, issue, access.user_id, now) if add
               else svc.remove_issue(db, sprint, issue, access.user_id, now))
        (changed if did else unchanged).append(issue.id)
    db.commit()
    return SuccessResponse(status_code=200,
                           message="Issues added to sprint" if add else "Issues removed",
                           data={"sprint": _payload(db, sprint), "changed": changed,
                                 "unchanged": unchanged})
