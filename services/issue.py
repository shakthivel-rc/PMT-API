"""Issue operations: numbering, ranking, status changes, the rules, and the card projection.

Two rules hold everywhere in this module:

* **Every query carries `workspace_id`.** See `services/workspace.py`.
* **Every state change writes its `issue_events` row in the same transaction.** The event log
  is what burndown and cumulative-flow reports are computed from (models/issue.py), so an
  update that skips it is an update the reports never see. `set_status` is the one place a
  status changes, for people and for webhooks alike.

The issue rules of docs/design/09-restructuring.md live here as functions that answer "why
not", so the issue routes, the transfer tool and the migration runner ask the same question
and get the same sentence: the standard hierarchy (§2), the epic's owner and completion
criteria (§2.3), and the WIP limit a move would break (§4.7).
"""
import re
from collections.abc import Iterable
from datetime import UTC, datetime

from sqlalchemy import func, or_, update
from sqlalchemy.orm import Query, Session

from models.hierarchy import Space, WorkflowStatus
from models.issue import Issue, IssueComment, IssueEvent
from models.time_tracking import TimeEntry
from models.user import User
from models.vcs import VcsLink
from schemas.issues import IssueFilters
from services import key_resolution, transitions
from utils import hierarchy_rules
from utils.issue_keys import SPACE_KEY_PATTERN, format_issue_key
from utils.rank import needs_rebalance, rank_after, rank_between, spread
from utils.workflow_rules import WIP_TYPES

#: When several pull requests link to one issue, the card shows the most advanced one.
_PR_PRECEDENCE = {"merged": 3, "open": 2, "closed": 1}


def display_name(user: User | None) -> str:
    if user is None:
        return ""
    return f"{user.first_name} {user.last_name}".strip() or str(user.username)


def user_ref(user: User | None) -> dict | None:
    return None if user is None else {"id": user.id, "display_name": display_name(user)}


def get_issue(db: Session, workspace_id: str, issue_id: str) -> Issue | None:
    return (
        db.query(Issue)
        .filter(Issue.workspace_id == workspace_id, Issue.id == issue_id, Issue.deleted == 0)
        .first()
    )


def get_status(db: Session, workspace_id: str, status_id: str) -> WorkflowStatus | None:
    """A live stage. A removed one is kept for history and is never a destination."""
    return transitions.get_live_status(db, workspace_id, status_id)


# ---- writes racing a workflow publish (09-restructuring.md §4.4) ---------------------------
#
# A publish holds its space's row FOR UPDATE while it moves the issues out of the stages it
# removes and soft-deletes them (services/workflow.lock_space, issues_to_move). A move or a
# create that read its destination stage as live BEFORE that publish committed could
# otherwise write `status_id` into a stage that no longer exists — an issue no board draws.
# So both take the space's row first and only then read the stage, with a locking read:
# under MySQL's REPEATABLE READ a plain SELECT returns the snapshot of the transaction's first
# read, from before the publish it may just have waited for, while a locking read sees the
# latest committed row. Either the write commits before the publish counts the stage's
# issues (and the publish moves it with them), or it waits for the publish and finds the
# stage removed. Lock order is always space, then issue — the publish's and the webhook's
# order too — so none of them can deadlock another. SQLite ignores both clauses; it has one
# writer, which is why the tests simulate the interleaving instead.


def lock_space_shared(db: Session, workspace_id: str, space_id: str) -> None:
    """Hold the space's row in share mode until commit. Moves in one space share it with
    each other; only a publish, the merge action and the space's own writes exclude them."""
    (db.query(Space.id)
     .filter(Space.workspace_id == workspace_id, Space.id == space_id)
     .with_for_update(read=True)
     .first())


