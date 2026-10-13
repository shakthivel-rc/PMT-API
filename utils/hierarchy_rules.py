"""Which issue types may sit under which (docs/design/09-restructuring.md §2).

Pure and type-checked, like `utils/workflow_rules.py`, for the same reasons: the request
schemas and the migration dry-run both ask these questions, and neither should need a
database to.

**Two rule sets, one principle.** The *standard* convention is the restructuring plan's
target: Story, Task and Bug are peer delivery items under an optional Epic, and Subtasks are
execution detail under any of the three. The *legacy* rules are what PMT allowed until
2026-10-05 — a task under a story, a bug under anything — and existing rows were written
under them. **A relationship that is created or changed follows the standard; one that is
left alone is kept.** Flattening a Story > Task tree automatically would decide what those
tasks *meant*, which the plan leaves to a person ("review their meaning before flattening or
retyping them"); refusing to read or edit such a tree would strand the data.
"""
from __future__ import annotations

ISSUE_TYPES: tuple[str, ...] = ("epic", "story", "task", "subtask", "bug")

#: The standard convention. An empty set means "top level only".
STANDARD_PARENT_TYPES: dict[str, frozenset[str]] = {
    "epic": frozenset(),
    "story": frozenset({"epic"}),
    "task": frozenset({"epic"}),
    "bug": frozenset({"epic"}),
    "subtask": frozenset({"story", "task", "bug"}),
}

#: What the rules accepted before 2026-10-05 (`models/issue.ALLOWED_PARENT_TYPES` as it was).
#: Only ever used to decide whether an EXISTING relationship is one PMT itself allowed — never
#: to accept a new one.
LEGACY_PARENT_TYPES: dict[str, frozenset[str]] = {
    "epic": frozenset(),
    "story": frozenset({"epic"}),
    "task": frozenset({"epic", "story"}),
    "subtask": frozenset({"story", "task"}),
    "bug": frozenset({"epic", "story", "task", "subtask"}),
}

#: A sub-task of nothing is a task.
PARENT_REQUIRED: frozenset[str] = frozenset({"subtask"})

#: The deepest a standard tree goes: epic > story/task/bug > subtask.
STANDARD_MAX_DEPTH = 3
#: The deepest the legacy rules allowed: epic > story > task > subtask > bug.
LEGACY_MAX_DEPTH = 5


def _article(word: str) -> str:
    return "An" if word[:1] in "aeiou" else "A"


def standard_parent_problem(child_type: str, parent_type: str | None) -> str | None:
    """Why an issue of `child_type` may not be put under one of `parent_type` (None: no
    parent) under the standard convention, or None when it may. Written to be shown."""
    if child_type not in STANDARD_PARENT_TYPES:
        return f"Unknown issue type {child_type!r}."
    if parent_type is None:
        if child_type in PARENT_REQUIRED:
            return f"{_article(child_type)} {child_type} needs a parent."
        return None
    allowed = STANDARD_PARENT_TYPES[child_type]
    if parent_type in allowed:
        return None
    if not allowed:
        return f"{_article(child_type)} {child_type} cannot have a parent."
    return (f"{_article(child_type)} {child_type} can sit under: "
            f"{', '.join(sorted(allowed))}.")


def is_standard_relationship(child_type: str, parent_type: str | None) -> bool:
    return standard_parent_problem(child_type, parent_type) is None


def is_legacy_relationship(child_type: str, parent_type: str) -> bool:
    """Allowed by the pre-2026-10-05 rules but not by the standard: kept, and listed by the
    inventory for review."""
    return (parent_type in LEGACY_PARENT_TYPES.get(child_type, frozenset())
            and parent_type not in STANDARD_PARENT_TYPES.get(child_type, frozenset()))


def is_known_relationship(child_type: str, parent_type: str | None) -> bool:
    """Allowed by either rule set — what a reconciliation accepts as a legal relationship
    that may legitimately exist in the data."""
    if parent_type is None:
        return child_type not in PARENT_REQUIRED
    return (parent_type in STANDARD_PARENT_TYPES.get(child_type, frozenset())
            or parent_type in LEGACY_PARENT_TYPES.get(child_type, frozenset()))
