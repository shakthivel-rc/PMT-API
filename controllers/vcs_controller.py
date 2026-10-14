"""Controllers for GitLab and Bitbucket webhook deliveries."""
from __future__ import annotations

import logging
import uuid
from typing import Any

from sqlalchemy.orm import Session

from integrations.bitbucket import BitbucketEvent, parse_bitbucket_event
from integrations.gitlab import GitLabEvent, parse_gitlab_event
from models.hierarchy import Space, WorkflowStatus
from models.issue import Issue
from models.vcs import VcsLink, VcsRepository, WebhookDelivery
from schemas.deployments import DeploymentCreate
from services import deployments as deployment_service
from services import key_resolution, workspace as workspace_service

logger = logging.getLogger("base_project.vcs")


def apply_gitlab_delivery(
    db: Session,
    event_header: str | None,
    delivery_id: str,
    payload: dict[str, Any],
) -> dict[str, Any]:
    event: GitLabEvent | None = parse_gitlab_event(event_header, payload)
    if not event:
        return {"handled": False, "note": "Unrecognized or unhandled GitLab event"}

    # Record delivery deduplication
    existing = db.query(WebhookDelivery).filter(WebhookDelivery.id == delivery_id).first()
    if existing:
        return {"handled": False, "duplicate": True, "note": "Duplicate delivery"}

    delivery = WebhookDelivery(
        id=delivery_id,
        provider="gitlab",
        event=event.event_type,
        status="processed",
    )
    db.add(delivery)

    repo = workspace_service.repository_by_name(db, "gitlab", event.repository)
    if not repo:
        db.commit()
        return {"handled": False, "note": f"Repository '{event.repository}' not registered"}

    delivery.workspace_id = repo.workspace_id

    linked_keys: list[str] = []
    # If pipeline or deployment event with a commit SHA, create a deployment
    if event.event_type in ("pipeline", "deployment") and event.sha:
        status_map = {
            "success": "success",
            "failed": "failed",
            "running": "in_progress",
            "canceled": "rolled_back",
        }
        dep_status = status_map.get((event.status or "").lower(), "in_progress")
        dep_env = event.environment or "production"
        issue_key_strs = [f"{k}-{num}" for k, num in event.keys]
        dep_create = DeploymentCreate(
            environment=dep_env if dep_env in ("development", "staging", "production", "canary") else "production",
            status=dep_status if dep_status in ("in_progress", "success", "failed", "rolled_back") else "in_progress",
            commit_sha=event.sha,
            ref_name=event.ref,
            deployer_name=event.author_name,
            repository_id=repo.id,
            issue_keys=issue_key_strs,
        )
        deployment_service.create_deployment(db, repo.workspace_id, dep_create)

    # Link issues for merge_request
    if event.event_type == "merge_request" and event.keys:
        for space_key, num in event.keys:
            resolved = key_resolution.resolve_key(db, repo.workspace_id, space_key, num)
            if resolved:
                issue, _ = resolved
                ext_id = f"{event.repository}!{issue.number}"
                existing_link = (
                    db.query(VcsLink)
                    .filter(
                        VcsLink.issue_id == issue.id,
                        VcsLink.provider == "gitlab",
                        VcsLink.external_id == ext_id,
                    )
                    .first()
                )
                if not existing_link:
                    db.add(VcsLink(
                        workspace_id=repo.workspace_id,
                        issue_id=issue.id,
                        repository_id=repo.id,
                        provider="gitlab",
                        kind="pull_request",
                        external_id=ext_id,
                        url=payload.get("object_attributes", {}).get("url") or "",
                        title=payload.get("object_attributes", {}).get("title"),
                        state=event.status,
                        author_login=event.author_name,
                    ))
                    linked_keys.append(f"{space_key}-{num}")

    db.commit()
    return {"handled": True, "event": event.event_type, "linked": linked_keys}


def apply_bitbucket_delivery(
    db: Session,
    event_key: str | None,
    delivery_id: str,
    payload: dict[str, Any],
) -> dict[str, Any]:
    event: BitbucketEvent | None = parse_bitbucket_event(event_key, payload)
    if not event:
        return {"handled": False, "note": "Unrecognized or unhandled Bitbucket event"}

    existing = db.query(WebhookDelivery).filter(WebhookDelivery.id == delivery_id).first()
    if existing:
        return {"handled": False, "duplicate": True, "note": "Duplicate delivery"}

    delivery = WebhookDelivery(
        id=delivery_id,
        provider="bitbucket",
        event=event.event_type,
        status="processed",
    )
    db.add(delivery)

    repo = workspace_service.repository_by_name(db, "bitbucket", event.repository)
    if not repo:
        db.commit()
        return {"handled": False, "note": f"Repository '{event.repository}' not registered"}

    delivery.workspace_id = repo.workspace_id

    linked_keys: list[str] = []
    if event.event_type == "commit_status" and event.sha:
        dep_create = DeploymentCreate(
            environment="production",
            status=event.status if event.status in ("in_progress", "success", "failed", "rolled_back") else "in_progress",
            commit_sha=event.sha,
            ref_name=event.ref,
            repository_id=repo.id,
            issue_keys=[f"{k}-{num}" for k, num in event.keys],
        )
        deployment_service.create_deployment(db, repo.workspace_id, dep_create)

    if event.event_type == "pull_request" and event.keys:
        for space_key, num in event.keys:
            resolved = key_resolution.resolve_key(db, repo.workspace_id, space_key, num)
            if resolved:
                issue, _ = resolved
                pr = payload.get("pullrequest") or {}
                pr_id = pr.get("id") or issue.number
                ext_id = f"{event.repository}#{pr_id}"
                existing_link = (
                    db.query(VcsLink)
                    .filter(
                        VcsLink.issue_id == issue.id,
                        VcsLink.provider == "bitbucket",
                        VcsLink.external_id == ext_id,
                    )
                    .first()
                )
                if not existing_link:
                    db.add(VcsLink(
                        workspace_id=repo.workspace_id,
                        issue_id=issue.id,
                        repository_id=repo.id,
                        provider="bitbucket",
                        kind="pull_request",
                        external_id=ext_id,
                        url=pr.get("links", {}).get("html", {}).get("href") or "",
                        title=pr.get("title"),
                        state=event.status,
                        author_login=event.author_name,
                    ))
                    linked_keys.append(f"{space_key}-{num}")

    db.commit()
    return {"handled": True, "event": event.event_type, "linked": linked_keys}
