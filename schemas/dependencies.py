"""Request bodies for the typed dependency routes (`routes/dependencies.py`).

docs/design/09-restructuring.md §10. A dependency names its target either by id — what the
SPA has when someone picks an issue from a search — or by key, which is what a person types
or pastes (`OPS-12`). Keys go through the one resolver (services/key_resolution.py), so a key
quoted before an issue moved, or before its space changed key, still finds it.
"""
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from utils.issue_keys import SPACE_KEY_PATTERN

#: `models.issue.DEPENDENCY_KINDS`, spelled again as a Literal because this module is
#: type-checked and must not import `models/` (utils/workflow_rules.py says why: SQLAlchemy's
#: untyped surface would come into the check with it).
#: tests/integration/test_dependencies.py pins the two together.
DependencyKind = Literal["blocks", "relates", "duplicates", "clones"]

#: An issue key as a person types it. The number is the same shape `utils/issue_keys` finds
#: in branch names — at most nine digits, no leading zero — and the space key is the shape
#: a space can be given, so anything that can be called a key can be looked up.
ISSUE_KEY_PATTERN = rf"^{SPACE_KEY_PATTERN}-[1-9][0-9]{{0,8}}$"
#: Ten characters of space key, a hyphen and nine digits.
MAX_ISSUE_KEY = 20


class DependencyCreate(BaseModel):
    """A typed link from the issue in the path to another issue of the workspace."""

    model_config = ConfigDict(extra="forbid", json_schema_extra={
        "examples": [{"kind": "blocks", "target_key": "OPS-12"},
                     {"kind": "relates", "target_issue_id": "9f0c…"}],
    })

    kind: DependencyKind = Field(
        description="`blocks`, `duplicates` and `clones` read from the issue in the path to "
                    "the target (\"PAY-3 blocks OPS-12\"); `relates` is symmetric and is "
                    "stored once.")
    target_issue_id: str | None = Field(default=None, min_length=1, max_length=36,
                                        description="The other issue's id. Give this or "
                                                    "`target_key`, not both.")
    target_key: str | None = Field(
        default=None, max_length=MAX_ISSUE_KEY, pattern=ISSUE_KEY_PATTERN,
        description="The other issue's key, `OPS-12`, in any case. An old key — from before "
                    "the issue moved, or before its space changed key — still resolves.")

    @field_validator("target_key", mode="before")
    @classmethod
    def _normalise_key(cls, value: object) -> object:
        # Upper-cased before the pattern runs. A key typed into a dialog is a deliberate
        # reference, unlike a lower-case word in a commit message, which is why the webhook's
        # case-sensitive matching (utils/issue_keys.py) does not apply here.
        return value.strip().upper() if isinstance(value, str) else value

    @model_validator(mode="after")
    def _exactly_one_target(self) -> "DependencyCreate":
        if (self.target_issue_id is None) == (self.target_key is None):
            raise ValueError("Give exactly one of target_issue_id or target_key.")
        return self

    def key_parts(self) -> tuple[str, int] | None:
        """`("OPS", 12)` for `target_key`, or None when the target is named by id."""
        if self.target_key is None:
            return None
        space_key, _, number = self.target_key.rpartition("-")
        return space_key, int(number)