def lock_issue(db: Session, workspace_id: str, issue_id: str) -> Issue | None:
    """The issue as last committed, locked for this write. `populate_existing` because the
    session already holds the copy read before the lock, whose stage and version a publish
    that committed meanwhile may have changed."""
    return (db.query(Issue)
            .filter(Issue.workspace_id == workspace_id, Issue.id == issue_id,
                    Issue.deleted == 0)
            .with_for_update()
            .populate_existing()
            .first())


def locked_live_status(db: Session, workspace_id: str, status_id: str) -> WorkflowStatus | None:
    """`get_status` as a locking read: the stage as last committed, or None once a publish
    has removed it. Call it only with the space's row already held."""
    return (db.query(WorkflowStatus)
            .filter(WorkflowStatus.workspace_id == workspace_id,
                    WorkflowStatus.id == status_id, WorkflowStatus.deleted == 0)
            .with_for_update(read=True)
            .populate_existing()
            .first())


def any_status(db: Session, workspace_id: str, status_id: str) -> WorkflowStatus | None:
    """A stage, removed ones included: the stage an issue is LEAVING may be one, and a
    restricted workflow still decides where it may go from there."""
    return (db.query(WorkflowStatus)
            .filter(WorkflowStatus.workspace_id == workspace_id, WorkflowStatus.id == status_id)
            .first())


def default_status(db: Session, workspace_id: str, space_id: str) -> WorkflowStatus | None:
    """Where a new issue lands when none is named: the space's initial stage
    (services/transitions.initial_status says what that falls back to)."""
    space = (db.query(Space)
             .filter(Space.workspace_id == workspace_id, Space.id == space_id)
             .first())
    return transitions.initial_status(db, workspace_id, space) if space is not None else None


def first_done_status(db: Session, workspace_id: str, space_id: str) -> WorkflowStatus | None:
    return (
        db.query(WorkflowStatus)
        .filter(WorkflowStatus.workspace_id == workspace_id,
                WorkflowStatus.space_id == space_id, WorkflowStatus.category == "done",
                WorkflowStatus.deleted == 0)
        .order_by(WorkflowStatus.position)
        .first()
    )


def parent_problem(child_type: str, parent: Issue | None, space_id: str) -> str | None:
    """Why `parent` cannot hold an issue of `child_type`, or None if it can.

    **The standard convention, always** (09-restructuring.md §2.1): this is asked only of a
    relationship being created or changed, and those follow the standard. A legacy one —
    a task under a story, a bug under a subtask — is never put to it unless someone changes
    it (§2.2), which is why nothing here consults the legacy table.
    """
    if parent is not None and parent.space_id != space_id:
        return "The parent must be in the same space."
    return hierarchy_rules.standard_parent_problem(child_type,
                                                   str(parent.type) if parent else None)


def _a(word: str) -> str:
    return f"an {word}" if word[:1] in "aeiou" else f"a {word}"


def children_type_problem(db: Session, workspace_id: str, issue: Issue,
                          new_type: str) -> str | None:
    """Why `issue` cannot become `new_type` because of the children it has, or None.

    Re-typing an issue changes every child's relationship to it, so each live child is
    checked against the standard as if it were being put there now (§2.2). The answer names
    the first child that cannot stay, the rule it breaks, and what to do about it.
    """
    children = (db.query(Issue.type, Issue.number)
                .filter(Issue.workspace_id == workspace_id, Issue.parent_id == issue.id,
                        Issue.deleted == 0)
                .order_by(Issue.number)
                .all())
    for child_type, number in children:
        problem = hierarchy_rules.standard_parent_problem(str(child_type), new_type)
        if problem is None:
            continue
        space_key = (db.query(Space.key)
                     .filter(Space.workspace_id == workspace_id, Space.id == issue.space_id)
                     .scalar()) or "?"
        return (f"{format_issue_key(str(space_key), int(number))} cannot sit under "
                f"{_a(new_type)}. {problem} Move or re-type its children first.")
    return None


def is_blank(text: str | None) -> bool:
    return not (text or "").strip()


