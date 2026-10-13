"""Context passed to all tool handlers, encapsulating database session,
active workspace, authenticated user, and workspace access RBAC role.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

from fastapi import Request
from sqlalchemy.orm import Session

from dependencies.workspace import WorkspaceAccess
from models.user import User


@dataclass
class ToolContext:
    db: Session
    workspace_id: str
    current_user: User
    access: WorkspaceAccess
    role: str  # owner, admin, member, viewer
    request: Optional[Request] = None
    today: str = "2026-10-08"
    date_format: str = "DD-MM-YYYY"
    timezone: str = "UTC"

    @property
    def user_id(self) -> Any:
        return self.current_user.id

    def get_request(self) -> Request:
        if self.request is not None:
            return self.request
        return Request({"type": "http", "client": ("127.0.0.1", 80), "headers": []})
