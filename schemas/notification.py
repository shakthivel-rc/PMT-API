from datetime import datetime
from pydantic import BaseModel, Field


class NotificationSummary(BaseModel):
    id: str
    user_id: str
    actor_id: str | None = None
    actor_name: str | None = None
    workspace_id: str | None = None
    issue_id: str | None = None
    type: str
    title: str
    message: str
    link: str | None = None
    is_read: bool
    created_at: datetime
    read_at: datetime | None = None

    model_config = {"from_attributes": True}


class NotificationListResponse(BaseModel):
    items: list[NotificationSummary]
    total: int
    unread_count: int
    page: int
    limit: int