def epic_problem(assignee_id: str | None, completion_criteria: str | None) -> str | None:
    """Why an epic with this owner and these criteria may not be created — or an issue
    re-typed into one — while the governance rule is on (09-restructuring.md §2.3), or None.

    The plan's creation rule for an epic is "require an owner and completion criteria": an
    epic is an outcome, and an outcome nobody owns, or whose finish line nobody wrote down,
    is the "permanent departmental bucket" the plan warns against.
    """
    missing_owner = assignee_id is None
    missing_criteria = is_blank(completion_criteria)
    if missing_owner and missing_criteria:
        return ("An epic needs an owner and completion criteria: assign it to the person "
                "accountable for the outcome, and say what must be true for it to be done.")
    if missing_owner:
        return "An epic needs an owner: assign it to the person accountable for the outcome."
    if missing_criteria:
        return ("An epic needs completion criteria: say what must be true for it to be "
                "done.")
    return None


def is_ancestor(db: Session, workspace_id: str, candidate_id: str, issue_id: str) -> bool:
    """Whether `issue_id` is `candidate_id` or one of its ancestors — that is, whether
    making `candidate_id` the parent of `issue_id` would create a cycle.

    Walks up from the candidate. The type rules bound the depth at five, so the loop is
    bounded too; the explicit limit is a guard against rows written before those rules.
    """
    current: str | None = candidate_id
    for _ in range(16):
        if current is None:
            return False
        if current == issue_id:
            return True
        current = (
            db.query(Issue.parent_id)
            .filter(Issue.workspace_id == workspace_id, Issue.id == current)
            .scalar()
        )
    return True


def allocate_number(db: Session, space_id: str) -> int:
    """The next issue number in a space.

    One UPDATE, then a read in the same transaction. On MySQL and PostgreSQL the UPDATE takes
    the row lock and holds it to commit, so two concurrent creates in one space are serialised
    here and cannot read the same counter; `uq_issues_space_number` is the backstop.
    """
    db.execute(
        update(Space).where(Space.id == space_id).values(issue_counter=Space.issue_counter + 1)
    )
    return int(db.query(Space.issue_counter).filter(Space.id == space_id).scalar())


def _column(db: Session, workspace_id: str, space_id: str, status_id: str):
    return db.query(Issue).filter(
        Issue.workspace_id == workspace_id,
        Issue.space_id == space_id,
        Issue.status_id == status_id,
        Issue.deleted == 0,
    )


def bottom_rank(db: Session, workspace_id: str, space_id: str, status_id: str,
                exclude_id: str | None = None) -> str:
    query = db.query(func.max(Issue.rank)).filter(
        Issue.workspace_id == workspace_id,
        Issue.space_id == space_id,
        Issue.status_id == status_id,
        Issue.deleted == 0,
    )
    if exclude_id is not None:
        query = query.filter(Issue.id != exclude_id)
    return rank_after(query.scalar())


def bottom_backlog_rank(db: Session, workspace_id: str, space_id: str,
                        exclude_id: str | None = None) -> str:
    """A backlog rank below every live issue of the space (09-restructuring.md §5.2)."""
    query = db.query(func.max(Issue.backlog_rank)).filter(
        Issue.workspace_id == workspace_id,
        Issue.space_id == space_id,
        Issue.deleted == 0,
    )
    if exclude_id is not None:
        query = query.filter(Issue.id != exclude_id)
    return rank_after(query.scalar())


