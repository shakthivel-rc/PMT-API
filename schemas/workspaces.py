"""Request bodies for the tenancy and hierarchy routes (`routes/workspaces.py`).

Since 2026-10-05 (docs/design/09-restructuring.md §3) a space carries the restructuring
plan's creation checklist — the **charter** — and its containers can be renamed, reparented,
archived and frozen. The charter is a JSON document on `spaces.charter`, and the column does
not constrain it, so `SpaceCharter` below is what validates it: on every write, and on every
read (services/workspace.charter_of), the way the `workspaces.settings` documents are.

Since 2026-10-06 a workspace can be renamed and given another address (`WorkspaceUpdate`)
and a member can be given another role (`MemberUpdate`); deleting a workspace and removing a
member take no body.
"""
import re
from typing import Annotated, Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)

from utils.issue_keys import SPACE_KEY_PATTERN

WorkspaceRole = Literal["guest", "member", "admin", "owner"]

#: A workspace's address: lower-case letters and digits, hyphens inside. One constant for the
#: create and the update body, so a slug one accepts is never one the other refuses.
WORKSPACE_SLUG_PATTERN = r"^[a-z0-9][a-z0-9-]*[a-z0-9]$"

#: How a space delivers: Scrum (goal-based sprints) or Kanban (continuous flow), the plan's two
#: operating policies (§4). The same values as `utils/workflow_rules.DELIVERY_METHODS`; a
#: Literal so the contract lists them, and tests/integration/test_space_create.py fails if
#: the two drift apart.
DeliveryMethod = Literal["scrum", "kanban"]

#: Every charter field is free text of at most this many characters (09-restructuring.md
#: §3.2): "the checklist is guidance first". Two thousand is a long paragraph — enough for an
#: objective with its success measure spelled out, and short enough that the checklist stays a
#: checklist rather than becoming the project's documentation, which belongs in a document.
MAX_CHARTER_FIELD = 2_000

#: A space description is prose about the space — what it is for, who to ask. Ten thousand
#: characters is several pages; the structured parts belong in the charter. `spaces.description`
#: is TEXT (64 KiB on MySQL), so the limit is the product's, not the column's.
MAX_SPACE_DESCRIPTION = 10_000

#: `initial_lists` names at most this many lists (09-restructuring.md §3.2). The plan says to
#: start with one delivery list and split only where ownership or demand justify it; ten
#: leaves room for a programme with several durable workstreams without turning space
#: creation into a bulk import.
MAX_INITIAL_LISTS = 10

#: A workflow template reference: `default`, `delivery`, `support`, `builtin:<key>`, or a
#: workspace template's id (a 36-character uuid). 64 is what
#: `schemas/governance.GovernanceSettings.default_workflow_template` accepts, so any default a
#: workspace can set is a value this field can name too.
MAX_TEMPLATE_REF = 64

#: A container's position among its siblings. Positions are small integers a person sets by
#: dragging; the ceiling stays far inside the 32-bit INTEGER column, so a runaway value from a
#: client is a 422 rather than a database error on MySQL.
MAX_POSITION = 1_000_000

ContainerName = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1,
                                                 max_length=100)]


class WorkspaceCreate(BaseModel):
    """A new workspace. The caller becomes its owner."""

    model_config = ConfigDict(json_schema_extra={
        "examples": [{"name": "Acme Engineering", "slug": "acme-eng"}],
    })

    name: str = Field(min_length=1, max_length=100, description="Display name.")
    slug: str = Field(
        min_length=2, max_length=50, pattern=WORKSPACE_SLUG_PATTERN,
        description="Globally unique, lower-case, URL-safe. Appears in shared links.",
    )


class WorkspaceUpdate(BaseModel):
    """Rename a workspace or change its address. Only the fields present change, and at
    least one has to be present: an empty body is a mistake in the client, not a request."""

    model_config = ConfigDict(extra="forbid", json_schema_extra={
        "examples": [{"name": "Acme Platform"}, {"name": "Acme Platform", "slug": "acme-pl"}],
    })

    name: ContainerName | None = Field(default=None,
                                       description="Display name, trimmed. Cannot be "
                                                   "cleared.")
    slug: str | None = Field(
        default=None, min_length=2, max_length=50, pattern=WORKSPACE_SLUG_PATTERN,
        description="A new address, with the rules of `WorkspaceCreate.slug`. The address "
                    "of a deleted workspace stays reserved, so one may be refused that no "
                    "live workspace uses. Cannot be cleared.")

    @field_validator("name", "slug")
    @classmethod
    def _not_cleared(cls, value: str | None) -> str | None:
        # Runs only for a field that was sent: an explicit null. A workspace has to have a
        # name and an address, so null cannot mean "clear it" here as it does elsewhere.
        if value is None:
            raise ValueError("cannot be cleared; leave the field out to keep it")
        return value

    @model_validator(mode="after")
    def _something_to_change(self) -> "WorkspaceUpdate":
        if not self.model_fields_set:
            raise ValueError("send name, slug or both")
        return self


