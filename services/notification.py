"""Service layer for user in-app notifications."""
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import desc, func
from sqlalchemy.orm import Session

from models.notification import Notification
from models.user import User


def create_notification(
    db: Session,
    user_id: str,
    title: str,
    message: str,
    type: str = "system",
    actor_id: str | None = None,
    workspace_id: str | None = None,
    issue_id: str | None = None,
    link: str | None = None,
) -> Notification:
    notif = Notification(
        user_id=user_id,
        actor_id=actor_id,
        workspace_id=workspace_id,
        issue_id=issue_id,
        type=type,
        title=title,
        message=message,
        link=link,
        is_read=False,
    )
    db.add(notif)
    db.flush()
    return notif


def get_user_notifications(
    db: Session,
    user_id: str,
    unread_only: bool = False,
    page: int = 1,
    limit: int = 20,
) -> tuple[list[dict[str, Any]], int, int]:
    # Total unread count
    unread_count = (
        db.query(func.count(Notification.id))
        .filter(Notification.user_id == user_id, Notification.is_read.is_(False))
        .scalar()
        or 0
    )

    query = db.query(Notification).filter(Notification.user_id == user_id)
    if unread_only:
        query = query.filter(Notification.is_read.is_(False))

    total = query.count()

    offset = (page - 1) * limit
    rows = (
        query.order_by(desc(Notification.created_at))
        .offset(offset)
        .limit(limit)
        .all()
    )

    actor_ids = {r.actor_id for r in rows if r.actor_id}
    actors = {}
    if actor_ids:
        for u in db.query(User).filter(User.id.in_(actor_ids)).all():
            actors[u.id] = f"{u.first_name} {u.last_name}".strip() or u.username

    items = []
    for r in rows:
        items.append({
            "id": r.id,
            "user_id": r.user_id,
            "actor_id": r.actor_id,
            "actor_name": actors.get(r.actor_id) if r.actor_id else None,
            "workspace_id": r.workspace_id,
            "issue_id": r.issue_id,
            "type": r.type,
            "title": r.title,
            "message": r.message,
            "link": r.link,
            "is_read": r.is_read,
            "created_at": r.created_at,
            "read_at": r.read_at,
        })

    return items, total, unread_count


def mark_as_read(db: Session, user_id: str, notification_id: str) -> Notification | None:
    notif = (
        db.query(Notification)
        .filter(Notification.id == notification_id, Notification.user_id == user_id)
        .first()
    )
    if not notif:
        return None

    if not notif.is_read:
        notif.is_read = True
        notif.read_at = datetime.now(UTC)
        db.commit()

    return notif


def mark_all_as_read(db: Session, user_id: str) -> int:
    count = (
        db.query(Notification)
        .filter(Notification.user_id == user_id, Notification.is_read.is_(False))
        .update(
            {"is_read": True, "read_at": datetime.now(UTC)},
            synchronize_session=False,
        )
    )
    db.commit()
    return count