def rank_for_drop(db: Session, issue: Issue, status_id: str, after: Issue | None,
                  before: Issue | None) -> str:
    """The rank for `issue` dropped into `status_id` below `after` / above `before`.

    **The server finds the other neighbour itself.** A client's picture of a column can be
    stale — someone else moved a card a second ago — so trusting both of its neighbours can
    produce `after >= before`, which has no key between. Instead the card it names *above*
    is taken as the anchor and the real next card is read from the database; only when the
    client names no card above is its card *below* used. The drop lands where the person
    aimed it relative to the card they aimed at, whatever else moved.
    """
    column = _column(db, issue.workspace_id, issue.space_id, status_id).filter(
        Issue.id != issue.id
    )
    if after is not None:
        following = (
            column.filter(Issue.rank > after.rank).order_by(Issue.rank).first()
        )
        return rank_between(after.rank, following.rank if following else None)
    if before is not None:
        preceding = (
            column.filter(Issue.rank < before.rank).order_by(Issue.rank.desc()).first()
        )
        return rank_between(preceding.rank if preceding else None, before.rank)
    return bottom_rank(db, issue.workspace_id, issue.space_id, status_id, exclude_id=issue.id)


def rebalance_column(db: Session, workspace_id: str, space_id: str, status_id: str) -> int:
    """Re-issue short, evenly spaced ranks for a whole column. Returns the rows rewritten."""
    cards = _column(db, workspace_id, space_id, status_id).order_by(Issue.rank, Issue.id).all()
    for card, key in zip(cards, spread(len(cards)), strict=True):
        card.rank = key
    db.flush()
    return len(cards)


def _space_issues(db: Session, workspace_id: str, space_id: str):
    return db.query(Issue).filter(
        Issue.workspace_id == workspace_id,
        Issue.space_id == space_id,
        Issue.deleted == 0,
    )


def backlog_rank_for_drop(db: Session, issue: Issue, after: Issue | None,
                          before: Issue | None) -> str:
    """The backlog rank for `issue` dropped below `after` / above `before`
    (09-restructuring.md §5.2).

    `rank_for_drop`'s rule over the space's one backlog order instead of a column: the issue
    named above is the anchor and the issue really below it is read here, else the one named
    below and the issue really above it, else the bottom. The order is over every live issue
    of the space — sprint members, closed work and epics included — because they all share
    the key; a gap between two visible cards may hold hidden ones, and landing anywhere in
    that gap is still "directly under the card I aimed at" for everything the person sees.
    """
    others = _space_issues(db, issue.workspace_id, issue.space_id).filter(Issue.id != issue.id)
    if after is not None:
        following = (others.filter(Issue.backlog_rank > after.backlog_rank)
                     .order_by(Issue.backlog_rank).first())
        return rank_between(after.backlog_rank,
                            following.backlog_rank if following else None)
    if before is not None:
        preceding = (others.filter(Issue.backlog_rank < before.backlog_rank)
                     .order_by(Issue.backlog_rank.desc()).first())
        return rank_between(preceding.backlog_rank if preceding else None,
                            before.backlog_rank)
    return bottom_backlog_rank(db, issue.workspace_id, issue.space_id, exclude_id=issue.id)


def rebalance_backlog(db: Session, workspace_id: str, space_id: str) -> int:
    """Re-issue short, evenly spaced backlog ranks for every live issue of the space, in their
    current order (ties by id, so the result is deterministic). Returns the rows rewritten.

    O(space) writes, where a column rebalance is O(column): the backlog is one order over the
    whole space, so there is no smaller unit to re-spread. It is rare for the same reason the
    column's is — keys grow only when drops keep landing in one gap (utils/rank.py).
    """
    issues = (_space_issues(db, workspace_id, space_id)
              .order_by(Issue.backlog_rank, Issue.id).all())
    for row, key in zip(issues, spread(len(issues)), strict=True):
        row.backlog_rank = key
    db.flush()
    return len(issues)


def set_backlog_rank(db: Session, issue: Issue, rank: str) -> None:
    """Put `issue` at `rank` in its space's backlog, re-spreading the space when the key has
    grown too long — `set_status`'s rule for a column, over the backlog."""
    issue.backlog_rank = rank
    db.flush()
    if needs_rebalance(rank):
        rebalance_backlog(db, str(issue.workspace_id), str(issue.space_id))


