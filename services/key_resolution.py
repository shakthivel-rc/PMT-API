"""`PAY-7` → the issue it means, today, wherever that issue now lives.

docs/design/09-restructuring.md §7.3. A key is quoted in branch names, commit messages, pull
request titles, chat and bookmarks, and none of those can be updated when an issue moves to
another space or its space changes key. So there is **one resolver**, and everything that
turns a key into an issue — the GitHub webhook, the deep-link route, the `q` filter — goes
through it, in this order:

1. **The live key.** A space with that key and an issue with that number in it.
2. **A space-key alias.** The space used to be called that (`space_key_aliases`); the number
   still means the same issue in it.
3. **An issue-key alias.** The issue had that key before it moved (`issue_key_aliases`).

The order cannot misfire: issue numbers are never reused (`spaces.issue_counter` only
increments) and a retired space key stays reserved, so at most one of the three answers.

Every lookup is inside one workspace. A key is only unique within a tenant, and resolving
one across tenants would be the cross-tenant read the whole schema is arranged to prevent.
"""
from __future__ import annotations

from sqlalchemy.orm import Session

from models.hierarchy import Space, SpaceKeyAlias
from models.issue import Issue, IssueKeyAlias
from utils.issue_keys import format_issue_key

#: How a key was resolved: `current`, `space_alias` or `issue_alias`.
RESOLVED_CURRENT = "current"
RESOLVED_SPACE_ALIAS = "space_alias"
RESOLVED_ISSUE_ALIAS = "issue_alias"


def _live(db: Session, workspace_id: str, space_id: str, number: int) -> Issue | None:
    return (db.query(Issue)
            .filter(Issue.workspace_id == workspace_id, Issue.space_id == space_id,
                    Issue.number == number, Issue.deleted == 0)
            .first())


def resolve_key(db: Session, workspace_id: str, space_key: str,
                number: int) -> tuple[Issue, str] | None:
    """The live issue `space_key`-`number` names in this workspace, and how it was found."""
    key = space_key.upper()
    space_id = (db.query(Space.id)
                .filter(Space.workspace_id == workspace_id, Space.key == key,
                        Space.deleted == 0)
                .scalar())
    if space_id is not None:
        issue = _live(db, workspace_id, space_id, number)
        if issue is not None:
            return issue, RESOLVED_CURRENT
    aliased_space = (db.query(SpaceKeyAlias.space_id)
                     .filter(SpaceKeyAlias.workspace_id == workspace_id,
                             SpaceKeyAlias.key == key)
                     .scalar())
    if aliased_space is not None:
        issue = _live(db, workspace_id, aliased_space, number)
        if issue is not None:
            return issue, RESOLVED_SPACE_ALIAS
    alias = (db.query(IssueKeyAlias.issue_id)
             .filter(IssueKeyAlias.workspace_id == workspace_id,
                     IssueKeyAlias.space_key == key, IssueKeyAlias.number == number)
             .scalar())
    if alias is not None:
        issue = (db.query(Issue)
                 .filter(Issue.workspace_id == workspace_id, Issue.id == alias,
                         Issue.deleted == 0)
                 .first())
        if issue is not None:
            return issue, RESOLVED_ISSUE_ALIAS
    return None


def current_key(db: Session, issue: Issue) -> str:
    key = db.query(Space.key).filter(Space.id == issue.space_id).scalar() or "?"
    return format_issue_key(str(key), int(issue.number))


def previous_keys(db: Session, workspace_id: str, issue_id: str) -> list[str]:
    """Every key the issue was known by before its current one, oldest first."""
    rows = (db.query(IssueKeyAlias.space_key, IssueKeyAlias.number)
            .filter(IssueKeyAlias.workspace_id == workspace_id,
                    IssueKeyAlias.issue_id == issue_id)
            .order_by(IssueKeyAlias.created_at, IssueKeyAlias.id)
            .all())
    return [format_issue_key(space_key, number) for space_key, number in rows]


def space_key_reserved(db: Session, workspace_id: str, key: str,
                       except_space_id: str | None = None) -> bool:
    """Whether `key` is taken by a live space, or reserved by another space's old key.

    A space may take back one of its own old keys (`except_space_id`), which is how a
    mistaken rename is undone.
    """
    key = key.upper()
    live = db.query(Space.id).filter(Space.workspace_id == workspace_id, Space.key == key)
    if except_space_id is not None:
        live = live.filter(Space.id != except_space_id)
    if live.first() is not None:
        return True
    aliased = db.query(SpaceKeyAlias.space_id).filter(SpaceKeyAlias.workspace_id == workspace_id,
                                                      SpaceKeyAlias.key == key)
    if except_space_id is not None:
        aliased = aliased.filter(SpaceKeyAlias.space_id != except_space_id)
    return aliased.first() is not None
