"""Unit tests for multimodal autonomous conversion handlers."""
from unittest.mock import MagicMock, patch

from ai_chat.handlers.context import ToolContext
from ai_chat.handlers.conversion_handlers import (
    handle_bulk_issue_create,
    handle_issue_create_from_attachment,
    handle_workspace_scaffold_from_spec,
)
from dependencies.workspace import WorkspaceAccess
from models.chat_session import ChatMessageAttachment
from models.user import User
from models.workspace import Workspace
from schemas.response import SuccessResponse


def test_handle_issue_create_from_attachment():
    db = MagicMock()
    user = User(id="user-1", email="dev@pmt.com", first_name="Dev", last_name="User")
    access = WorkspaceAccess(workspace_id="ws-1", user_id="user-1", role="admin")
    ctx = ToolContext(db=db, workspace_id="ws-1", current_user=user, access=access, role="admin")

    ws = Workspace(id="ws-1", name="Mobile Workspace")
    att = ChatMessageAttachment(
        id="att-99",
        workspace_id="ws-1",
        original_filename="error_screenshot.png",
        mime_type="image/png",
    )
    db.query.return_value.filter.return_value.first.return_value = ws

    with patch("controllers.issue_controller.create_issue") as mock_create_issue, \
         patch("services.chat_attachment_service.get_chat_attachment") as mock_get_att, \
         patch("services.chat_attachment_service.read_chat_attachment_bytes") as mock_read_bytes, \
         patch("services.storage_service.upload_attachment") as mock_storage_upload:

        mock_create_issue.return_value = SuccessResponse(
            status_code=201,
            message="Issue created",
            data={"id": "iss-101", "key": "MOB-1", "title": "Checkout 500 error"},
        )
        mock_get_att.return_value = att
        mock_read_bytes.return_value = b"\x89PNGfakebytes"

        res = handle_issue_create_from_attachment(
            ctx,
            {
                "attachment_id": "att-99",
                "list_id": "list-1",
                "title": "Checkout 500 error",
                "type": "bug",
                "priority": "urgent",
            },
        )

        assert res["attachment_linked"] is True
        assert res["filename"] == "error_screenshot.png"
        assert res["issue"]["id"] == "iss-101"
        assert mock_storage_upload.called


def test_handle_workspace_scaffold_from_spec():
    db = MagicMock()
    user = User(id="user-1", email="dev@pmt.com")
    access = WorkspaceAccess(workspace_id="ws-1", user_id="user-1", role="admin")
    ctx = ToolContext(db=db, workspace_id="ws-1", current_user=user, access=access, role="admin")

    with patch("controllers.workspace_controller.create_space") as mock_space, \
         patch("controllers.workspace_controller.create_list") as mock_list, \
         patch("controllers.sprint_controller.create_sprint") as mock_sprint, \
         patch("controllers.issue_controller.create_issue") as mock_issue:

        mock_space.return_value = SuccessResponse(
            status_code=201,
            message="Space created",
            data={"id": "sp-1", "name": "Payment Gateway", "key": "PAY"},
        )
        mock_list.side_effect = [
            SuccessResponse(status_code=201, message="", data={"id": "l-1", "name": "Auth"}),
            SuccessResponse(status_code=201, message="", data={"id": "l-2", "name": "Checkout"}),
        ]
        mock_sprint.return_value = SuccessResponse(
            status_code=201, message="", data={"id": "sp-1", "name": "Sprint 1"}
        )
        mock_issue.return_value = SuccessResponse(
            status_code=201, message="", data={"id": "iss-1", "title": "Setup OAuth"}
        )

        res = handle_workspace_scaffold_from_spec(
            ctx,
            {
                "space_name": "Payment Gateway",
                "lists": ["Auth", "Checkout"],
                "sprint_name": "Sprint 1",
                "initial_issues": [{"title": "Setup OAuth", "list_name": "Auth"}],
            },
        )

        assert res["space"]["key"] == "PAY"
        assert len(res["lists"]) == 2
        assert len(res["issues_created"]) == 1
        assert "Payment Gateway" in res["summary"]


def test_handle_bulk_issue_create():
    db = MagicMock()
    user = User(id="user-1", email="dev@pmt.com")
    access = WorkspaceAccess(workspace_id="ws-1", user_id="user-1", role="admin")
    ctx = ToolContext(db=db, workspace_id="ws-1", current_user=user, access=access, role="admin")

    with patch("controllers.issue_controller.create_issue") as mock_issue:
        mock_issue.side_effect = [
            SuccessResponse(status_code=201, message="", data={"id": "i-1", "title": "Defect 1"}),
            SuccessResponse(status_code=201, message="", data={"id": "i-2", "title": "Defect 2"}),
        ]

        res = handle_bulk_issue_create(
            ctx,
            {
                "list_id": "list-10",
                "issues": [
                    {"title": "Defect 1", "priority": "high"},
                    {"title": "Defect 2", "priority": "medium"},
                ],
            },
        )

        assert res["total_requested"] == 2
        assert res["total_created"] == 2
        assert len(res["created_issues"]) == 2
