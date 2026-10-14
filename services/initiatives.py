"""Services for cross-space portfolio initiatives (Epics 2.0)."""
from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import func
from sqlalchemy.orm import Session

from models.hierarchy import Space, WorkflowStatus
from models.initiative import Initiative, InitiativeIssue, InitiativeSpace
from models.issue import Issue
from models.time_tracking import TimeEntry
from models.user import User
from schemas.initiatives import (
    InitiativeCreate,
    InitiativeDetailResponse,
    InitiativeHealth,
    InitiativeIssueSummary,
    InitiativeResponse,
    InitiativeRollup,
    InitiativeSpaceSummary,
    InitiativeUpdate,
)
from utils.issue_keys import format_issue_key


def _compute_rollup(
    db: Session,
    workspace_id: str,
    initiative: Initiative,
    linked_issues: list[Issue],
    statuses: dict[str, WorkflowStatus],
) -> InitiativeRollup:
    total_issues = len(linked_issues)
    completed_issues = 0
    total_points = 0.0
    completed_points = 0.0

    for issue in linked_issues:
        pts = float(issue.story_points or 0.0)
        total_points += pts
        st = statuses.get(issue.status_id)
        is_done = st.category == "done" if st else False
        if is_done:
            completed_issues += 1
            completed_points += pts

    progress_pct = 0.0
    if total_points > 0:
        progress_pct = round((completed_points / total_points) * 100, 1)
    elif total_issues > 0:
        progress_pct = round((completed_issues / total_issues) * 100, 1)

    # Health computation
    health: InitiativeHealth = "planned"
    if initiative.status == "completed":
        health = "on_track"
    elif initiative.status == "cancelled":
        health = "off_track"
    elif initiative.start_date and initiative.target_date and initiative.target_date > initiative.start_date:
        today = datetime.now(UTC).date()
        if today <= initiative.start_date:
            time_elapsed_pct = 0.0
        elif today >= initiative.target_date:
            time_elapsed_pct = 100.0
        else:
            time_elapsed_pct = (
                (today - initiative.start_date).days
                / (initiative.target_date - initiative.start_date).days
            ) * 100.0

        if progress_pct >= time_elapsed_pct - 10.0:
            health = "on_track"
        elif progress_pct >= time_elapsed_pct - 25.0:
            health = "at_risk"
        else:
            health = "off_track"
    else:
        health = "on_track" if progress_pct > 0 or initiative.status == "active" else "planned"

    # Financial cost rollup
    linked_issue_ids = [i.id for i in linked_issues]
    actual_spent = 0.0
    if linked_issue_ids:
        spent_sum = (
            db.query(func.coalesce(func.sum(TimeEntry.cost_amount), 0.0))
            .filter(
                TimeEntry.workspace_id == workspace_id,
                TimeEntry.issue_id.in_(linked_issue_ids),
                TimeEntry.deleted == 0,
            )
            .scalar()
        )
        actual_spent = round(float(spent_sum or 0.0), 2)

    budget = float(initiative.budget_amount) if initiative.budget_amount is not None else None
    budget_burn_pct = None
    if budget and budget > 0:
        budget_burn_pct = round((actual_spent / budget) * 100, 1)

    return InitiativeRollup(
        total_issues=total_issues,
        completed_issues=completed_issues,
        total_points=round(total_points, 1),
        completed_points=round(completed_points, 1),
        progress_percentage=progress_pct,
        health=health,
        actual_spent_amount=actual_spent,
        budget_amount=budget,
        budget_burn_percentage=budget_burn_pct,
    )