class MemberAdd(BaseModel):
    """Add an existing account to the workspace, by its sign-in address."""

    model_config = ConfigDict(json_schema_extra={
        "examples": [{"email": "dev@example.com", "role": "member"}],
    })

    email: str = Field(min_length=3, max_length=100,
                       description="The account's sign-in address. It must already exist.")
    role: WorkspaceRole = Field(
        default="member",
        description="`guest` reads; `member` also writes issues; `admin` also manages "
                    "spaces, lists, repositories and members; `owner` is `admin` that "
                    "cannot be removed by an admin.",
    )


class MemberUpdate(BaseModel):
    """Give a member another role in the workspace."""

    model_config = ConfigDict(extra="forbid", json_schema_extra={
        "examples": [{"role": "admin"}],
    })

    role: WorkspaceRole = Field(
        description="The new role, with the meanings of `MemberAdd.role`. Only an owner "
                    "makes someone an owner or changes an owner's role, and the last owner "
                    "cannot be given another one. The role they already have changes "
                    "nothing.")


def _charter_text(description: str) -> Any:
    """One charter field: optional text, at most `MAX_CHARTER_FIELD` characters. `Any`
    because that is what `Field` returns: the declaration, not a value."""
    return Field(default=None, max_length=MAX_CHARTER_FIELD, description=description)


class SpaceCharter(BaseModel):
    """The space's creation checklist (restructuring plan §3; 09-restructuring.md §3.2).

    Every field is optional text: the checklist is guidance first. A workspace that turns on
    `require_space_charter` makes the objective, the success measure and the reuse
    justification required when a space is created — together with the space's `owner_id`
    and `delivery_method`, which are columns of the space rather than charter text.

    Surrounding whitespace is trimmed and a blank field is stored as null, so "filled in"
    always means "has words in it" — the question the governance check and the inventory ask.
    """

    model_config = ConfigDict(extra="forbid", json_schema_extra={
        "examples": [{
            "objective": "Customers can refund a card payment without calling support.",
            "success_measure": "Refund tickets down 50% within a quarter of launch.",
            "delivery_team": "Payments squad: four engineers and a designer.",
            "scope": "Card refunds, partial refunds, the refund e-mail.",
            "exclusions": "Bank transfers.",
            "reuse_justification": "Payments has its own sprint cadence; the platform "
                                   "space runs Kanban.",
            "reporting_cadence": "Sprint review every second Friday.",
        }],
    })

    objective: str | None = _charter_text("The business objective: what changes for whom.")
    success_measure: str | None = _charter_text("How the objective will be measured.")
    delivery_team: str | None = _charter_text(
        "Who delivers the work. The accountable owner is the space's `owner_id`.")
    scope: str | None = _charter_text("What the space's work covers.")
    exclusions: str | None = _charter_text("What it deliberately does not cover.")
    access_requirements: str | None = _charter_text(
        "Who needs to see or change the work, and anything that restricts it.")
    reuse_justification: str | None = _charter_text(
        "Why an existing space will not do: a separate backlog, sprint cadence or a "
        "materially different workflow.")
    canonical_lists: str | None = _charter_text(
        "The lists work lives in, as prose. The lists themselves are `initial_lists`.")
    epic_completion_criteria: str | None = _charter_text(
        "What an epic in this space needs before it counts as done.")
    initial_backlog: str | None = _charter_text("The work the space starts with.")
    reporting_cadence: str | None = _charter_text("When and how progress is reported.")
    retention_policy: str | None = _charter_text(
        "When the space is archived, and how long its history is kept.")
    notes: str | None = _charter_text("Anything else.")

    @field_validator("*", mode="before")
    @classmethod
    def _blank_is_missing(cls, value: object) -> object:
        # Before the length check, so the 2,000 characters count the words, not the padding.
        if isinstance(value, str):
            value = value.strip()
            return value or None
        return value

    def filled(self) -> bool:
        """Whether any field has text in it."""
        return any(value is not None for value in self.model_dump().values())


