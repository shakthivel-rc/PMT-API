"""Inbound integrations. Today: the GitHub webhook, and what an admin reads about it.

**The webhook is outside JWT authentication** (`routes/auth_middleware.UNAUTHENTICATED_ROUTES`)
because GitHub cannot hold a session. It authenticates itself: every delivery must carry an
`X-Hub-Signature-256` HMAC of its raw body under `GITHUB_WEBHOOK_SECRET`, checked before the
body is parsed, and with no secret configured every delivery is refused. That is the claim
`tests/unit/test_route_inventory.PUBLIC` makes about it, and
`tests/integration/test_github_webhook.py` holds it to.

`async def` because the signature has to be computed over the raw bytes, which only
`await request.body()` gives; the database work is therefore handed to a thread, per
CLAUDE.md §9.

**`admin_router` is the authenticated half** (2026-10-05): per workspace, under the usual
tenancy gate, and nothing on it is exempt from JWT authentication. Today it lists the
deliveries a frozen space is holding back (docs/design/09-restructuring.md §6.3).
"""
import asyncio
import json
import logging

from fastapi import APIRouter, Depends, Header, Request
from sqlalchemy.orm import Session

from controllers.github_controller import apply_delivery, list_deferred
from core.config import get_settings
from db.session import get_db
from dependencies.workspace import WorkspaceAccess, as_admin
from integrations.github import verify_signature
from routes.workspaces import NOT_A_MEMBER
from schemas.errors import COMMON_ERRORS
from schemas.response import ErrorResponse, SuccessResponse, error_json
from schemas.responses.integrations import DeferredDelivery, WebhookResult

router = APIRouter(prefix="/integrations", tags=["Integrations"])
#: The authenticated, per-workspace half: what an admin reads about integrations
#: (deferred deliveries, 09-restructuring.md §6.3). The webhook above stays public.
admin_router = APIRouter(prefix="/workspaces/{workspace_id}/integrations",
                         tags=["Integrations"])
logger = logging.getLogger("base_project.github")


@router.post(
    "/github/webhook",
    response_model=SuccessResponse[WebhookResult],
    responses={
        400: {"model": ErrorResponse, "description": "Not a well-formed GitHub delivery."},
        401: {"model": ErrorResponse,
              "description": "The signature is missing or wrong, or no secret is configured."},
        413: {"model": ErrorResponse, "description": "Body over MAX_REQUEST_BODY_BYTES."},
        429: {"model": ErrorResponse, "description": "Rate limited, per source address."},
    },
    summary="Receive a GitHub webhook delivery",
    operation_id="receive_github_webhook",
)
async def receive_github_webhook(
    request: Request,
    db: Session = Depends(get_db),
    x_github_event: str | None = Header(None),
    x_github_delivery: str | None = Header(None),
    x_hub_signature_256: str | None = Header(None),
):
    """Links pull requests to the issues their branch, title or body names, and moves an
    issue to Done when a linked pull request is merged. Answers 200 for anything it chooses
    not to act on, so GitHub's delivery log stays green for events that are simply not ours.

    2026-10-05 (docs/design/09-restructuring.md §6, §7.3): "to Done" is now the space's merge
    action — a move to the stage the space chose, or only the link — and old keys of moved or
    re-keyed issues still find them. A delivery naming an issue in a space frozen for a
    migration is deferred, still with a 200, and applied when the space is unfrozen."""
    body = await request.body()
    if not verify_signature(get_settings().github_webhook_secret, body, x_hub_signature_256):
        logger.warning("github delivery refused: bad or missing signature",
                       extra={"extra_fields": {"delivery_id": x_github_delivery}})
        return error_json(401, "The webhook signature did not verify.")
    if not x_github_event or not x_github_delivery or len(x_github_delivery) > 64:
        return error_json(400, "Missing X-GitHub-Event or X-GitHub-Delivery.")
    try:
        payload = json.loads(body)
    except ValueError:
        return error_json(400, "The body is not JSON.")
    if not isinstance(payload, dict):
        return error_json(400, "The body is not a JSON object.")

    def _apply() -> dict:
        return apply_delivery(db, x_github_event, x_github_delivery, payload)

    result = await asyncio.to_thread(_apply)
    return SuccessResponse(status_code=200, message="Delivery processed", data=result)


