"""GitHub webhooks: proving a delivery came from GitHub, and reading what it says.

Pure functions — no database, no network — so the part of the webhook that decides whether
to trust a request is small enough to read in one sitting and is covered by unit tests that
need nothing running (`tests/unit/test_github_webhook_parsing.py`).

**Authentication is the signature, and only the signature.** The webhook route is outside
JWT authentication (GitHub cannot hold a session), so `verify_signature` is the entire
answer to "who sent this". It is HMAC-SHA256 over the **raw** body with the shared secret,
compared in constant time. Two things it deliberately does not do:

* accept the legacy `X-Hub-Signature` (SHA-1) header — a downgrade an attacker would choose;
* verify a re-serialised body. Parsing the JSON and dumping it again changes whitespace and
  key order, so the digest must be taken over the bytes exactly as they arrived.
"""
from __future__ import annotations

import hashlib
import hmac
from dataclasses import dataclass
from typing import Any

from utils.issue_keys import extract_issue_keys

#: The pull_request actions that can change what a link should say. Everything else
#: (labeled, review_requested, …) is acknowledged and ignored.
HANDLED_PR_ACTIONS = frozenset({"opened", "edited", "reopened", "closed", "synchronize",
                                "ready_for_review", "converted_to_draft"})


def verify_signature(secret: str, body: bytes, header: str | None) -> bool:
    """Whether `header` (`X-Hub-Signature-256`) is the HMAC of `body` under `secret`.

    False — never an exception — for anything malformed, and always False when no secret is
    configured: an empty key is a key anyone can compute with.
    """
    if not secret or not header or not header.startswith("sha256="):
        return False
    expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, header.removeprefix("sha256="))


@dataclass(frozen=True)
class PullRequestEvent:
    repository: str
    number: int
    external_id: str
    url: str
    title: str
    state: str
    author_login: str | None
    keys: list[tuple[str, int]]


def _text(value: Any) -> str:
    return value if isinstance(value, str) else ""


def parse_pull_request(payload: dict[str, Any]) -> PullRequestEvent | None:
    """The fields a link needs, or None if this delivery is not one we act on.

    Tolerant of missing fields — a payload shape GitHub changes, or one a test builds by
    hand — because a KeyError here would be a 500 to GitHub, which then retries the same
    delivery until it gives up.
    """
    if payload.get("action") not in HANDLED_PR_ACTIONS:
        return None
    pr = payload.get("pull_request")
    repo = payload.get("repository")
    if not isinstance(pr, dict) or not isinstance(repo, dict):
        return None
    full_name = _text(repo.get("full_name"))
    number = pr.get("number")
    if not full_name or not isinstance(number, int):
        return None

    if pr.get("merged") is True:
        state = "merged"
    elif pr.get("state") == "closed":
        state = "closed"
    else:
        state = "open"

    head = pr.get("head")
    branch = _text(head.get("ref")) if isinstance(head, dict) else ""
    title = _text(pr.get("title"))
    user = pr.get("user")
    login = _text(user.get("login")) if isinstance(user, dict) else ""

    # Title and body are matched case-sensitively; the branch name is not, because branch
    # tooling lower-cases keys (`feature/pay-142-refunds`). See utils/issue_keys.py.
    keys = extract_issue_keys(title, _text(pr.get("body")))
    for key in extract_issue_keys(branch, case_insensitive=True):
        if key not in keys:
            keys.append(key)

    return PullRequestEvent(
        repository=full_name,
        number=number,
        external_id=f"{full_name}#{number}",
        url=_text(pr.get("html_url")),
        title=title[:500],
        state=state,
        author_login=login[:100] or None,
        keys=keys,
    )
