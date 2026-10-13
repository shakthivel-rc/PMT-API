"""Payloads of the integration routes (`routes/integrations.py`).

`WebhookResult` is read by whoever opens a delivery in GitHub's webhook log, so every list in
it names issue keys — the thing that person can search for — and every refusal carries its
reason as a sentence. `DeferredDelivery` is the admin's view of what is waiting for a frozen
space to be unfrozen (docs/design/09-restructuring.md §6.3).
"""
from datetime import datetime

from pydantic import BaseModel, Field


class WebhookKeyReason(BaseModel):
    """An issue the delivery named but did not act on, and why."""

    key: str = Field(description="The issue's current key — an old key in the pull request "
                                 "resolves to it (09-restructuring.md §7.3).")
    reason: str = Field(description="Why, in a sentence written to be shown.")


class WebhookResult(BaseModel):
    """What a delivery did. Read by whoever opens the delivery in GitHub's webhook log."""

    event: str = Field(description="The `X-GitHub-Event` header, echoed.")
    handled: bool = Field(description="True when the delivery was applied here. False for "
                                      "events and actions this service ignores, for a "
                                      "repository no workspace has linked, for a duplicate "
                                      "and for a deferred delivery.")
    duplicate: bool = Field(description="True when this delivery id was already received.")
    linked: list[str] = Field(description="Issue keys the pull request is now linked to, as "
                                          "the issues are called now.")
    moved_to_done: list[str] = Field(description="The keys in `moved` whose new stage is in "
                                                 "the `done` category — completed work. A "
                                                 "merge that moves work to QA is in `moved` "
                                                 "and not here.")
    moved: list[str] = Field(description="Issue keys the space's merge action moved, into "
                                         "whatever stage the space chose (09-restructuring.md "
                                         "§6).")
    failed: list[WebhookKeyReason] = Field(
        description="Issues the merge action could not move — its target stage was removed, "
                    "or the workflow does not allow the move from where the issue is. Never "
                    "moved anywhere else instead; each also has an `automation_failed` event.")
    skipped: list[WebhookKeyReason] = Field(
        description="Issues in an archived list, folder or space: nothing was written to "
                    "them, not even the link.")
    deferred: bool = Field(description="True when the delivery named an issue in a space "
                                       "frozen for a migration. Nothing was applied; it is "
                                       "kept and applied when the space is unfrozen.")
    note: str = Field(description="Why nothing happened, when nothing did.")


class DeferredDelivery(BaseModel):
    """A GitHub delivery held back because it named an issue in a frozen space."""

    id: str = Field(description="GitHub's `X-GitHub-Delivery` id — what its webhook log shows.")
    event: str = Field(description="The `X-GitHub-Event` header: `pull_request`.")
    received_at: datetime = Field(description="When it arrived, UTC. Replays run oldest first.")
    note: str | None = Field(description="Which frozen space held it back.")
    keys: list[str] = Field(description="The issue keys the pull request names, as written "
                                        "in it (an old key is resolved only when it is "
                                        "replayed).")
