"""Typed dependencies between issues: blocks, relates, duplicates and clones.

docs/design/09-restructuring.md §10. The restructuring plan's structural rule 7 says to record
cross-space coordination explicitly and not to force cross-space parents. A parent must be in
its child's space (services/issue.parent_problem), so work that waits on another team's work
is a `blocks` link between two spaces of the workspace, not a parent in the wrong place.

Three rules hold in this module:

* **Every query carries `workspace_id`**, including the lookup of a link by its own id
  (services/workspace.py says why). The controller resolves both ends inside the caller's
  workspace, so a link never joins two tenants.
* **One row per dependency.** A directional kind is stored once, source to target, and read
  from either end ("PAY-3 blocks OPS-12" / "OPS-12 is blocked by PAY-3"). `relates` reads the
  same both ways, so a second row the other way round would be the same fact twice:
  `existing_link` treats it as the duplicate it is.
* **Both issues get the event**, in the same transaction as the row (models/issue.IssueEvent):
  a dependency changes what both issues mean to the people reading them. `to_value` is
  `<kind>:<the other issue's id>`, as models/issue.py documents. `from_value` says which end
  the issue is on, `outward` or `inward`, because the kind alone cannot say whether an issue
  blocks the other one or is blocked by it — and once the link is deleted, nothing else can.
"""
from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime

from sqlalchemy import and_, or_
from sqlalchemy.orm import Session

from models.hierarchy import Space, WorkflowStatus
from models.issue import Issue, IssueLink
from services import issue as issue_svc
from utils.issue_keys import format_issue_key

OUTWARD = "outward"
INWARD = "inward"

#: How each kind reads from its source and from its target. Every kind in
#: `models.issue.DEPENDENCY_KINDS` has an entry; tests/integration/test_dependencies.py
#: checks that, so a new kind cannot be stored and then fail to render.
LABELS: dict[str, tuple[str, str]] = {
    "blocks": ("blocks", "is blocked by"),
    "relates": ("relates to", "relates to"),
    "duplicates": ("duplicates", "is duplicated by"),
    "clones": ("clones", "is cloned by"),
}

#: The order an issue's dependencies are listed in: what stops the work first, then what it
#: stops, then context. Jira's issue view groups them the same way, and a list in creation
#: order would put the one blocker that matters wherever it happened to be added.
_LABEL_ORDER: tuple[str, ...] = (
    "blocks", "is blocked by", "relates to", "duplicates", "is duplicated by", "clones",
    "is cloned by",
)

#: Kinds that read the same from both ends: stored once, and refused the other way round.
SYMMETRIC_KINDS: frozenset[str] = frozenset({"relates"})


def label(kind: str, direction: str) -> str:
    # A kind this table does not know reads as itself rather than raising: every writer
    # validates the kind, so such a row was written by hand, and one bad row must not turn
    # every read of the issue's dependencies into a 500.
    outward, inward = LABELS.get(kind, (kind, kind))
    return outward if direction == OUTWARD else inward


def _label_rank(text: str) -> int:
    return _LABEL_ORDER.index(text) if text in _LABEL_ORDER else len(_LABEL_ORDER)


def _naive(value: datetime) -> datetime:
    # A row read back from the database is naive; one created in this session holds the
    # aware default it was given (models/issue._now). Comparing the two would raise.
    return value.replace(tzinfo=None)


def get_link(db: Session, workspace_id: str, link_id: str) -> IssueLink | None:
    return (db.query(IssueLink)
            .filter(IssueLink.workspace_id == workspace_id, IssueLink.id == link_id)
            .first())


def links_of(db: Session, workspace_id: str, issue_id: str) -> list[IssueLink]:
    """Every link with `issue_id` at either end, in one query. The two halves of the OR are
    `ix_issue_links_workspace_source` and `ix_issue_links_target`."""
    return (db.query(IssueLink)
            .filter(IssueLink.workspace_id == workspace_id,
                    or_(IssueLink.source_issue_id == issue_id,
                        IssueLink.target_issue_id == issue_id))
            .all())


def existing_link(db: Session, workspace_id: str, source_id: str, target_id: str,
                  kind: str) -> IssueLink | None:
    """The row that already records this dependency, if there is one. For a symmetric kind
    a row either way round is the same fact."""
    pair = and_(IssueLink.source_issue_id == source_id, IssueLink.target_issue_id == target_id)
    if kind in SYMMETRIC_KINDS:
        pair = or_(pair, and_(IssueLink.source_issue_id == target_id,
                              IssueLink.target_issue_id == source_id))
    return (db.query(IssueLink)
            .filter(IssueLink.workspace_id == workspace_id, IssueLink.kind == kind, pair)
            .first())


def lock_pair(db: Session, workspace_id: str, first_id: str, second_id: str) -> None:
    """Serialise link writes between the same two issues until commit.

    `uq_issue_links_pair_kind` stops two identical rows, but not `A relates B` and `B relates
    A` arriving at the same moment — each request would check, find nothing, and insert. Both
    issue rows are locked, in id order so that two requests for the same pair queue rather
    than deadlock. SQLite ignores FOR UPDATE; it has one writer anyway.
    """
    (db.query(Issue.id)
     .filter(Issue.workspace_id == workspace_id, Issue.id.in_([first_id, second_id]))
     .order_by(Issue.id)
     .with_for_update()
     .all())


