"""Applying a verified GitHub delivery: link pull requests, and run each space's merge action.

By the time this runs, `routes/integrations.py` has checked the signature. Everything here
happens in one transaction, **including the delivery-id record**, so a delivery that fails
half way leaves no trace and GitHub's redelivery gets a clean second attempt, while one that
succeeded is never applied twice.

**The one automation rule Phase 1 ships** is hard-wired here: *when a linked pull request is
merged, move the issue to the first `done` column of its space.* It is the rule every team
asks for first, and hard-wiring it is the honest size for one rule. The rules engine that
generalises it — trigger, conditions, actions, stored per workspace — is Phase 2
(docs/design/04-api-and-views.md, "Automation"), and this function becomes its first
built-in rule.

**2026-10-05: the rule became each space's merge action** (docs/design/09-restructuring.md
§6). "The first `done` column" chose the target by position, which is exactly the inference
the restructuring plan rules out: a team that accepts work in QA *after* the merge had it
marked delivered at the merge, and its sprint reports with it. A space now says what a merge
does — move the issue to a stage it chose (`spaces.merge_target_status_id`, in any category)
or only link (`merge_action = none`) — and the migration aimed every existing space at the
stage the old rule picked, so nothing changed for a space until someone changes it. What a
merged delivery does to each issue it names, in order:

1. **Keys resolve through `services/key_resolution`**, old keys included (§7.3): a branch cut
   before its issue moved to another space, or before the space was re-keyed, still finds it,
   and the link goes to the issue as it is now.
2. **An issue in a frozen space defers the whole delivery** (§6.3): nothing is written but
   the delivery row, which keeps the payload; GitHub is told 200 so it does not retry, and the
   delivery is replayed through this same path when the space is unfrozen. A migration wave
   reconciles links against a baseline (§8.6), so a link that slipped in mid-wave would be a
   discrepancy the plan cannot explain.
3. **An issue in an archived list, folder or space is skipped** — named in `skipped`, with
   nothing written to it, not even the link. Archived is read-only (§3.4), for GitHub too.
4. **The merge action.** Already at the target, or in a closed category: nothing — a merge
   never reopens or re-closes work. A target that is no longer a live stage, or a move the
   space's restricted flow does not allow from where the issue is: a **failure**, recorded as
   an `automation_failed` event on the issue, named in `failed`, and logged as a warning.
   **Never a fallback** to some other stage: guessing a completed stage is how work got
   marked delivered before acceptance in the first place.

**The merge action runs on the delivery that makes the link `merged`**, not on every delivery
that says the pull request is merged. GitHub sends `edited` for a merged pull request whose
title is fixed afterwards, and that delivery says `merged` too. While the only target was a
closed stage this did not matter, because "already closed: nothing" absorbed the repeat; with
an open target it would — a team that sends work from QA back to Development for rework would
watch it jump back to QA the next time somebody edited the merged pull request's title. So
the action runs when the link is new or was `open`/`closed` before this delivery: the merge
itself, a merged pull request linked after the fact (as before), and a replayed merge.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from dependencies.workspace import WorkspaceAccess
from integrations.github import PullRequestEvent, parse_pull_request
from models.hierarchy import Space, WorkflowStatus
from models.issue import Issue
from models.vcs import VcsLink, VcsRepository, WebhookDelivery
from schemas.response import SuccessResponse
from services import issue as issue_service
from services import key_resolution, transitions, write_guard
from services import workspace as workspace_service
from utils import workflow_rules as rules
from utils.issue_keys import format_issue_key

logger = logging.getLogger("base_project.github")

#: `webhook_deliveries.note` is VARCHAR(255); `issue_events.to_value` is too.
_NOTE_LENGTH = 255

#: The most deferred deliveries the admin listing returns. A delivery is deferred only while
#: a space is frozen for one migration wave — hours, during which a busy repository sends
#: tens of pull-request deliveries — so 200 is well past any real backlog, and it is the page
#: cap the issue list already uses (controllers/issue_controller.MAX_PAGE_SIZE). Each row
#: carries a whole GitHub payload, which is what bounds it at all: 200 payloads parsed for
#: their keys is a read an admin page can afford, an unbounded number is not.
MAX_DEFERRED_LISTED = 200

#: Why the merge action did nothing, for a target that is no longer a live stage. A workflow
#: publish refuses to remove the target (`merge_target_invalid`, §4.4), so this is a stage
#: removed some other way — and the person who can fix it is whoever edits the workflow.
_TARGET_REMOVED = ("This space's merge action moves issues to a stage that is no longer part "
                   "of its workflow, so nothing was moved. Someone who edits the workflow can "
                   "choose another target.")
_NO_TARGET = ("This space's merge action has no target stage, so nothing was moved. Someone "
              "who edits the workflow can choose one, or set the merge action to link only.")
_STAGE_UNKNOWN = "The issue's current stage could not be read, so nothing was moved."


def _now() -> datetime:
    return datetime.now(UTC)


def _clip(text: str, limit: int = _NOTE_LENGTH) -> str:
    return text if len(text) <= limit else text[:limit - 3] + "..."


def _naive_utc(value: datetime) -> datetime:
    """`value` as naive UTC. MySQL's DATETIME and SQLite hand values back naive, while one set
    in this process and not yet reloaded is aware — and comparing one of each raises."""
    return value.astimezone(UTC).replace(tzinfo=None) if value.tzinfo else value


@dataclass
class _Outcome:
    """What applying one pull-request delivery did — or, with `frozen_keys`, why it did
    nothing yet. Lists hold issue keys as the issues are called now."""

    frozen_keys: list[str] = field(default_factory=list)
    linked: list[str] = field(default_factory=list)
    moved: list[str] = field(default_factory=list)
    moved_to_done: list[str] = field(default_factory=list)
    failed: list[dict[str, str]] = field(default_factory=list)
    skipped: list[dict[str, str]] = field(default_factory=list)
    # How many of the issues were found through an old key — worth a field in the log line
    # because "do old references still work" is a migration gate (plan §7).
    resolved_by_alias: int = 0

    @property
    def deferred(self) -> bool:
        return bool(self.frozen_keys)


def _result(event: str, *, handled: bool = False, duplicate: bool = False,
            deferred: bool = False, outcome: _Outcome | None = None, note: str = "") -> dict:
    outcome = outcome or _Outcome()
    return {"event": event, "handled": handled, "duplicate": duplicate,
            "linked": outcome.linked, "moved_to_done": outcome.moved_to_done,
            "moved": outcome.moved, "failed": outcome.failed, "skipped": outcome.skipped,
            "deferred": deferred, "note": note}


def _find_issues(db: Session, workspace_id: str,
                 keys: list[tuple[str, int]]) -> list[tuple[Issue, str]]:
    """The live issues `keys` name, each once, in first-mentioned order, with how its key was
    resolved (`key_resolution.RESOLVED_*`).

    Deduplicated by issue id, because two keys can name one issue — its old key in the branch,
    its new one in the title — and that is still one link and one merge action.
    """
    found: dict[str, tuple[Issue, str]] = {}
    for space_key, number in keys:
        resolved = key_resolution.resolve_key(db, workspace_id, space_key, number)
        if resolved is not None:
            found.setdefault(str(resolved[0].id), resolved)
    return list(found.values())


def _lock_spaces(db: Session, workspace_id: str, space_ids: set[str]) -> dict[str, Space]:
    """The spaces the delivery touches, row-locked until it commits.

    The lock is what makes "frozen: defer" true rather than probably true. Without it, a
    freeze — or a workflow publish, which runs under the same row lock (§4.4) — committing
    between this delivery's check and its commit would let a link land in a frozen space, or a
    move land in a stage that was just removed. With it, the other writer waits for this
    delivery to commit, or this delivery waits for the other and then reads what it wrote: a
    locking read sees the latest committed row, and `populate_existing` makes the session
    believe it rather than an object it loaded earlier. In id order, so two deliveries that
    touch the same two spaces cannot deadlock. SQLite ignores FOR UPDATE; it has one writer.

    What it does not buy, written down rather than pretended away: under MySQL's REPEATABLE
    READ the plain reads after it (the stages, the transitions) still come from the snapshot
    the delivery's first read took, so a publish committing in the milliseconds before the
    lock can make this merge fail — visibly, as an `automation_failed` — rather than see the
    new stages. The freeze is read from the locked row itself, so deferral has no such gap.
    """
    if not space_ids:
        return {}
    rows = (db.query(Space)
            .filter(Space.workspace_id == workspace_id, Space.id.in_(sorted(space_ids)))
            .order_by(Space.id)
            .populate_existing()
            .with_for_update()
            .all())
    return {str(row.id): row for row in rows}


def _upsert_link(db: Session, repo: VcsRepository, issue: Issue, pr: PullRequestEvent,
                 received_at: datetime | None = None) -> str | None:
    """Create or refresh the link. Returns the state the link had before this delivery, or
    None when it is new.

    `received_at` is given on a replay. A deferred delivery is older than anything applied
    since it arrived, so it refreshes a link only if no later delivery has touched it: a pull
    request whose title stopped naming the frozen issue was applied at once, and replaying the
    older delivery afterwards must not put its stale `open` back over a newer `merged`. A live
    delivery is the newest thing there is and always refreshes.
    """
    link = (
        db.query(VcsLink)
        .filter(VcsLink.workspace_id == issue.workspace_id, VcsLink.issue_id == issue.id,
                VcsLink.provider == "github", VcsLink.kind == "pull_request",
                VcsLink.external_id == pr.external_id)
        .first()
    )
    if link is None:
        db.add(VcsLink(workspace_id=issue.workspace_id, issue_id=issue.id,
                       repository_id=repo.id, provider="github", kind="pull_request",
                       external_id=pr.external_id, url=pr.url, title=pr.title,
                       state=pr.state, author_login=pr.author_login))
        issue_service.record_event(db, issue, "linked", None, "integration",
                                   to_value=pr.external_id)
        return None
    previous = link.state
    if received_at is not None and _naive_utc(link.updated_at) > _naive_utc(received_at):
        return previous
    link.url, link.title, link.state, link.author_login = (pr.url, pr.title, pr.state,
                                                           pr.author_login)
    return previous


def _merge_action(db: Session, issue: Issue,
                  space: Space) -> tuple[WorkflowStatus | None, tuple[str, str] | None]:
    """Run the space's merge action on one issue (09-restructuring.md §6.2).

    Returns `(stage moved to, None)`, `(None, (reason code, reason))` for a failure, or
    `(None, None)` when the merge leaves the issue where it is by design. The order of the
    checks is the design's: what leaves an issue alone by design is decided before what
    counts as a failure, so a merge on finished work never writes a failure onto it.
    """
    if space.merge_action != "status":
        return None, None
    if space.merge_target_status_id and issue.status_id == space.merge_target_status_id:
        return None, None
    current = (db.query(WorkflowStatus)
               .filter(WorkflowStatus.workspace_id == issue.workspace_id,
                       WorkflowStatus.id == issue.status_id)
               .first())
    if current is not None and rules.is_closed(current.category):
        return None, None
    target = transitions.merge_target(db, str(issue.workspace_id), space)
    if target is None:
        if space.merge_target_status_id:
            return None, ("target_removed", _TARGET_REMOVED)
        return None, ("no_target", _NO_TARGET)
    if current is None:
        return None, ("stage_unknown", _STAGE_UNKNOWN)
    problem = transitions.transition_problem(db, str(issue.workspace_id), space, current,
                                             target)
    if problem:
        return None, ("transition_not_allowed", problem)
    rank = issue_service.bottom_rank(db, issue.workspace_id, issue.space_id, target.id,
                                     exclude_id=issue.id)
    issue_service.set_status(db, issue, target, rank, None, "automation")
    return target, None


def _record_failure(db: Session, issue: Issue, space: Space, key: str, code: str,
                    reason: str, delivery_id: str) -> None:
    """Two of the three parts of an explicit failure: the event the issue's history shows, and
    the warning an operator greps for. (The third, the key in `failed`, is the caller's.)

    The log line carries ids and a reason code — no payload, no title, no stage names — so it
    is safe to ship anywhere logs go.
    """
    issue_service.record_event(db, issue, "automation_failed", None, "automation",
                               from_value=str(issue.status_id), to_value=reason[:_NOTE_LENGTH])
    logger.warning("github merge action failed", extra={"extra_fields": {
        "delivery_id": delivery_id, "workspace_id": issue.workspace_id,
        "space_id": space.id, "issue_id": issue.id, "issue_key": key,
        "status_id": issue.status_id, "target_status_id": space.merge_target_status_id,
        "reason": code}})


def _apply_pull_request(db: Session, repo: VcsRepository, pr: PullRequestEvent, *,
                        delivery_id: str, received_at: datetime | None = None) -> _Outcome:
    """Apply one pull-request delivery — the one path a live delivery and a replay share.

    Nothing is written before the freeze check, so a deferred delivery leaves no trace but its
    `webhook_deliveries` row. Does not commit: the caller owns the transaction, and the
    delivery row's status is written in it.
    """
    workspace_id = str(repo.workspace_id)
    resolved = _find_issues(db, workspace_id, pr.keys)
    spaces = _lock_spaces(db, workspace_id, {str(issue.space_id) for issue, _ in resolved})
    outcome = _Outcome(resolved_by_alias=sum(
        1 for _, how in resolved if how != key_resolution.RESOLVED_CURRENT))
    outcome.frozen_keys = sorted({str(space.key) for space in spaces.values()
                                  if space.frozen_at is not None})
    if outcome.frozen_keys:
        return outcome

    for issue, _how in resolved:
        space = spaces.get(str(issue.space_id))
        if space is None:
            # Unreachable — `issues.space_id` is a foreign key — and here so that a row that
            # breaks it costs this one issue rather than the delivery.
            continue
        key = format_issue_key(str(space.key), int(issue.number))
        # The one write guard (§3.4). A freeze has already deferred the delivery, so what it
        # can refuse here is an archived list, folder or space.
        block = write_guard.issue_block(db, issue)
        if block is not None:
            outcome.skipped.append({"key": key, "reason": block.message})
            continue
        previous_state = _upsert_link(db, repo, issue, pr, received_at)
        outcome.linked.append(key)
        if pr.state != "merged" or previous_state == "merged":
            continue
        moved_to, failure = _merge_action(db, issue, space)
        if moved_to is not None:
            outcome.moved.append(key)
            if rules.is_completed(moved_to.category):
                outcome.moved_to_done.append(key)
        elif failure is not None:
            code, reason = failure
            _record_failure(db, issue, space, key, code, reason, delivery_id)
            outcome.failed.append({"key": key, "reason": reason})
    return outcome


def _deferred_note(space_keys: list[str]) -> str:
    if len(space_keys) == 1:
        text = (f"Deferred: the space {space_keys[0]} is frozen for a migration. This "
                "delivery is applied when it is unfrozen.")
    else:
        text = (f"Deferred: the spaces {', '.join(space_keys)} are frozen for a migration. "
                "This delivery is applied when they are unfrozen.")
    return _clip(text)


def _duplicate_note(db: Session, workspace_id: str, delivery_id: str) -> str:
    status = (db.query(WebhookDelivery.status)
              .filter(WebhookDelivery.workspace_id == workspace_id,
                      WebhookDelivery.id == delivery_id)
              .scalar())
    if status == "deferred":
        return "delivery already received; it is deferred until its space is unfrozen"
    return "delivery already processed"


def apply_delivery(db: Session, event: str, delivery_id: str, payload: dict) -> dict:
    if event == "ping":
        return _result(event, note="pong")
    if event != "pull_request":
        return _result(event, note="event not handled")

    pr = parse_pull_request(payload)
    if pr is None:
        return _result(event, note="action not handled")
    repo = workspace_service.repository_by_name(db, "github", pr.repository)
    if repo is None:
        # 200, not 4xx: an unlinked repository is a configuration state, not a bad request,
        # and a 4xx would have GitHub mark the hook as failing and retry it.
        return _result(event, note="repository is not linked to a workspace")
    if workspace_service.get_workspace(db, str(repo.workspace_id)) is None:
        # Since 2026-10-06 a workspace can be deleted, and it keeps its repositories — one
        # repository feeds one workspace, and a restore must find its links where it left
        # them. But nothing is applied to a workspace nobody can see: merges would go on
        # moving its issues, unseen, for as long as the repository's hook is set up. 200 for
        # the reason above; nothing is recorded, as for an unlinked repository.
        return _result(event, note="the workspace this repository is linked to was deleted")
    workspace_id = str(repo.workspace_id)

    delivery = WebhookDelivery(id=delivery_id, provider="github", event=event,
                               workspace_id=workspace_id, status="processed")
    try:
        db.add(delivery)
        db.flush()
    except IntegrityError:
        db.rollback()
        return _result(event, duplicate=True,
                       note=_duplicate_note(db, workspace_id, delivery_id))

    outcome = _apply_pull_request(db, repo, pr, delivery_id=delivery_id)
    if outcome.deferred:
        note = _deferred_note(outcome.frozen_keys)
        delivery.status = "deferred"
        # The only copy of what GitHub said: it was told 200 and will not send it again.
        delivery.payload = payload
        delivery.note = note
        db.commit()
        logger.info("github delivery deferred", extra={"extra_fields": {
            "delivery_id": delivery_id, "workspace_id": workspace_id,
            "pull_request": pr.external_id, "frozen_spaces": outcome.frozen_keys}})
        return _result(event, deferred=True, note=note)

    delivery.processed_at = _now()
    db.commit()
    logger.info("github delivery applied", extra={"extra_fields": {
        "delivery_id": delivery_id, "workspace_id": workspace_id,
        "pull_request": pr.external_id, "state": pr.state, "linked": len(outcome.linked),
        "moved": len(outcome.moved), "moved_to_done": len(outcome.moved_to_done),
        "failed": len(outcome.failed), "skipped": len(outcome.skipped),
        "resolved_by_alias": outcome.resolved_by_alias}})
    return _result(event, handled=True, outcome=outcome)


# ---- deferred deliveries (09-restructuring.md §6.3) -----------------------------------------


def _claim_deferred(db: Session, workspace_id: str, delivery_id: str) -> WebhookDelivery | None:
    """The delivery, row-locked, if it is still deferred. Two admins unfreezing two spaces of
    one workspace at once both replay it; the lock and the re-read status are what make the
    second one find it already replayed instead of applying it again."""
    return (db.query(WebhookDelivery)
            .filter(WebhookDelivery.workspace_id == workspace_id,
                    WebhookDelivery.id == delivery_id,
                    WebhookDelivery.status == "deferred")
            .populate_existing()
            .with_for_update()
            .first())


def _replay_summary(outcome: _Outcome) -> str:
    sections = [
        ("linked", outcome.linked),
        ("moved", outcome.moved),
        ("merge action failed for", [item["key"] for item in outcome.failed]),
        ("skipped", [item["key"] for item in outcome.skipped]),
    ]
    said = [f"{label} {', '.join(keys)}" for label, keys in sections if keys]
    return _clip("Replayed after unfreezing: "
                 + ("; ".join(said) if said else "it named no issue that still exists") + ".")


def _replay_one(db: Session, workspace_id: str, delivery_id: str) -> str | None:
    """Replay one deferred delivery and commit. Returns which count it adds to, or None when
    another replay got to it first."""
    delivery = _claim_deferred(db, workspace_id, delivery_id)
    if delivery is None:
        db.rollback()
        return None
    payload = delivery.payload if isinstance(delivery.payload, dict) else {}
    pr = parse_pull_request(payload)
    repo = workspace_service.repository_by_name(db, "github", pr.repository) if pr else None
    if pr is None or repo is None or str(repo.workspace_id) != workspace_id:
        # Not an exception, and not something waiting will cure: the payload no longer reads
        # as a pull request we act on, or its repository is no longer this workspace's. Kept,
        # payload and all, for whoever investigates.
        delivery.status = "failed"
        delivery.processed_at = _now()
        delivery.note = ("Replay failed: the stored payload is not a pull request this "
                         "service acts on." if pr is None else
                         "Replay failed: the repository is no longer linked to this workspace.")
        db.commit()
        return "failed"

    outcome = _apply_pull_request(db, repo, pr, delivery_id=str(delivery.id),
                                  received_at=delivery.received_at)
    if outcome.deferred:
        # Its issues are still in a frozen space — perhaps not the one first named: a wave can
        # move an issue into another space its plan froze too.
        delivery.note = _deferred_note(outcome.frozen_keys)
        db.commit()
        return "still_deferred"

    delivery.status = "replayed"
    delivery.processed_at = _now()
    delivery.note = _replay_summary(outcome)
    # Applied: GitHub's words are now links and events, and the copy has done its job.
    delivery.payload = None
    db.commit()
    logger.info("github deferred delivery replayed", extra={"extra_fields": {
        "delivery_id": delivery_id, "workspace_id": workspace_id,
        "pull_request": pr.external_id, "state": pr.state, "linked": len(outcome.linked),
        "moved": len(outcome.moved), "failed": len(outcome.failed),
        "skipped": len(outcome.skipped), "resolved_by_alias": outcome.resolved_by_alias}})
    return "replayed"


def _mark_failed(db: Session, workspace_id: str, delivery_id: str, error: Exception) -> None:
    delivery = _claim_deferred(db, workspace_id, delivery_id)
    if delivery is not None:
        delivery.status = "failed"
        delivery.processed_at = _now()
        # The class name only: an exception's text can quote SQL and the values in it. The
        # traceback is in the log, under this delivery id. The payload is kept — nothing of
        # it was applied, so it is still the only copy.
        delivery.note = _clip(f"Replay failed ({type(error).__name__}); nothing of it was "
                              f"applied. The log has the details under delivery {delivery_id}.")
    db.commit()


def replay_deferred(db: Session, workspace_id: str) -> dict:
    """Apply the workspace's deferred deliveries, oldest first (09-restructuring.md §6.3).

    Called when a space is unfrozen. Returns `{replayed, still_deferred, failed}`. Commits
    per delivery, so one failure does not hold back the rest.

    Each goes through `_apply_pull_request`, the path a live delivery takes, without a new
    `webhook_deliveries` row — the deferred row *is* the delivery, and its id is what makes a
    GitHub redelivery a duplicate. One whose issues are still in a frozen space stays deferred.
    One that applies is marked `replayed`, with a summary note, and its payload is cleared.
    One that raises is rolled back alone and marked `failed`, keeping its payload.

    **It commits whatever the caller has pending, first.** The caller is unfreezing: that has
    to be durable before any delivery is applied against it, and a failing delivery's rollback
    must not be able to take it back. A caller that has already committed loses nothing.
    """
    db.commit()
    pending = [str(delivery_id) for (delivery_id,) in
               db.query(WebhookDelivery.id)
               .filter(WebhookDelivery.workspace_id == workspace_id,
                       WebhookDelivery.status == "deferred")
               .order_by(WebhookDelivery.received_at, WebhookDelivery.id)
               .all()]
    counts = {"replayed": 0, "still_deferred": 0, "failed": 0}
    for delivery_id in pending:
        try:
            counted = _replay_one(db, workspace_id, delivery_id)
        except Exception as error:
            # Broad on purpose: whatever went wrong with this delivery, the rest of the
            # workspace's deliveries still deserve their replay.
            db.rollback()
            logger.exception("github deferred delivery replay failed", extra={"extra_fields": {
                "delivery_id": delivery_id, "workspace_id": workspace_id,
                "error": type(error).__name__}})
            _mark_failed(db, workspace_id, delivery_id, error)
            counts["failed"] += 1
            continue
        if counted is not None:
            counts[counted] += 1
    if pending:
        logger.info("github deferred deliveries replayed", extra={"extra_fields": {
            "workspace_id": workspace_id, **counts}})
    return counts


def _quoted_keys(payload: Any) -> list[str]:
    """The keys a stored payload's pull request names, as written in it."""
    pr = parse_pull_request(payload) if isinstance(payload, dict) else None
    return [format_issue_key(space_key, number) for space_key, number in pr.keys] if pr else []


def list_deferred(access: WorkspaceAccess, db: Session):
    """What a freeze is holding back in this workspace, newest first."""
    rows = (db.query(WebhookDelivery)
            .filter(WebhookDelivery.workspace_id == access.workspace_id,
                    WebhookDelivery.status == "deferred")
            .order_by(WebhookDelivery.received_at.desc(), WebhookDelivery.id.desc())
            .limit(MAX_DEFERRED_LISTED)
            .all())
    return SuccessResponse(
        status_code=200,
        message="Deferred deliveries fetched",
        data=[{"id": row.id, "event": row.event, "received_at": row.received_at,
               "note": row.note, "keys": _quoted_keys(row.payload)}
              for row in rows],
    )
