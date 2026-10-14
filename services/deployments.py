"""Services for CI/CD deployments and issue deployment markers."""
from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func
from sqlalchemy.orm import Session

from models.deployment import Deployment, IssueDeployment
from models.issue import Issue, IssueEvent
from schemas.deployments import DeploymentCreate, DeploymentResponse, IssueDeploymentSummary
from services import key_resolution
from utils.issue_keys import extract_issue_keys


def _now() -> datetime:
    return datetime.now(UTC)


def create_deployment(
    db: Session,
    workspace_id: str,
    payload: DeploymentCreate,
) -> DeploymentResponse:
    started_at = payload.started_at or _now()
    deployment = Deployment(
        workspace_id=workspace_id,
        repository_id=payload.repository_id,
        environment=payload.environment,
        status=payload.status,
        commit_sha=payload.commit_sha.strip(),
        ref_name=payload.ref_name.strip() if payload.ref_name else None,
        deployer_name=payload.deployer_name.strip() if payload.deployer_name else None,
        deployment_url=payload.deployment_url.strip() if payload.deployment_url else None,
        started_at=started_at,
        finished_at=payload.finished_at,
    )
    db.add(deployment)
    db.flush()

    # Extract issue keys from ref_name, commit_messages, and explicit issue_keys
    candidate_keys: list[tuple[str, int]] = []
    if payload.ref_name:
        for k in extract_issue_keys(payload.ref_name, case_insensitive=True):
            if k not in candidate_keys:
                candidate_keys.append(k)
    for msg in payload.commit_messages:
        for k in extract_issue_keys(msg):
            if k not in candidate_keys:
                candidate_keys.append(k)
    for k_str in payload.issue_keys:
        for k in extract_issue_keys(k_str, case_insensitive=True):
            if k not in candidate_keys:
                candidate_keys.append(k)

    linked_issue_ids: set[str] = set()
    for space_key, num in candidate_keys:
        resolved = key_resolution.resolve_key(db, workspace_id, space_key, num)
        if resolved:
            issue, _ = resolved
            if issue.id not in linked_issue_ids:
                linked_issue_ids.add(issue.id)
                db.add(IssueDeployment(
                    workspace_id=workspace_id,
                    issue_id=issue.id,
                    deployment_id=deployment.id,
                ))

                if payload.status == "success":
                    db.add(IssueEvent(
                        workspace_id=workspace_id,
                        issue_id=issue.id,
                        actor_id=None,
                        actor_kind="integration",
                        event="deployed",
                        to_value=f"{payload.environment}:{payload.commit_sha[:7]}",
                    ))

    db.commit()
    db.refresh(deployment)

    return DeploymentResponse(
        id=deployment.id,
        workspace_id=deployment.workspace_id,
        repository_id=deployment.repository_id,
        environment=deployment.environment,
        status=deployment.status,
        commit_sha=deployment.commit_sha,
        ref_name=deployment.ref_name,
        deployer_name=deployment.deployer_name,
        deployment_url=deployment.deployment_url,
        started_at=deployment.started_at,
        finished_at=deployment.finished_at,
        created_at=deployment.created_at,
        shipped_issue_count=len(linked_issue_ids),
    )


def list_deployments(
    db: Session,
    workspace_id: str,
    environment: str | None = None,
    limit: int = 50,
) -> list[DeploymentResponse]:
    query = (
        db.query(Deployment)
        .filter(Deployment.workspace_id == workspace_id)
    )
    if environment:
        query = query.filter(Deployment.environment == environment)

    deployments = query.order_by(Deployment.created_at.desc()).limit(limit).all()
    if not deployments:
        return []

    dep_ids = [d.id for d in deployments]
    counts = dict(
        db.query(IssueDeployment.deployment_id, func.count(IssueDeployment.id))
        .filter(IssueDeployment.deployment_id.in_(dep_ids))
        .group_by(IssueDeployment.deployment_id)
        .all()
    )

    return [
        DeploymentResponse(
            id=d.id,
            workspace_id=d.workspace_id,
            repository_id=d.repository_id,
            environment=d.environment,
            status=d.status,
            commit_sha=d.commit_sha,
            ref_name=d.ref_name,
            deployer_name=d.deployer_name,
            deployment_url=d.deployment_url,
            started_at=d.started_at,
            finished_at=d.finished_at,
            created_at=d.created_at,
            shipped_issue_count=counts.get(d.id, 0),
        )
        for d in deployments
    ]


def get_issue_deployments(
    db: Session,
    workspace_id: str,
    issue_id: str,
) -> list[IssueDeploymentSummary]:
    results = (
        db.query(Deployment)
        .join(IssueDeployment, IssueDeployment.deployment_id == Deployment.id)
        .filter(
            IssueDeployment.workspace_id == workspace_id,
            IssueDeployment.issue_id == issue_id,
        )
        .order_by(Deployment.started_at.desc())
        .all()
    )

    return [
        IssueDeploymentSummary(
            deployment_id=d.id,
            environment=d.environment,
            status=d.status,
            commit_sha=d.commit_sha,
            ref_name=d.ref_name,
            deployment_url=d.deployment_url,
            deployed_at=d.finished_at or d.started_at,
        )
        for d in results
    ]
