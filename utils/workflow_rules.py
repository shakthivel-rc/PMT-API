"""The vocabulary and the pure rules of a space's workflow (docs/design/09-restructuring.md §4).

No database and no ORM here, on purpose. The request schemas import this vocabulary and they
are type-checked (`make typecheck`); an import of `models/` from them would pull SQLAlchemy's
untyped surface into that check. And the rules a publish validates — reachability above all —
are small graph functions that deserve unit tests needing nothing running.

**Categories are what reports, automations and sprint maths reason about** — never a stage's
name or its position on the board. `canceled` is the fourth category, added on 2026-10-05 so
that work closed without being delivered stops counting as delivered: before it, the only way
to close something was to put it in a `done` stage, which is what the restructuring plan
means by "do not mark canceled work as successfully Done merely to simplify mappings".
"""
from __future__ import annotations

from collections.abc import Iterable

STATUS_CATEGORIES: tuple[str, ...] = ("todo", "in_progress", "done", "canceled")

#: Work that is still going to happen. Everything else is closed.
OPEN_CATEGORIES: frozenset[str] = frozenset({"todo", "in_progress"})
CLOSED_CATEGORIES: frozenset[str] = frozenset({"done", "canceled"})
#: The one category that counts as delivered: velocity, burndown "completed", cycle time,
#: "resolved", the sprint's completed points.
COMPLETED_CATEGORY = "done"

#: How the plan names the categories ("not started, active, or completed"), for messages.
CATEGORY_LABELS: dict[str, str] = {
    "todo": "Not started",
    "in_progress": "Active",
    "done": "Completed",
    "canceled": "Canceled",
}

#: A stage's colour is one of the SPA's theme-safe tones (`components/StatusBadge.tsx`), never
#: a hex value: ten themes, light and dark, and a hex colour is legible in at most some of
#: them. The name is always drawn beside it, so colour is never the only signal.
STAGE_COLORS: tuple[str, ...] = ("neutral", "info", "primary", "success", "warning", "danger")
DEFAULT_COLOR_BY_CATEGORY: dict[str, str] = {
    "todo": "neutral",
    "in_progress": "info",
    "done": "success",
    "canceled": "neutral",
}

#: `any`: every stage reaches every other and column order imposes nothing. `restricted`:
#: only the stored transitions are allowed.
TRANSITION_MODES: tuple[str, ...] = ("any", "restricted")
#: What a move over a stage's WIP limit does: succeed and say so, or be refused.
WIP_POLICIES: tuple[str, ...] = ("warn", "block")
#: What a merged, linked pull request does: move the issue to the space's chosen stage, or
#: only link it. There is deliberately no "first done stage" mode: choosing the target by
#: position is the inference §6.1 rules out.
MERGE_ACTIONS: tuple[str, ...] = ("status", "none")
DELIVERY_METHODS: tuple[str, ...] = ("scrum", "kanban")

#: A board column is 288 px wide; twenty stages is already a 5,760 px board. Past that the
#: board stops being something a person scans, which is the only thing a board is for.
MAX_STAGES = 20
#: `workflow_statuses.name` is VARCHAR(50).
MAX_STAGE_NAME = 50
#: A limit is a count of cards a team can hold in one stage; four digits would be a typo.
MAX_WIP_LIMIT = 999

#: What counts toward a stage's WIP. Epics are outcomes, not work in progress, and a subtask
#: is part of an item that is already counted — counting both would show a team of four
#: "at 11 of 4" because one story has seven subtasks.
WIP_TYPES: frozenset[str] = frozenset({"story", "task", "bug"})

#: The stage reference a draft uses for a stage that does not exist yet: `new:<anything>`.
NEW_STAGE_PREFIX = "new:"


def is_closed(category: str | None) -> bool:
    return category in CLOSED_CATEGORIES


def is_completed(category: str | None) -> bool:
    return category == COMPLETED_CATEGORY


def category_class(category: str | None) -> str:
    """`open`, `done` or `canceled` — the classes a status mapping must not cross silently
    (§7.2). Unknown categories read as open: an issue the rules cannot place is not closed."""
    if category == "done":
        return "done"
    if category == "canceled":
        return "canceled"
    return "open"


Transition = tuple[str | None, str]


def transition_allowed(mode: str, transitions: Iterable[Transition], from_id: str,
                       to_id: str) -> bool:
    """Whether an issue in `from_id` may move to `to_id`.

    Staying in the same stage (a reorder) is always allowed. In `restricted` mode a
    transition whose `from` is None is a global one: it leaves every stage.
    """
    if from_id == to_id or mode != "restricted":
        return True
    return any(to == to_id and source in (None, from_id) for source, to in transitions)


def destinations(mode: str, stage_ids: Iterable[str], transitions: Iterable[Transition],
                 from_id: str) -> list[str]:
    """The stages an issue in `from_id` may move to, in the order of `stage_ids`."""
    pairs = list(transitions)
    return [stage for stage in stage_ids
            if stage != from_id and transition_allowed(mode, pairs, from_id, stage)]


def reachable(initial: str, stage_ids: Iterable[str],
              transitions: Iterable[Transition]) -> set[str]:
    """Every stage reachable from `initial` by following transitions (restricted mode).

    A global transition (`from` None) is reachable from wherever the walk has got to, so its
    target is reachable as soon as anything is — which is from the initial stage itself.
    """
    stages = set(stage_ids)
    pairs = [(source, to) for source, to in transitions if to in stages]
    seen = {initial} if initial in stages else set()
    frontier = list(seen)
    while frontier:
        current = frontier.pop()
        for source, to in pairs:
            if source in (None, current) and to not in seen:
                seen.add(to)
                frontier.append(to)
    return seen


def dead_ends(stage_ids: Iterable[str], categories: dict[str, str],
              transitions: Iterable[Transition]) -> list[str]:
    """Open stages nothing leaves, in restricted mode: work that enters them is stuck.

    Closed stages are allowed to be terminal — that is what closed means.
    """
    pairs = list(transitions)
    stuck = []
    for stage in stage_ids:
        if categories.get(stage) not in OPEN_CATEGORIES:
            continue
        # A global transition leaves every stage except its own target.
        leaves = any(source in (None, stage) and to != stage for source, to in pairs)
        if not leaves:
            stuck.append(stage)
    return stuck