def _event_value(kind: str, other_id: str) -> str:
    return f"{kind}:{other_id}"


def create_link(db: Session, source: Issue, target: Issue, kind: str, actor_id: str, *,
                actor_kind: str = "user") -> IssueLink:
    """Insert the row and the event on both issues, and flush; the caller commits.

    Callers outside this feature — a transfer detaching an issue from a parent it leaves
    behind keeps the context as a `relates` link (09-restructuring.md §7.2) — check
    `existing_link` first and pass their own `actor_kind` (`migration` for a plan's runner).
    """
    link = IssueLink(workspace_id=source.workspace_id, source_issue_id=source.id,
                     target_issue_id=target.id, kind=kind, created_by=actor_id)
    db.add(link)
    db.flush()
    issue_svc.record_event(db, source, "dependency_added", actor_id, actor_kind=actor_kind,
                           from_value=OUTWARD, to_value=_event_value(kind, str(target.id)))
    issue_svc.record_event(db, target, "dependency_added", actor_id, actor_kind=actor_kind,
                           from_value=INWARD, to_value=_event_value(kind, str(source.id)))
    return link


def link_ends(db: Session, link: IssueLink) -> tuple[Issue | None, Issue | None]:
    """The source and the target, deleted ones included: removing a link to an issue that
    has since been deleted must still work, and still be recorded on the live end."""
    rows = {issue.id: issue for issue in db.query(Issue).filter(
        Issue.workspace_id == link.workspace_id,
        Issue.id.in_([link.source_issue_id, link.target_issue_id]))}
    return rows.get(link.source_issue_id), rows.get(link.target_issue_id)


def remove_link(db: Session, link: IssueLink, source: Issue | None, target: Issue | None,
                actor_id: str, *, actor_kind: str = "user") -> None:
    """Delete the row — `issue_links` has no soft delete; the two events are the history."""
    for issue, direction, other_id in ((source, OUTWARD, link.target_issue_id),
                                       (target, INWARD, link.source_issue_id)):
        if issue is not None:
            issue_svc.record_event(db, issue, "dependency_removed", actor_id,
                                   actor_kind=actor_kind, from_value=direction,
                                   to_value=_event_value(str(link.kind), str(other_id)))
    db.delete(link)
    db.flush()


def _other_ends(db: Session, workspace_id: str,
                issue_ids: Iterable[str]) -> dict[str, tuple[Issue, str, str, str]]:
    """Live issues with their space key, stage name and stage category — one query.

    A removed stage still joins: its row is kept for exactly this (models/hierarchy.py), and
    an issue cannot be in one after a publish anyway.
    """
    ids = set(issue_ids)
    if not ids:
        return {}
    rows = (db.query(Issue, Space.key, WorkflowStatus.name, WorkflowStatus.category)
            .join(Space, and_(Space.id == Issue.space_id, Space.workspace_id == workspace_id))
            .join(WorkflowStatus, and_(WorkflowStatus.id == Issue.status_id,
                                       WorkflowStatus.workspace_id == workspace_id))
            .filter(Issue.workspace_id == workspace_id, Issue.id.in_(ids), Issue.deleted == 0)
            .all())
    return {issue.id: (issue, key, name, category) for issue, key, name, category in rows}


def payloads(db: Session, workspace_id: str, issue_id: str,
             links: list[IssueLink]) -> list[dict]:
    """`DependencySummary` payloads for `links`, read from `issue_id`'s side, in
    `_LABEL_ORDER` and oldest first within a label.

    Two queries whatever the number of links: the other ends (with their space key and
    stage), and the people who linked them. A link whose other issue has been deleted is
    left out — that issue answers 404 everywhere, so a row pointing at it is a link nobody
    can follow. The row itself stays, like the deleted issue's own history.
    """
    other_of = {link.id: str(link.target_issue_id if link.source_issue_id == issue_id
                             else link.source_issue_id)
                for link in links}
    others = _other_ends(db, workspace_id, other_of.values())
    people = issue_svc.users_by_id(db, (link.created_by for link in links))
    result = []
    for link in links:
        found = others.get(other_of[link.id])
        if found is None:
            continue
        other, space_key, status_name, category = found
        direction = OUTWARD if link.source_issue_id == issue_id else INWARD
        result.append({
            "id": link.id,
            "kind": link.kind,
            "direction": direction,
            "label": label(str(link.kind), direction),
            "issue": {
                "id": other.id,
                "key": format_issue_key(str(space_key), int(other.number)),
                "title": other.title,
                "type": other.type,
                "status_id": other.status_id,
                "status_name": status_name,
                "category": category,
                "space_id": other.space_id,
                "list_id": other.list_id,
            },
            "created_at": link.created_at,
            "created_by": (issue_svc.user_ref(people.get(link.created_by))
                           or {"id": link.created_by, "display_name": ""}),
        })
    result.sort(key=lambda item: (_label_rank(item["label"]), _naive(item["created_at"]),
                                  item["id"]))
    return result