def create_initiative(
    db: Session,
    workspace_id: str,
    payload: InitiativeCreate,
    creator_id: str,
) -> InitiativeDetailResponse:
    existing = (
        db.query(Initiative)
        .filter(
            Initiative.workspace_id == workspace_id,
            func.lower(Initiative.key) == payload.key.strip().lower(),
            Initiative.deleted == 0,
        )
        .first()
    )
    if existing:
        raise ValueError(f"Initiative with key '{payload.key}' already exists in this workspace")

    initiative = Initiative(
        workspace_id=workspace_id,
        key=payload.key.strip().upper(),
        title=payload.title.strip(),
        description=payload.description.strip() if payload.description else None,
        status=payload.status,
        owner_id=payload.owner_id,
        target_quarter=payload.target_quarter,
        start_date=payload.start_date,
        target_date=payload.target_date,
        budget_amount=payload.budget_amount,
        created_by=creator_id,
    )
    db.add(initiative)
    db.flush()

    for space_id in payload.space_ids:
        # Verify space exists in workspace
        sp = (
            db.query(Space)
            .filter(Space.id == space_id, Space.workspace_id == workspace_id, Space.deleted == 0)
            .first()
        )
        if sp:
            db.add(InitiativeSpace(
                initiative_id=initiative.id,
                space_id=sp.id,
                workspace_id=workspace_id,
            ))

    db.commit()
    db.refresh(initiative)
    return get_initiative(db, workspace_id, initiative.id)


def get_initiative(db: Session, workspace_id: str, initiative_id: str) -> InitiativeDetailResponse:
    initiative = (
        db.query(Initiative)
        .filter(
            Initiative.id == initiative_id,
            Initiative.workspace_id == workspace_id,
            Initiative.deleted == 0,
        )
        .first()
    )
    if not initiative:
        raise ValueError("Initiative not found")

    owner = db.query(User).filter(User.id == initiative.owner_id).first() if initiative.owner_id else None

    # Load contributing spaces
    space_links = db.query(InitiativeSpace).filter(InitiativeSpace.initiative_id == initiative.id).all()
    space_ids = [sl.space_id for sl in space_links]
    spaces = (
        db.query(Space)
        .filter(Space.workspace_id == workspace_id, Space.id.in_(space_ids), Space.deleted == 0)
        .all()
    ) if space_ids else []
    space_map = {s.id: s for s in spaces}

    # Load linked issues
    issue_links = db.query(InitiativeIssue).filter(InitiativeIssue.initiative_id == initiative.id).all()
    linked_issue_ids = [il.issue_id for il in issue_links]
    issues = (
        db.query(Issue)
        .filter(Issue.workspace_id == workspace_id, Issue.id.in_(linked_issue_ids), Issue.deleted == 0)
        .all()
    ) if linked_issue_ids else []

    all_space_ids = set(space_ids) | {i.space_id for i in issues}
    if all_space_ids:
        for s in db.query(Space).filter(Space.workspace_id == workspace_id, Space.id.in_(all_space_ids)).all():
            space_map[s.id] = s

    # Load statuses
    status_ids = [i.status_id for i in issues]
    statuses = {
        st.id: st
        for st in db.query(WorkflowStatus)
        .filter(WorkflowStatus.workspace_id == workspace_id, WorkflowStatus.id.in_(status_ids))
        .all()
    } if status_ids else {}

    rollup = _compute_rollup(db, workspace_id, initiative, issues, statuses)

    contributing_spaces = [
        InitiativeSpaceSummary(id=s.id, key=s.key, name=s.name)
        for s in spaces
    ]

    linked_issue_summaries = []
    for issue in issues:
        sp = space_map.get(issue.space_id)
        st = statuses.get(issue.status_id)
        linked_issue_summaries.append(
            InitiativeIssueSummary(
                id=issue.id,
                key=format_issue_key(sp.key if sp else "??", issue.number),
                title=issue.title,
                type=issue.type,
                status_name=st.name if st else "Unknown",
                status_category=st.category if st else "todo",
                story_points=issue.story_points,
                space_id=issue.space_id,
                space_name=sp.name if sp else "Unknown",
            )
        )

    return InitiativeDetailResponse(
        id=initiative.id,
        workspace_id=initiative.workspace_id,
        key=initiative.key,
        title=initiative.title,
        description=initiative.description,
        status=initiative.status,
        owner_id=initiative.owner_id,
        owner_name=owner.display_name if owner else (owner.username if owner else None),
        target_quarter=initiative.target_quarter,
        start_date=initiative.start_date,
        target_date=initiative.target_date,
        budget_amount=float(initiative.budget_amount) if initiative.budget_amount is not None else None,
        created_by=initiative.created_by,
        created_at=initiative.created_at,
        updated_at=initiative.updated_at,
        rollup=rollup,
        contributing_spaces=contributing_spaces,
        linked_issues=linked_issue_summaries,
    )