def record_event(db: Session, issue: Issue, event: str, actor_id: str | None,
                 actor_kind: str = "user", from_value: str | None = None,
                 to_value: str | None = None) -> None:
    db.add(IssueEvent(
        workspace_id=issue.workspace_id,
        issue_id=issue.id,
        actor_id=actor_id,
        actor_kind=actor_kind,
        event=event,
        from_value=from_value[:255] if from_value else from_value,
        to_value=to_value[:255] if to_value else to_value,
    ))


def set_status(db: Session, issue: Issue, status: WorkflowStatus, rank: str,
               actor_id: str | None, actor_kind: str = "user") -> None:
    """Move `issue` to `status` at `rank`, recording the transition if the column changed."""
    previous_status_id = issue.status_id
    issue.rank = rank
    if previous_status_id != status.id:
        issue.status_id = status.id
        issue.status_entered_at = datetime.now(UTC)
        issue.version = (issue.version or 0) + 1
        record_event(db, issue, "status_changed", actor_id, actor_kind,
                     from_value=previous_status_id, to_value=status.id)
    db.flush()
    if needs_rebalance(rank):
        rebalance_column(db, issue.workspace_id, issue.space_id, status.id)


# ---- WIP (09-restructuring.md §4.7) -------------------------------------------------------


def _wip_issues(db: Session, workspace_id: str, space_id: str):
    """Live story, task and bug issues of the whole space: what a WIP limit counts. Epics are
    outcomes and subtasks are parts of an item already counted (utils/workflow_rules)."""
    return db.query(Issue).filter(
        Issue.workspace_id == workspace_id,
        Issue.space_id == space_id,
        Issue.deleted == 0,
        Issue.type.in_(sorted(WIP_TYPES)),
    )


def wip_counts(db: Session, workspace_id: str, space_id: str) -> dict[str, int]:
    """WIP issues per stage across the space, in one grouped query, for the board's columns.

    Never narrowed by a board's scope or filters: the limit is the team's capacity, so the
    count a person filtering to their own work sees must be the count the team is held to.
    """
    rows = (_wip_issues(db, workspace_id, space_id)
            .with_entities(Issue.status_id, func.count(Issue.id))
            .group_by(Issue.status_id)
            .all())
    return {status_id: int(count) for status_id, count in rows}


def column_wip(status: WorkflowStatus, counts: dict[str, int]) -> dict:
    """`ColumnWip` for one stage. A limit on a stage that does not count toward WIP is never
    enforced by a move, so it is not shown as one either."""
    count = counts.get(str(status.id), 0)
    limit = status.wip_limit if status.counts_toward_wip else None
    return {"count": count, "limit": limit,
            "over_limit": limit is not None and count > int(limit)}


def stage_wip_breach(db: Session, workspace_id: str, space_id: str, status: WorkflowStatus,
                     issue_type: str, exclude_id: str | None = None) -> tuple[int, int] | None:
    """`(count, limit)` when one more issue of `issue_type` in `status` takes the stage over
    its WIP limit, `count` including that issue; None when it does not or no limit applies.

    A limit applies only when the stage counts toward WIP and has one, and the issue is a
    story, task or bug. The count is space-wide, whatever list the issue is in, and leaves
    out `exclude_id` — the issue being moved — so it is never counted twice.
    """
    if (not status.counts_toward_wip or status.wip_limit is None
            or issue_type not in WIP_TYPES):
        return None
    query = _wip_issues(db, workspace_id, space_id).filter(Issue.status_id == status.id)
    if exclude_id is not None:
        query = query.filter(Issue.id != exclude_id)
    count = query.count() + 1
    limit = int(status.wip_limit)
    return (count, limit) if count > limit else None


def wip_breach(db: Session, issue: Issue, status: WorkflowStatus) -> tuple[int, int] | None:
    """`stage_wip_breach` for moving `issue` into `status`. A reorder inside its own stage is
    never checked: it adds nothing to the work in progress."""
    if status.id == issue.status_id:
        return None
    return stage_wip_breach(db, str(issue.workspace_id), str(issue.space_id), status,
                            str(issue.type), exclude_id=str(issue.id))


