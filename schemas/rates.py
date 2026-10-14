"""Financial billing rates and currency schemas for workspaces."""
from pydantic import BaseModel, ConfigDict, Field


class MemberRateItem(BaseModel):
    """A member's billing rate in a workspace."""

    user_id: str = Field(description="Account id.")
    email: str = Field(description="Account email.")
    display_name: str = Field(description="User's display name.")
    role: str = Field(description="Role in workspace.")
    hourly_rate: float | None = Field(default=None, description="Hourly billing rate override, or null.")


class WorkspaceRatesResponse(BaseModel):
    """The workspace's financial currency and member rates."""

    currency: str = Field(default="USD", description="3-letter currency code (USD, EUR, GBP, INR, etc.).")
    default_billing_rate: float | None = Field(default=None, description="Default hourly billing rate for the workspace.")
    member_rates: list[MemberRateItem] = Field(default_factory=list, description="Per-member billing rates.")


class MemberRateUpdate(BaseModel):
    """An update to a member's billing rate."""

    user_id: str = Field(description="Account id.")
    hourly_rate: float | None = Field(default=None, ge=0, description="Hourly rate override, or null to clear.")


class WorkspaceRatesUpdate(BaseModel):
    """A change to the workspace's currency, default rate, or member rate overrides."""

    model_config = ConfigDict(extra="forbid")

    currency: str | None = Field(default=None, min_length=3, max_length=3, description="3-letter currency code.")
    default_billing_rate: float | None = Field(default=None, ge=0, description="Workspace default rate, or null to clear.")
    member_rates: list[MemberRateUpdate] | None = Field(default=None, description="List of member rate overrides.")
