"""User notification routes."""
from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy.orm import Session

from controllers import notification_controller as ctl
from db.session import get_db
from models import notification as _notif_model  # noqa: F401
from schemas.errors import COMMON_ERRORS
from schemas.notification import NotificationListResponse
from schemas.response import SuccessResponse

router = APIRouter(prefix="/notifications", tags=["Notifications"])


@router.get(
    "",
    response_model=SuccessResponse[NotificationListResponse],
    responses=COMMON_ERRORS,
    summary="List your notifications",
    operation_id="list_user_notifications",
)
def list_notifications(
    request: Request,
    unread_only: bool = Query(False, description="Filter for unread notifications only"),
    page: int = Query(1, ge=1, description="Page number"),
    limit: int = Query(20, ge=1, le=100, description="Items per page"),
    db: Session = Depends(get_db),
):
    """Retrieve notifications for the authenticated user, ordered by creation date descending."""
    return ctl.list_notifications(
        request, db, unread_only=unread_only, page=page, limit=limit
    )


@router.patch(
    "/{notification_id}/read",
    response_model=SuccessResponse,
    responses=COMMON_ERRORS,
    summary="Mark a notification as read",
    operation_id="mark_notification_as_read",
)
def mark_notification_as_read(
    notification_id: str,
    request: Request,
    db: Session = Depends(get_db),
):
    """Mark an individual notification as read."""
    return ctl.mark_read(request, notification_id, db)


@router.post(
    "/mark-all-read",
    response_model=SuccessResponse,
    responses=COMMON_ERRORS,
    summary="Mark all notifications as read",
    operation_id="mark_all_notifications_as_read",
)
def mark_all_notifications_as_read(
    request: Request,
    db: Session = Depends(get_db),
):
    """Mark all unread notifications for the user as read."""
    return ctl.mark_all_read(request, db)