def wip_refusal(status: WorkflowStatus, limit: int) -> str:
    """The 409 a `block` policy answers with: the limit, and what to do."""
    return (f"{status.name} is at its WIP limit of {limit}. Finish or move something out of "
            "it first.")


def wip_warning(status: WorkflowStatus, count: int, limit: int) -> str:
    """The clause a `warn` policy adds to a successful answer."""
    return f"{status.name} is over its WIP limit ({count} of {limit})."


# ---- filters (09-restructuring.md §5.1) ---------------------------------------------------

#: A whole issue key typed into a search box. Case-insensitive, unlike the scan of commit
#: messages (utils/issue_keys.py), which has to guard against a slug being read as a key: a
#: person searching for `pay-7` means the issue, and there is nothing else it could match.
_WHOLE_KEY = re.compile(rf"({SPACE_KEY_PATTERN})-([1-9][0-9]{{0,8}})", re.IGNORECASE)


def parse_issue_key(text: str | None) -> tuple[str, int] | None:
    """`("PAY", 7)` when `text` is exactly an issue key, else None."""
    match = _WHOLE_KEY.fullmatch((text or "").strip())
    if match is None:
        return None
    return match.group(1).upper(), int(match.group(2))


def subtree_ids(db: Session, workspace_id: str, issue_id: str) -> list[str]:
    """`issue_id` and every live issue beneath it, at any depth: the `epic_id` filter.

    One query per level, the walk `services/time_tracking.descendant_ids` makes; written here
    rather than imported because that module imports this one. Each level is tenant-filtered.
    """
    found = [issue_id]
    seen = {issue_id}
    frontier = [issue_id]
    # The legacy rules allowed five levels (utils/hierarchy_rules.LEGACY_MAX_DEPTH); sixteen
    # is the same guard `is_ancestor` keeps against rows written before any rules existed.
    for _ in range(16):
        if not frontier:
            break
        children = [child_id for (child_id,) in
                    db.query(Issue.id)
                    .filter(Issue.workspace_id == workspace_id,
                            Issue.parent_id.in_(frontier), Issue.deleted == 0)
                    .all()
                    if child_id not in seen]
        seen.update(children)
        found.extend(children)
        frontier = children
    return found


def apply_filters(db: Session, query: Query, workspace_id: str, user_id: str,
                  filters: IssueFilters) -> Query:
    """`query` (over `Issue`, already tenant-filtered) narrowed by the board's filters.

    `q` matches a title substring, or — when it is a whole key — the issue that key names
    now, through the one resolver (services/key_resolution.py), so a key quoted before the
    issue moved still finds it.
    """
    if filters.assignee_id == "none":
        query = query.filter(Issue.assignee_id.is_(None))
    elif filters.assignee_id:
        query = query.filter(Issue.assignee_id == (
            user_id if filters.assignee_id == "me" else filters.assignee_id))
    if filters.type:
        query = query.filter(Issue.type.in_(list(filters.type)))
    if filters.priority:
        query = query.filter(Issue.priority.in_(list(filters.priority)))
    if filters.blocked is not None:
        query = query.filter(Issue.blocked.is_(True) if filters.blocked
                             else Issue.blocked.is_(False))
    if filters.epic_id:
        query = query.filter(Issue.id.in_(subtree_ids(db, workspace_id, filters.epic_id)))
    term = (filters.q or "").strip()
    if term:
        title = Issue.title.ilike(f"%{escape_like(term)}%", escape="\\")
        key = parse_issue_key(term)
        found = key_resolution.resolve_key(db, workspace_id, *key) if key else None
        query = query.filter(or_(title, Issue.id == found[0].id) if found else title)
    return query


# ---- projection ---------------------------------------------------------------------------


