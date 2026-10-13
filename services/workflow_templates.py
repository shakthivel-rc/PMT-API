"""Workflow templates: the built-in starting points, and turning one into a new space's stages.

docs/design/09-restructuring.md §4.5. A template is a *definition*, not a set of rows: stages
by position, and the initial stage, the transitions and the merge target as indexes into that
list — the shape `schemas/workflow.TemplateDefinition` validates. Built-in templates live
here, in code, so every workspace has them and nobody can delete them; a workspace's own are
rows of `workflow_templates`.

The three built-ins are the restructuring plan's examples (its §4: "Delivery", "Support", and
the four-stage default every space starts with). They are starting points. Nothing enforces
their shape after they are applied: the space's editors change whatever they like.
"""
from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from models.hierarchy import Space, WorkflowStatus, WorkflowTransition
from utils import workflow_rules as rules

#: Stage: (name, category, colour, counts toward WIP). WIP limits start empty: a limit is
#: set from a team's observed capacity (plan §4 Kanban policy), not from a template.
_Stage = tuple[str, str, str, bool]


def _definition(stages: list[_Stage], *, mode: str, initial: int,
                transitions: list[tuple[int | None, int]],
                merge_target: int | None) -> dict[str, Any]:
    return {
        "transition_mode": mode,
        "wip_policy": "warn",
        "stages": [{"name": name, "category": category, "color": color,
                    "counts_toward_wip": wip, "wip_limit": None}
                   for name, category, color, wip in stages],
        "initial": initial,
        "transitions": [{"from": source, "to": to} for source, to in transitions],
        "merge_target": merge_target,
    }


BUILTIN_TEMPLATES: dict[str, dict[str, Any]] = {
    "default": {
        "name": "Default (four stages)",
        "description": "To Do, In Progress, In Review, Done. Any stage can move to any other; "
                       "a merged pull request moves its issue to Done.",
        "definition": _definition(
            [("To Do", "todo", "neutral", False),
             ("In Progress", "in_progress", "info", True),
             ("In Review", "in_progress", "info", True),
             ("Done", "done", "success", False)],
            mode="any", initial=0, transitions=[], merge_target=3),
    },
    "delivery": {
        "name": "Delivery with QA",
        "description": "Backlog → Ready → Development → Review → QA → Released, with rework "
                       "from Review and QA back to Development. A merge moves work to QA, "
                       "not to completion, so acceptance happens before Released.",
        "definition": _definition(
            [("Backlog", "todo", "neutral", False),
             ("Ready", "todo", "primary", False),
             ("Development", "in_progress", "info", True),
             ("Review", "in_progress", "info", True),
             ("QA", "in_progress", "warning", True),
             ("Released", "done", "success", False)],
            mode="restricted", initial=0,
            transitions=[(0, 1), (1, 0), (1, 2), (2, 3), (3, 2), (3, 4), (4, 2), (4, 5),
                         (5, 2)],
            merge_target=4),
    },
    "support": {
        "name": "Support",
        "description": "New → Triage → Investigating → Resolved, with Resolved reopening to "
                       "Investigating. Merges only link: support work is not always code.",
        "definition": _definition(
            [("New", "todo", "neutral", False),
             ("Triage", "todo", "warning", False),
             ("Investigating", "in_progress", "info", True),
             ("Resolved", "done", "success", False)],
            mode="restricted", initial=0,
            transitions=[(0, 1), (1, 2), (2, 3), (3, 2)],
            merge_target=None),
    },
}

DEFAULT_TEMPLATE_KEY = "default"


def builtin_definition(key: str) -> dict[str, Any] | None:
    template = BUILTIN_TEMPLATES.get(key)
    return None if template is None else template["definition"]


def materialize(db: Session, space: Space, definition: dict[str, Any]) -> list[WorkflowStatus]:
    """Create a NEW space's stages, transitions, initial stage and merge target from a
    template definition. Only for a space with no stages yet: changing an existing
    workflow goes through the publish of services/workflow.py, with its impact review.

    The definition is trusted to be valid — built-ins are, and stored templates were
    validated when saved — but indexes are bounds-checked anyway, because a bad template
    must fail as a 422 at the caller rather than leave a half-built space.
    """
    stages = definition.get("stages") or []
    if not stages or len(stages) > rules.MAX_STAGES:
        raise ValueError("a template needs between 1 and 20 stages")
    rows: list[WorkflowStatus] = []
    for position, stage in enumerate(stages):
        category = stage["category"]
        if category not in rules.STATUS_CATEGORIES:
            raise ValueError(f"unknown category {category!r}")
        color = stage.get("color") or rules.DEFAULT_COLOR_BY_CATEGORY[category]
        row = WorkflowStatus(
            workspace_id=space.workspace_id, space_id=space.id, name=stage["name"],
            category=category, position=position, color=color,
            counts_toward_wip=bool(stage.get("counts_toward_wip",
                                             category == "in_progress")),
            wip_limit=stage.get("wip_limit"), deleted=0,
        )
        db.add(row)
        rows.append(row)
    db.flush()

    def at(index: int | None) -> WorkflowStatus | None:
        if index is None:
            return None
        if not 0 <= index < len(rows):
            raise ValueError(f"stage index {index} is out of range")
        return rows[index]

    initial = at(definition.get("initial", 0))
    space.initial_status_id = initial.id if initial is not None else None
    space.transition_mode = definition.get("transition_mode", "any")
    space.wip_policy = definition.get("wip_policy", "warn")
    target = at(definition.get("merge_target"))
    space.merge_action = "status" if target is not None else "none"
    space.merge_target_status_id = target.id if target is not None else None
    for transition in definition.get("transitions") or []:
        source = at(transition.get("from"))
        destination = at(transition.get("to"))
        if destination is None:
            raise ValueError("a transition needs a target stage")
        db.add(WorkflowTransition(workspace_id=space.workspace_id, space_id=space.id,
                                  from_status_id=source.id if source is not None else None,
                                  to_status_id=destination.id))
    db.flush()
    return rows


def definition_of_space(statuses: list[WorkflowStatus], space: Space,
                        transitions: list[tuple[str | None, str]]) -> dict[str, Any]:
    """A live workflow as a template definition — what "save as template" stores."""
    index = {status.id: position for position, status in enumerate(statuses)}
    return {
        "transition_mode": space.transition_mode,
        "wip_policy": space.wip_policy,
        "stages": [{"name": status.name, "category": status.category, "color": status.color,
                    "counts_toward_wip": bool(status.counts_toward_wip),
                    "wip_limit": status.wip_limit}
                   for status in statuses],
        "initial": index.get(space.initial_status_id or "", 0),
        "transitions": [{"from": index[source] if source is not None else None,
                         "to": index[to]}
                        for source, to in transitions
                        if to in index and (source is None or source in index)],
        "merge_target": (index.get(space.merge_target_status_id or "")
                         if space.merge_action == "status" else None),
    }