@router.post(
    "/gitlab/webhook",
    response_model=SuccessResponse[dict],
    responses={
        400: {"model": ErrorResponse, "description": "Not a well-formed GitLab delivery."},
        401: {"model": ErrorResponse, "description": "Token is missing or wrong."},
        413: {"model": ErrorResponse, "description": "Body over MAX_REQUEST_BODY_BYTES."},
        429: {"model": ErrorResponse, "description": "Rate limited, per source address."},
    },
    summary="Receive a GitLab webhook delivery",
    operation_id="receive_gitlab_webhook",
)
async def receive_gitlab_webhook(
    request: Request,
    db: Session = Depends(get_db),
    x_gitlab_event: str | None = Header(None),
    x_gitlab_token: str | None = Header(None),
    x_gitlab_delivery: str | None = Header(None),
):
    """Receives GitLab merge requests, pipelines, and deployments."""
    from integrations.gitlab import verify_gitlab_token
    from controllers.vcs_controller import apply_gitlab_delivery

    body = await request.body()
    if not verify_gitlab_token(get_settings().gitlab_webhook_secret, x_gitlab_token):
        logger.warning("gitlab delivery refused: bad or missing token")
        return error_json(401, "The GitLab token did not verify.")

    delivery_id = x_gitlab_delivery or str(hash(body))[:32]
    try:
        payload = json.loads(body)
    except ValueError:
        return error_json(400, "The body is not JSON.")
    if not isinstance(payload, dict):
        return error_json(400, "The body is not a JSON object.")

    def _apply() -> dict:
        return apply_gitlab_delivery(db, x_gitlab_event, delivery_id, payload)

    result = await asyncio.to_thread(_apply)
    return SuccessResponse(status_code=200, message="GitLab delivery processed", data=result)


@router.post(
    "/bitbucket/webhook",
    response_model=SuccessResponse[dict],
    responses={
        400: {"model": ErrorResponse, "description": "Not a well-formed Bitbucket delivery."},
        401: {"model": ErrorResponse, "description": "Signature is missing or wrong."},
        413: {"model": ErrorResponse, "description": "Body over MAX_REQUEST_BODY_BYTES."},
        429: {"model": ErrorResponse, "description": "Rate limited, per source address."},
    },
    summary="Receive a Bitbucket webhook delivery",
    operation_id="receive_bitbucket_webhook",
)
async def receive_bitbucket_webhook(
    request: Request,
    db: Session = Depends(get_db),
    x_event_key: str | None = Header(None),
    x_hub_signature: str | None = Header(None),
    x_hook_uuid: str | None = Header(None),
):
    """Receives Bitbucket pull requests and commit status events."""
    from integrations.bitbucket import verify_bitbucket_signature
    from controllers.vcs_controller import apply_bitbucket_delivery

    body = await request.body()
    if not verify_bitbucket_signature(get_settings().bitbucket_webhook_secret, body, x_hub_signature):
        logger.warning("bitbucket delivery refused: bad or missing signature")
        return error_json(401, "The Bitbucket signature did not verify.")

    delivery_id = x_hook_uuid or str(hash(body))[:32]
    try:
        payload = json.loads(body)
    except ValueError:
        return error_json(400, "The body is not JSON.")
    if not isinstance(payload, dict):
        return error_json(400, "The body is not a JSON object.")

    def _apply() -> dict:
        return apply_bitbucket_delivery(db, x_event_key, delivery_id, payload)

    result = await asyncio.to_thread(_apply)
    return SuccessResponse(status_code=200, message="Bitbucket delivery processed", data=result)


@admin_router.get(
    "/deferred",
    response_model=SuccessResponse[list[DeferredDelivery]],
    responses={**COMMON_ERRORS, **NOT_A_MEMBER},
    summary="List the GitHub deliveries held back by a frozen space",
    operation_id="list_deferred_deliveries",
)
def list_deferred_deliveries(access: WorkspaceAccess = Depends(as_admin),
                             db: Session = Depends(get_db)):
    """Admins only. Newest first, at most 200. Each is applied, oldest first, when the space
    holding it back is unfrozen; until then nothing of it has been written."""
    return list_deferred(access, db)