def _space_keys(db: Session, space_ids: Iterable[str]) -> dict[str, str]:
    ids = set(space_ids)
    if not ids:
        return {}
    return dict(db.query(Space.id, Space.key).filter(Space.id.in_(ids)).all())


def users_by_id(db: Session, user_ids: Iterable[str | None]) -> dict[str, User]:
    ids = {user_id for user_id in user_ids if user_id}
    if not ids:
        return {}
    return {user.id: user for user in db.query(User).filter(User.id.in_(ids)).all()}


def _pr_states(db: Session, issue_ids: Iterable[str]) -> dict[str, str]:
    ids = set(issue_ids)
    if not ids:
        return {}
    best: dict[str, str] = {}
    rows = (
        db.query(VcsLink.issue_id, VcsLink.state)
        .filter(VcsLink.issue_id.in_(ids), VcsLink.kind == "pull_request")
        .all()
    )
    for issue_id, state in rows:
        if state and _PR_PRECEDENCE.get(state, 0) > _PR_PRECEDENCE.get(best.get(issue_id, ""), 0):
            best[issue_id] = state
    return best


def time_spent_by_issue(db: Session, workspace_id: str,
                        issue_ids: Iterable[str]) -> dict[str, int]:
    """Seconds logged on each issue itself (not its children), for issues with any time.

    One grouped query on `ix_time_entries_issue_work_date`. Summed on read rather than kept
    on the issue row, so logging time never touches `issues.version` (models/time_tracking.py).
    """
    ids = set(issue_ids)
    if not ids:
        return {}
    rows = (
        db.query(TimeEntry.issue_id, func.sum(TimeEntry.duration_seconds))
        .filter(TimeEntry.workspace_id == workspace_id, TimeEntry.issue_id.in_(ids),
                TimeEntry.deleted == 0)
        .group_by(TimeEntry.issue_id)
        .all()
    )
    # `int()`: MySQL's SUM over an INTEGER comes back as a DECIMAL, SQLite's as an int.
    return {issue_id: int(seconds or 0) for issue_id, seconds in rows}


def cards(db: Session, issues: list[Issue]) -> list[dict]:
    """`IssueCard` payloads for `issues`, in the order given. Four queries, whatever N is.

    The fourth, added with time tracking on 2026-10-05, is the grouped SUM of time logged;
    it kept the board at the six queries `controllers/issue_controller.get_board` counted
    then (the restructuring's workflow and WIP added a fixed four more, measured 2026-10-06,
    none of them per card).

    The blocked flag and reason, the stage age and the backlog rank (09-restructuring.md §5)
    are columns of the issue row and cost nothing; anything that needs another table — the
    keys an issue used to have, who blocked it — is `detail`'s, never the card's, so
    projecting five hundred cards still costs four queries.
    """
    keys = _space_keys(db, (issue.space_id for issue in issues))
    users = users_by_id(db, (issue.assignee_id for issue in issues))
    prs = _pr_states(db, (issue.id for issue in issues))
    spent = (time_spent_by_issue(db, issues[0].workspace_id, (issue.id for issue in issues))
             if issues else {})
    return [
        {
            "id": issue.id,
            "key": format_issue_key(keys.get(issue.space_id, "?"), issue.number),
            "type": issue.type,
            "title": issue.title,
            "status_id": issue.status_id,
            "list_id": issue.list_id,
            "parent_id": issue.parent_id,
            "priority": issue.priority,
            "story_points": issue.story_points,
            "sprint_id": issue.sprint_id,
            "time_estimate_seconds": issue.time_estimate_seconds,
            "time_spent_seconds": spent.get(issue.id, 0),
            "assignee": user_ref(users.get(issue.assignee_id)) if issue.assignee_id else None,
            "rank": issue.rank,
            "backlog_rank": issue.backlog_rank,
            "start_date": issue.start_date,
            "due_date": issue.due_date,
            "pull_request_state": prs.get(issue.id),
            "blocked": bool(issue.blocked),
            "blocked_reason": issue.blocked_reason if issue.blocked else None,
            "status_entered_at": issue.status_entered_at,
            "version": issue.version,
        }
        for issue in issues
    ]