def list_initiatives(db: Session, workspace_id: str) -> list[InitiativeResponse]:
    initiatives = (
        db.query(Initiative)
        .filter(Initiative.workspace_id == workspace_id, Initiative.deleted == 0)
        .order_by(Initiative.created_at.desc())
        .all()
    )
    if not initiatives:
        return []

    init_ids = [i.id for i in initiatives]
    owner_ids = [i.owner_id for i in initiatives if i.owner_id]
    owners = {u.id: u for u in db.query(User).filter(User.id.in_(owner_ids)).all()} if owner_ids else {}

    # Bulk query all linked issues for rollups
    issue_links = db.query(InitiativeIssue).filter(InitiativeIssue.initiative_id.in_(init_ids)).all()
    init_issue_map: dict[str, list[str]] = {}
    all_issue_ids = []
    for il in issue_links:
        init_issue_map.setdefault(il.initiative_id, []).append(il.issue_id)
        all_issue_ids.append(il.issue_id)

    issues_by_id = {
        i.id: i
        for i in db.query(Issue)
        .filter(Issue.workspace_id == workspace_id, Issue.id.in_(all_issue_ids), Issue.deleted == 0)
        .all()
    } if all_issue_ids else {}

    status_ids = [i.status_id for i in issues_by_id.values()]
    statuses = {
        st.id: st
        for st in db.query(WorkflowStatus)
        .filter(WorkflowStatus.workspace_id == workspace_id, WorkflowStatus.id.in_(status_ids))
        .all()
    } if status_ids else {}

    results = []
    for init in initiatives:
        owner = owners.get(init.owner_id) if init.owner_id else None
        issue_ids = init_issue_map.get(init.id, [])
        linked_issues = [issues_by_id[iid] for iid in issue_ids if iid in issues_by_id]
        rollup = _compute_rollup(db, workspace_id, init, linked_issues, statuses)

        results.append(
            InitiativeResponse(
                id=init.id,
                workspace_id=init.workspace_id,
                key=init.key,
                title=init.title,
                description=init.description,
                status=init.status,
                owner_id=init.owner_id,
                owner_name=owner.display_name if owner else (owner.username if owner else None),
                target_quarter=init.target_quarter,
                start_date=init.start_date,
                target_date=init.target_date,
                budget_amount=float(init.budget_amount) if init.budget_amount is not None else None,
                created_by=init.created_by,
                created_at=init.created_at,
                updated_at=init.updated_at,
                rollup=rollup,
            )
        )
    return results


def update_initiative(
    db: Session,
    workspace_id: str,
    initiative_id: str,
    payload: InitiativeUpdate,
) -> InitiativeDetailResponse:
    initiative = (
        db.query(Initiative)
        .filter(
            Initiative.id == initiative_id,
            Initiative.workspace_id == workspace_id,
            Initiative.deleted == 0,
        )
        .first()
    )
    if not initiative:
        raise ValueError("Initiative not found")

    if payload.title is not None:
        initiative.title = payload.title.strip()
    if payload.description is not None:
        initiative.description = payload.description.strip() if payload.description else None
    if payload.status is not None:
        initiative.status = payload.status
    if payload.owner_id is not None:
        initiative.owner_id = payload.owner_id
    if payload.target_quarter is not None:
        initiative.target_quarter = payload.target_quarter
    if payload.start_date is not None:
        initiative.start_date = payload.start_date
    if payload.target_date is not None:
        initiative.target_date = payload.target_date
    if payload.budget_amount is not None:
        initiative.budget_amount = payload.budget_amount

    db.commit()
    db.refresh(initiative)
    return get_initiative(db, workspace_id, initiative.id)


