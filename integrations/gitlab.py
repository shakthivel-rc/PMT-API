"""GitLab webhooks: token verification and event parsing."""
from __future__ import annotations

import hmac
from dataclasses import dataclass
from typing import Any

from utils.issue_keys import extract_issue_keys


def verify_gitlab_token(secret: str, header: str | None) -> bool:
    """Verify GitLab webhook token against X-Gitlab-Token header."""
    if not secret or not header:
        return False
    return hmac.compare_digest(secret, header)


@dataclass(frozen=True)
class GitLabEvent:
    event_type: str
    repository: str
    ref: str | None
    sha: str | None
    status: str | None
    environment: str | None
    author_name: str | None
    keys: list[tuple[str, int]]


def parse_gitlab_event(event_header: str | None, payload: dict[str, Any]) -> GitLabEvent | None:
    """Parse GitLab webhook payload (Pipeline, Deployment, or Merge Request)."""
    object_kind = payload.get("object_kind") or ""
    project = payload.get("project") or {}
    repo_name = project.get("path_with_namespace") or project.get("name") or ""

    keys: list[tuple[str, int]] = []

    # 1. Pipeline Hook
    if object_kind == "pipeline" or event_header == "Pipeline Hook":
        obj_attrs = payload.get("object_attributes") or {}
        ref = obj_attrs.get("ref") or payload.get("ref") or ""
        sha = obj_attrs.get("sha") or payload.get("sha") or ""
        status = obj_attrs.get("status") or ""
        user = payload.get("user") or {}
        author = user.get("name") or user.get("username") or ""

        # Extract keys from commit message & ref
        commit = payload.get("commit") or {}
        msg = commit.get("message") or ""
        for k in extract_issue_keys(msg):
            if k not in keys:
                keys.append(k)
        for k in extract_issue_keys(ref, case_insensitive=True):
            if k not in keys:
                keys.append(k)

        return GitLabEvent(
            event_type="pipeline",
            repository=repo_name,
            ref=ref or None,
            sha=sha or None,
            status=status or None,
            environment=None,
            author_name=author or None,
            keys=keys,
        )

    # 2. Deployment Hook
    if object_kind == "deployment" or event_header == "Deployment Hook":
        ref = payload.get("ref") or ""
        sha = payload.get("sha") or ""
        status = payload.get("status") or ""
        environment = payload.get("environment") or ""
        user = payload.get("user") or {}
        author = user.get("name") or user.get("username") or ""

        for k in extract_issue_keys(ref, case_insensitive=True):
            if k not in keys:
                keys.append(k)

        return GitLabEvent(
            event_type="deployment",
            repository=repo_name,
            ref=ref or None,
            sha=sha or None,
            status=status or None,
            environment=environment or None,
            author_name=author or None,
            keys=keys,
        )

    # 3. Merge Request Hook
    if object_kind == "merge_request" or event_header == "Merge Request Hook":
        obj_attrs = payload.get("object_attributes") or {}
        title = obj_attrs.get("title") or ""
        desc = obj_attrs.get("description") or ""
        source_branch = obj_attrs.get("source_branch") or ""
        sha = obj_attrs.get("last_commit", {}).get("id") or ""
        state = obj_attrs.get("state") or ""

        for k in extract_issue_keys(title, desc):
            if k not in keys:
                keys.append(k)
        for k in extract_issue_keys(source_branch, case_insensitive=True):
            if k not in keys:
                keys.append(k)

        user = payload.get("user") or {}
        author = user.get("name") or user.get("username") or ""

        return GitLabEvent(
            event_type="merge_request",
            repository=repo_name,
            ref=source_branch or None,
            sha=sha or None,
            status=state or None,
            environment=None,
            author_name=author or None,
            keys=keys,
        )

    return None
