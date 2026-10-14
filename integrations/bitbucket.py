"""Bitbucket webhooks: signature verification and event parsing."""
from __future__ import annotations

import hashlib
import hmac
from dataclasses import dataclass
from typing import Any

from utils.issue_keys import extract_issue_keys


def verify_bitbucket_signature(secret: str, body: bytes, header: str | None) -> bool:
    """Verify Bitbucket webhook signature against X-Hub-Signature header."""
    if not secret or not header or not header.startswith("sha256="):
        return False
    expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, header.removeprefix("sha256="))


@dataclass(frozen=True)
class BitbucketEvent:
    event_type: str
    repository: str
    ref: str | None
    sha: str | None
    status: str | None
    environment: str | None
    author_name: str | None
    keys: list[tuple[str, int]]


def parse_bitbucket_event(event_key: str | None, payload: dict[str, Any]) -> BitbucketEvent | None:
    """Parse Bitbucket webhook payload (Pull Request or Commit Status)."""
    repo = payload.get("repository") or {}
    repo_name = repo.get("full_name") or repo.get("name") or ""
    keys: list[tuple[str, int]] = []

    # 1. Pull Request Events
    if event_key and event_key.startswith("pullrequest:"):
        pr = payload.get("pullrequest") or {}
        title = pr.get("title") or ""
        desc = pr.get("description") or ""
        state = pr.get("state") or ""
        source = pr.get("source") or {}
        branch = source.get("branch", {}).get("name") or ""
        commit = source.get("commit", {}).get("hash") or ""
        author = pr.get("author", {}).get("display_name") or ""

        for k in extract_issue_keys(title, desc):
            if k not in keys:
                keys.append(k)
        for k in extract_issue_keys(branch, case_insensitive=True):
            if k not in keys:
                keys.append(k)

        return BitbucketEvent(
            event_type="pull_request",
            repository=repo_name,
            ref=branch or None,
            sha=commit or None,
            status=state.lower() if state else None,
            environment=None,
            author_name=author or None,
            keys=keys,
        )

    # 2. Commit Status Events
    if event_key and event_key.startswith("repo:commit_status_"):
        commit_status = payload.get("commit_status") or {}
        state = commit_status.get("state") or ""  # SUCCESSFUL, FAILED, INPROGRESS, STOPPED
        commit_hash = commit_status.get("commit", {}).get("hash") or ""
        name = commit_status.get("name") or ""
        refname = commit_status.get("refname") or ""

        status_mapping = {
            "SUCCESSFUL": "success",
            "FAILED": "failed",
            "INPROGRESS": "in_progress",
            "STOPPED": "rolled_back",
        }

        for k in extract_issue_keys(name, refname, case_insensitive=True):
            if k not in keys:
                keys.append(k)

        return BitbucketEvent(
            event_type="commit_status",
            repository=repo_name,
            ref=refname or None,
            sha=commit_hash or None,
            status=status_mapping.get(state, state.lower()),
            environment=None,
            author_name=None,
            keys=keys,
        )

    return None