class SpaceCreate(BaseModel):
    """A new space: a stable product or delivery unit with its own workflow and cadence.

    Only `name` and `key` are required — the two-field body every client already sends still
    creates a space with the governance default template, itself the four-stage default.
    Everything else is the restructuring plan's creation checklist (09-restructuring.md §3.2),
    which a workspace can make mandatory with `require_space_charter`.
    """

    # `forbid` since 2026-10-05: the body now carries the template and the charter, and a
    # misspelt `workflow_template` silently creating a four-stage space is a worse answer
    # than a 422 naming the field. Every body the old two-field contract allowed still passes.
    model_config = ConfigDict(extra="forbid", json_schema_extra={
        "examples": [
            {"name": "Payments", "key": "PAY"},
            {"name": "Payments", "key": "PAY",
             "description": "Card payments and refunds.",
             "owner_id": "<a member's user id>", "delivery_method": "scrum",
             "workflow_template": "delivery", "initial_lists": ["Delivery"],
             "charter": {"objective": "Customers can refund a card payment themselves.",
                         "success_measure": "Refund tickets down 50% in a quarter.",
                         "reuse_justification": "Its own sprint cadence."}},
        ],
    })

    name: str = Field(min_length=1, max_length=100, description="Display name.")
    key: str = Field(
        pattern=rf"^{SPACE_KEY_PATTERN}$",
        description="The issue-key prefix, 2-10 upper-case letters or digits starting with "
                    "a letter: `PAY` makes `PAY-1`. Unique within the workspace, and a key "
                    "another space used to have stays reserved for it (old issue keys keep "
                    "resolving).",
    )
    description: str | None = Field(default=None, max_length=MAX_SPACE_DESCRIPTION,
                                     description="What the space is for, as prose.")
    owner_id: str | None = Field(default=None,
                                 description="The accountable owner: a member's user id.")
    delivery_method: DeliveryMethod | None = Field(
        default=None, description="`scrum` (sprints toward a goal) or `kanban` (continuous "
                                  "flow).")
    charter: SpaceCharter | None = Field(default=None, description="The creation checklist.")
    budget_amount: float | None = Field(default=None, ge=0,
                                        description="Optional financial budget ceiling.")
    budget_alert_threshold: int | None = Field(default=80, ge=1, le=100,
                                               description="Budget alert percentage threshold.")
    ai_triage_enabled: bool | None = Field(default=True, description="Enable autonomous bug triage & deduplication")
    ai_standup_enabled: bool | None = Field(default=True, description="Enable automated async daily standups")
    ai_retrospective_enabled: bool | None = Field(default=True, description="Enable automated sprint retrospectives")
    workflow_template: str | None = Field(
        default=None, min_length=1, max_length=MAX_TEMPLATE_REF,
        description="The workflow to start with: `default`, `delivery` or `support` (also "
                    "spelt `builtin:<key>`), or the id of one of this workspace's templates. "
                    "Default: the workspace's governance `default_workflow_template`, or "
                    "`default` when that names a template that no longer exists.")
    initial_lists: list[ContainerName] = Field(
        default_factory=list, max_length=MAX_INITIAL_LISTS,
        description="Lists to create at the space root, in this order. Names are distinct, "
                    "ignoring case.")

    @field_validator("initial_lists")
    @classmethod
    def _distinct_lists(cls, value: list[str]) -> list[str]:
        # Two lists of one name in one space is a finding of the inventory (09-restructuring.md
        # §8.1); one request asking for both is a typo, not a design.
        seen: set[str] = set()
        for name in value:
            if name.casefold() in seen:
                raise ValueError(f"the list name {name!r} appears twice")
            seen.add(name.casefold())
        return value


class SpaceUpdate(BaseModel):
    """A change to a space. Only the fields present change; null clears the nullable ones.

    `charter` replaces the whole checklist: fields left out of it are cleared, so a form
    sends the document it shows. A new `key` keeps the old one resolving and reserved
    (09-restructuring.md §3.5).
    """

    model_config = ConfigDict(extra="forbid", json_schema_extra={
        "examples": [{"key": "PAYM"}, {"owner_id": "<a member's user id>",
                                       "delivery_method": "kanban"}],
    })

    name: ContainerName | None = Field(default=None,
                                       description="Display name, trimmed. Cannot be "
                                                   "cleared.")
    key: str | None = Field(
        default=None, pattern=rf"^{SPACE_KEY_PATTERN}$",
        description="A new issue-key prefix. Every issue's key changes with it; the old key "
                    "stays an alias that resolves to the same issues and that no other "
                    "space can take. Changing back to one of the space's own old keys is "
                    "allowed. Cannot be cleared.")
    description: str | None = Field(default=None, max_length=MAX_SPACE_DESCRIPTION,
                                     description="Prose about the space; null clears it.")
    owner_id: str | None = Field(default=None,
                                 description="A member's user id; null clears it.")
    delivery_method: DeliveryMethod | None = Field(default=None,
                                                   description="null clears it.")
    charter: SpaceCharter | None = Field(
        default=None, description="The whole checklist, replacing the stored one; null "
                                  "clears it.")
    budget_amount: float | None = Field(default=None, ge=0,
                                        description="Optional financial budget ceiling; null clears.")
    budget_alert_threshold: int | None = Field(default=None, ge=1, le=100,
                                               description="Budget alert percentage threshold; null clears.")
    ai_triage_enabled: bool | None = Field(default=None, description="Enable autonomous bug triage & deduplication; null clears.")
    ai_standup_enabled: bool | None = Field(default=None, description="Enable automated async daily standups; null clears.")
    ai_retrospective_enabled: bool | None = Field(default=None, description="Enable automated sprint retrospectives; null clears.")