def delete_initiative(db: Session, workspace_id: str, initiative_id: str) -> None:
    initiative = (
        db.query(Initiative)
        .filter(
            Initiative.id == initiative_id,
            Initiative.workspace_id == workspace_id,
            Initiative.deleted == 0,
        )
        .first()
    )
    if not initiative:
        raise ValueError("Initiative not found")

    initiative.deleted = 1
    db.commit()


def link_spaces(
    db: Session,
    workspace_id: str,
    initiative_id: str,
    space_ids: list[str],
) -> InitiativeDetailResponse:
    initiative = (
        db.query(Initiative)
        .filter(
            Initiative.id == initiative_id,
            Initiative.workspace_id == workspace_id,
            Initiative.deleted == 0,
        )
        .first()
    )
    if not initiative:
        raise ValueError("Initiative not found")

    for sid in space_ids:
        sp = (
            db.query(Space)
            .filter(Space.id == sid, Space.workspace_id == workspace_id, Space.deleted == 0)
            .first()
        )
        if not sp:
            continue
        existing = (
            db.query(InitiativeSpace)
            .filter(InitiativeSpace.initiative_id == initiative_id, InitiativeSpace.space_id == sid)
            .first()
        )
        if not existing:
            db.add(InitiativeSpace(
                initiative_id=initiative_id,
                space_id=sid,
                workspace_id=workspace_id,
            ))

    db.commit()
    return get_initiative(db, workspace_id, initiative_id)


def unlink_space(
    db: Session,
    workspace_id: str,
    initiative_id: str,
    space_id: str,
) -> InitiativeDetailResponse:
    link = (
        db.query(InitiativeSpace)
        .filter(
            InitiativeSpace.initiative_id == initiative_id,
            InitiativeSpace.space_id == space_id,
            InitiativeSpace.workspace_id == workspace_id,
        )
        .first()
    )
    if link:
        db.delete(link)
        db.commit()
    return get_initiative(db, workspace_id, initiative_id)


def link_issues(
    db: Session,
    workspace_id: str,
    initiative_id: str,
    issue_ids: list[str],
) -> InitiativeDetailResponse:
    initiative = (
        db.query(Initiative)
        .filter(
            Initiative.id == initiative_id,
            Initiative.workspace_id == workspace_id,
            Initiative.deleted == 0,
        )
        .first()
    )
    if not initiative:
        raise ValueError("Initiative not found")

    for iid in issue_ids:
        issue = (
            db.query(Issue)
            .filter(Issue.id == iid, Issue.workspace_id == workspace_id, Issue.deleted == 0)
            .first()
        )
        if not issue:
            continue
        existing = (
            db.query(InitiativeIssue)
            .filter(InitiativeIssue.initiative_id == initiative_id, InitiativeIssue.issue_id == iid)
            .first()
        )
        if not existing:
            db.add(InitiativeIssue(
                initiative_id=initiative_id,
                issue_id=iid,
                workspace_id=workspace_id,
            ))

    db.commit()
    return get_initiative(db, workspace_id, initiative_id)


def unlink_issue(
    db: Session,
    workspace_id: str,
    initiative_id: str,
    issue_id: str,
) -> InitiativeDetailResponse:
    link = (
        db.query(InitiativeIssue)
        .filter(
            InitiativeIssue.initiative_id == initiative_id,
            InitiativeIssue.issue_id == issue_id,
            InitiativeIssue.workspace_id == workspace_id,
        )
        .first()
    )
    if link:
        db.delete(link)
        db.commit()
    return get_initiative(db, workspace_id, initiative_id)