def detail(db: Session, issue: Issue) -> dict:
    """`IssueDetail`: the card plus description, reporter, children, links, the epic's
    completion criteria, who blocked it and the keys it used to have."""
    payload = cards(db, [issue])[0]
    children = (
        db.query(Issue)
        .filter(Issue.workspace_id == issue.workspace_id, Issue.parent_id == issue.id,
                Issue.deleted == 0)
        .order_by(Issue.rank)
        .all()
    )
    links = (
        db.query(VcsLink)
        .filter(VcsLink.workspace_id == issue.workspace_id, VcsLink.issue_id == issue.id)
        .order_by(VcsLink.updated_at.desc())
        .all()
    )
    # The reporter and whoever blocked it in one read.
    people = users_by_id(db, (issue.reporter_id, issue.blocked_by_id if issue.blocked else None))
    payload.update({
        "description": issue.description,
        "space_id": issue.space_id,
        "reporter": (user_ref(people.get(issue.reporter_id))
                     or {"id": issue.reporter_id, "display_name": ""}),
        "completion_criteria": issue.completion_criteria,
        "previous_keys": key_resolution.previous_keys(db, str(issue.workspace_id),
                                                      str(issue.id)),
        "blocked_at": issue.blocked_at if issue.blocked else None,
        "blocked_by": (user_ref(people.get(issue.blocked_by_id))
                       if issue.blocked and issue.blocked_by_id else None),
        "children": cards(db, children),
        "links": [
            {"id": link.id, "kind": link.kind, "external_id": link.external_id,
             "url": link.url, "title": link.title, "state": link.state,
             "author_login": link.author_login, "updated_at": link.updated_at}
            for link in links
        ],
        "created_at": issue.created_at,
        "updated_at": issue.updated_at,
    })
    return payload


def comments(db: Session, workspace_id: str, issue_id: str) -> list[IssueComment]:
    return (
        db.query(IssueComment)
        .filter(IssueComment.workspace_id == workspace_id, IssueComment.issue_id == issue_id,
                IssueComment.deleted == 0)
        .order_by(IssueComment.created_at, IssueComment.id)
        .all()
    )


def comment_payloads(db: Session, rows: list[IssueComment]) -> list[dict]:
    authors = users_by_id(db, (row.author_id for row in rows))
    comment_ids = [row.id for row in rows]
    attachments_by_comment: dict[str, list[dict]] = {cid: [] for cid in comment_ids}
    if comment_ids:
        from models.storage import IssueAttachment
        from services import storage_service as storage_svc

        att_rows = (
            db.query(IssueAttachment)
            .filter(
                IssueAttachment.comment_id.in_(comment_ids),
                IssueAttachment.deleted == 0,
            )
            .order_by(IssueAttachment.created_at.asc())
            .all()
        )
        for att in att_rows:
            if att.comment_id and att.comment_id in attachments_by_comment:
                attachments_by_comment[att.comment_id].append(
                    storage_svc.attachment_summary(db, att).model_dump()
                )

    return [
        {
            "id": row.id,
            "author": user_ref(authors.get(row.author_id))
            or {"id": row.author_id, "display_name": ""},
            "body": row.body,
            "created_at": row.created_at,
            "attachments": attachments_by_comment.get(row.id, []),
        }
        for row in rows
    ]


def escape_like(term: str) -> str:
    """`term` with LIKE's wildcards made literal, for use with `escape="\\\\"`.

    Without it a search for `100%` matches everything that starts with `100`, and a search
    for `_` matches every title — harmless here, but it is also a way to make one request
    scan a whole tenant's issues on purpose.
    """
    return term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