class SpaceFreeze(BaseModel):
    """Freeze a space: read-only until it is unfrozen, for the length of a reorganisation."""

    model_config = ConfigDict(extra="forbid", json_schema_extra={
        "examples": [{"reason": "Wave 1: moving the Payments lists into Delivery"}],
    })

    # 500 is `spaces.freeze_reason`'s length; the reason is quoted in every refusal a frozen
    # space gives, so it is one sentence, not a page.
    reason: str = Field(min_length=1, max_length=500,
                        description="Why, in one sentence. Shown to everyone whose change "
                                    "the freeze refuses.")

    @field_validator("reason")
    @classmethod
    def _not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("say why the space is being frozen")
        return value.strip()


class FolderCreate(BaseModel):
    """A folder inside a space. Optional structure: lists may also sit on the space."""

    model_config = ConfigDict(json_schema_extra={"examples": [{"name": "Q4 initiatives"}]})

    name: str = Field(min_length=1, max_length=100, description="Display name.")


class FolderUpdate(BaseModel):
    """Rename or reorder a folder. Only the fields present change."""

    model_config = ConfigDict(extra="forbid", json_schema_extra={
        "examples": [{"name": "Website programme", "position": 0}],
    })

    name: ContainerName | None = Field(default=None,
                                       description="Display name, trimmed. Cannot be "
                                                   "cleared.")
    position: int | None = Field(
        default=None, ge=0, le=MAX_POSITION,
        description="Order among the space's folders, set as given; folders sharing a "
                    "position are ordered by name.")


class ListCreate(BaseModel):
    """A list of issues, in a space and optionally in one of its folders."""

    model_config = ConfigDict(json_schema_extra={
        "examples": [{"space_id": "6a1d…", "folder_id": None, "name": "Backlog"}],
    })

    space_id: str = Field(description="The space the list belongs to.")
    folder_id: str | None = Field(default=None,
                                  description="A folder of that space, or null for none.")
    name: str = Field(min_length=1, max_length=100, description="Display name.")


class ListUpdate(BaseModel):
    """Rename, reorder or reparent a list within its space. Only the fields present change.

    Moving a list to another *space* is not here: its issues would change space and key, so
    it is a transfer or a migration (09-restructuring.md §3.5, §7, §8).
    """

    model_config = ConfigDict(extra="forbid", json_schema_extra={
        "examples": [{"folder_id": "<a folder of the same space>", "position": 2},
                     {"folder_id": None}],
    })

    name: ContainerName | None = Field(default=None,
                                       description="Display name, trimmed. Cannot be "
                                                   "cleared.")
    folder_id: str | None = Field(
        default=None,
        description="A folder of the same space, or null to move the list to the space "
                    "root. Without `position`, a moved list goes to the bottom of its new "
                    "place.")
    position: int | None = Field(
        default=None, ge=0, le=MAX_POSITION,
        description="Order among its siblings, set as given; lists sharing a position are "
                    "ordered by name.")


class RepositoryLink(BaseModel):
    """Accept GitHub webhook deliveries for this repository into this workspace."""

    model_config = ConfigDict(json_schema_extra={"examples": [{"full_name": "acme/payments"}]})

    full_name: str = Field(
        description="`owner/name`, as GitHub spells it. One repository feeds one workspace.",
    )

    @field_validator("full_name", mode="before")
    @classmethod
    def validate_full_name(cls, v: Any) -> str:
        if not isinstance(v, str):
            raise ValueError("Repository name must be a string.")
        s = v.strip()
        s = re.sub(r"^(?:https?://)?(?:www\.)?github\.com/", "", s, flags=re.IGNORECASE)
        s = re.sub(r"^git@github\.com:", "", s, flags=re.IGNORECASE)
        s = re.sub(r"\.git$", "", s, flags=re.IGNORECASE)
        s = s.strip("/")
        if "/" not in s:
            raise ValueError(
                "Repository name must include the owner or organization in 'owner/repository' format (e.g. 'organization/repository')."
            )
        if not re.match(r"^[A-Za-z0-9-]{1,39}/[A-Za-z0-9._-]{1,100}$", s):
            raise ValueError(
                "Repository name must be in 'owner/repo' format (e.g. 'acme/payments')."
            )
        return s
