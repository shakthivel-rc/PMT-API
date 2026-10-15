"""Controller for user notifications."""
from fastapi import Request
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from schemas.response import ErrorResponse, SuccessResponse
from services import notification as svc


def list_notifications(
    request: Request,
    db: Session,
    unread_only: bool = False,
    page: int = 1,
    limit: int = 20,
):
    user = getattr(request.state, "user", None)
    if not user:
        return JSONResponse(
            status_code=401,
            content=ErrorResponse(status_code=401, message="Not authenticated").model_dump(),
        )

    user_id = user.get("sub")
    items, total, unread_count = svc.get_user_notifications(
        db, user_id=user_id, unread_only=unread_only, page=page, limit=limit
    )

    return SuccessResponse(
        status_code=200,
        message="Notifications retrieved",
        data={
            "items": items,
            "total": total,
            "unread_count": unread_count,
            "page": page,
            "limit": limit,
        },
    )


def mark_read(request: Request, notification_id: str, db: Session):
    user = getattr(request.state, "user", None)
    if not user:
        return JSONResponse(
            status_code=401,
            content=ErrorResponse(status_code=401, message="Not authenticated").model_dump(),
        )

    user_id = user.get("sub")
    notif = svc.mark_as_read(db, user_id, notification_id)
    if not notif:
        return JSONResponse(
            status_code=404,
            content=ErrorResponse(status_code=404, message="Notification not found").model_dump(),
        )

    return SuccessResponse(
        status_code=200,
        message="Notification marked as read",
        data={"id": notif.id, "is_read": True},
    )


def mark_all_read(request: Request, db: Session):
    user = getattr(request.state, "user", None)
    if not user:
        return JSONResponse(
            status_code=401,
            content=ErrorResponse(status_code=401, message="Not authenticated").model_dump(),
        )

    user_id = user.get("sub")
    count = svc.mark_all_as_read(db, user_id)

    return SuccessResponse(
        status_code=200,
        message="All notifications marked as read",
        data={"marked_count": count},
    )
