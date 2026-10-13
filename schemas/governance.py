"""The governance settings document, `workspaces.settings["governance"]`.

docs/design/09-restructuring.md §3.3: how strictly a workspace applies the restructuring
plan's creation rules. Shaped like the time-tracking and reports documents — every field has
a default, a stored document is validated on read, and one that no longer validates reads as
the defaults rather than failing every route that consults it.
"""
from pydantic import BaseModel, ConfigDict, Field


class GovernanceSettings(BaseModel):
    model_config = ConfigDict(json_schema_extra={
        "examples": [{"require_space_charter": True, "require_epic_owner_and_criteria": True,
                      "default_workflow_template": "default", "inactive_after_days": 90}],
    })

    require_space_charter: bool = Field(
        default=False,
        description="A new space needs an owner, a delivery method, an objective, a success "
                    "measure and a reason it is not an existing space (the creation "
                    "checklist).")
    require_epic_owner_and_criteria: bool = Field(
        default=True,
        description="A new epic, or an issue re-typed to one, needs an assignee (its owner) "
                    "and completion criteria. On by default: the plan states it as the "
                    "epic's creation rule.")
    default_workflow_template: str = Field(
        default="default", min_length=1, max_length=64,
        description="A built-in template key (`default`, `delivery`, `support`) or a "
                    "workspace template id: what a new space starts with.")
    inactive_after_days: int = Field(
        default=90, ge=7, le=3650,
        description="How long without activity before the inventory calls a container "
                    "inactive.")


class GovernanceSettingsUpdate(BaseModel):
    """A partial update. Only the fields present change."""

    model_config = ConfigDict(extra="forbid", json_schema_extra={
        "examples": [{"require_space_charter": True}]})

    require_space_charter: bool | None = None
    require_epic_owner_and_criteria: bool | None = None
    default_workflow_template: str | None = Field(default=None, min_length=1, max_length=64)
    inactive_after_days: int | None = Field(default=None, ge=7, le=3650)
